import { useMemo, useState, useEffect } from 'react';
import axios from 'axios';
import { 
  Cloud, Shield, Database, Search, Key, Loader2, CheckCircle,
  ArrowRight, FileText, Archive, Edit, Trash2,
  Folder, Plus, RefreshCw, Globe, Cpu, Clock, Activity, Terminal,
  Lock, LogOut, Download, HeartPulse, AlertCircle, X
} from 'lucide-react';

const API_BASE = import.meta.env.VITE_API_BASE || (
  window.location.port === '5173'
    ? `http://${window.location.hostname}:8000`
    : window.location.origin
);
const SESSION_TOKEN_KEY = 'OCI_MIGRATOR_SESSION_TOKEN';
const SESSION_USERNAME_KEY = 'OCI_MIGRATOR_SESSION_USERNAME';
const DEFAULT_REMOTE_CONFIG = {
  name: '',
  provider: 'oci',
  accessKey: '',
  secretKey: '',
  region: 'eu-stockholm-1',
  accountName: '',
  accountKey: '',
  gcpObjectAcl: 'bucketOwnerFullControl',
  gcpBucketAcl: 'private',
  gcpLocation: 'us-west3',
  localMode: 'server_folder',
  localFolderName: '',
  localMountPath: '',
  localShareAccess: 'none',
  localShareName: '',
  localShareUsername: '',
  localSharePassword: ''
};

function getLegacyApiToken() {
  return import.meta.env.VITE_API_TOKEN || localStorage.getItem('OCI_MIGRATOR_API_TOKEN') || '';
}

function getInitialAuth() {
  const sessionToken = localStorage.getItem(SESSION_TOKEN_KEY);
  if (sessionToken) {
    return {
      token: sessionToken,
      mode: 'session',
      username: localStorage.getItem(SESSION_USERNAME_KEY) || 'admin'
    };
  }

  const legacyApiToken = getLegacyApiToken();
  if (legacyApiToken) {
    return { token: legacyApiToken, mode: 'api-token', username: 'admin' };
  }

  return { token: '', mode: '', username: 'admin' };
}

function formatApiError(err, fallback = 'Request failed.') {
  const detail = err?.response?.data?.detail;
  if (typeof detail === 'string') return detail;
  if (detail && typeof detail === 'object') {
    const parts = [
      detail.message,
      detail.hint ? `Hint: ${detail.hint}` : '',
      detail.code ? `Code: ${detail.code}` : '',
      detail.service_message ? `OCI: ${detail.service_message}` : '',
      detail.error ? `Error: ${detail.error}` : ''
    ].filter(Boolean);
    return parts.join('\n');
  }
  return err?.message || fallback;
}

function runStatusClass(status = '') {
  const normalized = status.toLowerCase();
  if (normalized === 'success') return 'text-green-700 bg-green-50 border-green-200';
  if (normalized === 'failed' || normalized === 'timeout') return 'text-red-700 bg-red-50 border-red-200';
  if (normalized === 'running' || normalized === 'retrying') return 'text-blue-700 bg-blue-50 border-blue-200';
  return 'text-amber-700 bg-amber-50 border-amber-200';
}

function formatTimestamp(value) {
  if (!value) return 'Never';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

export default function App() {
  const [authState, setAuthState] = useState(getInitialAuth);
  const [loginForm, setLoginForm] = useState({ username: 'admin', password: '' });
  const [loginError, setLoginError] = useState('');
  const [authLoading, setAuthLoading] = useState(false);
  const [showPasswordPanel, setShowPasswordPanel] = useState(false);
  const [passwordForm, setPasswordForm] = useState({ currentPassword: '', newPassword: '', confirmPassword: '' });
  const [passwordMessage, setPasswordMessage] = useState('');
  const [notice, setNotice] = useState(null);
  const [health, setHealth] = useState(null);
  const [jobRuns, setJobRuns] = useState([]);
  const [exportingConfig, setExportingConfig] = useState(false);
  const isAuthenticated = Boolean(authState.token);

  const api = useMemo(() => {
    const headers = {};
    if (authState.mode === 'session') {
      headers.Authorization = `Bearer ${authState.token}`;
    } else if (authState.mode === 'api-token') {
      headers['X-API-Token'] = authState.token;
    }

    const instance = axios.create({
      baseURL: API_BASE,
      headers
    });

    instance.interceptors.response.use(
      (res) => res,
      (err) => {
        if (err?.response?.status === 401) {
          if (authState.mode === 'session') {
            localStorage.removeItem(SESSION_TOKEN_KEY);
            localStorage.removeItem(SESSION_USERNAME_KEY);
            setAuthState({ token: '', mode: '', username: 'admin' });
          } else if (authState.mode === 'api-token' && localStorage.getItem('OCI_MIGRATOR_API_TOKEN')) {
            localStorage.removeItem('OCI_MIGRATOR_API_TOKEN');
            setAuthState({ token: '', mode: '', username: 'admin' });
          }
          console.error('Unauthorized admin session.');
        }
        return Promise.reject(err);
      }
    );

    return instance;
  }, [authState]);

  const [vms, setVms] = useState([]);
  const [selectedVms, setSelectedVms] = useState([]);
  const [loading, setLoading] = useState(false);
  const [view, setView] = useState('keys'); 
  const [searchTerm, setSearchTerm] = useState('');
  
  // Multi-Tenant State (OCI Profiles)
  const [profiles, setProfiles] = useState([]);
  const [activeSourceProfile, setActiveSourceProfile] = useState(''); 
  const [formData, setFormData] = useState({
    profileName: '', userOcid: '', tenancyOcid: '', fingerprint: '', region: 'eu-stockholm-1', 
    compartmentOcid: '', storageCompartmentOcid: ''
  });
  
  // Add Remote State (Combined OCI + Big 5)
  const [remoteConfig, setRemoteConfig] = useState(DEFAULT_REMOTE_CONFIG);
  const [gcpKeyFile, setGcpKeyFile] = useState(null);

  // Rclone / Data Sync State
  const [remotes, setRemotes] = useState([]);
  const [remoteDetails, setRemoteDetails] = useState([]);
  const [jobs, setJobs] = useState([]);
  const [sourceBuckets, setSourceBuckets] = useState([]);
  const [destBuckets, setDestBuckets] = useState([]);

  const [syncJob, setSyncJob] = useState({
    name: '', source_remote: '', dest_profile: '', dest_bucket: '',
    sync_mode: 'copy', transfers: 16, checkers: 32, buffer_size: '128M',
    schedule: { frequency: 'none', time: '02:00', day_of_week: 'monday', day_of_month: '1' }
  });
  const visibleRemoteDetails = useMemo(() => {
    const detailsByName = new Map(remoteDetails.map((remote) => [remote.name, remote]));
    return remotes
      .filter((remoteName) => !remoteName.endsWith('_rclone'))
      .map((remoteName) => detailsByName.get(remoteName) || { name: remoteName, type: '' });
  }, [remotes, remoteDetails]);
  const localRemotes = visibleRemoteDetails.filter((remote) => remote.type === 'local');
  const externalRemotes = visibleRemoteDetails.filter((remote) => remote.type !== 'local');

  // VM Migration Panel State
  const [vmMigrationConfig, setVmMigrationConfig] = useState({
    destProfile: '', destBucket: ''
  });
  
  const [vmTasks, setVmTasks] = useState({});
  const [activeLogJob, setActiveLogJob] = useState(null);
  const [liveLogData, setLiveLogData] = useState("");
  const [activeRunLogId, setActiveRunLogId] = useState(null);
  const [runLogData, setRunLogData] = useState("");

  // Common UI State
  const [keyInputMode, setKeyInputMode] = useState('upload');
  const [file, setFile] = useState(null);
  const [pastedKey, setPastedKey] = useState('');
  const [lastKeySavedPath, setLastKeySavedPath] = useState('');

  useEffect(() => {
    if (!lastKeySavedPath) return;
    const timer = setTimeout(() => setLastKeySavedPath(''), 10_000);
    return () => clearTimeout(timer);
  }, [lastKeySavedPath]);

  // Storage Viewer State
  const [storageProfile, setStorageProfile] = useState('');
  const [storageBuckets, setStorageBuckets] = useState([]);
  const [selectedBucket, setSelectedBucket] = useState('');
  const [storageObjects, setStorageObjects] = useState([]);
  
  const [newBucketName, setNewBucketName] = useState('');
  const [newFolderName, setNewFolderName] = useState('');

  const showError = (title, err) => {
    console.error(err);
    setNotice({ type: 'error', title, message: formatApiError(err, title) });
  };

  const showSuccess = (message) => {
    setNotice({ type: 'success', title: 'Done', message });
  };

  const fetchHealth = async () => {
    try {
      const res = await api.get('/health');
      setHealth(res.data);
    } catch (err) {
      console.error(err);
      setHealth({ status: 'error' });
    }
  };

  const fetchJobRuns = async () => {
    try {
      const res = await api.get('/job-history?limit=60');
      setJobRuns(res.data.runs || []);
    } catch (err) {
      console.error(err);
    }
  };

  const latestRunByJob = useMemo(() => {
    const byJob = {};
    jobRuns.forEach(run => {
      if (run.kind === 'data_sync' && run.job_name && !byJob[run.job_name]) {
        byJob[run.job_name] = run;
      }
    });
    return byJob;
  }, [jobRuns]);

  const handleExportRuntimeConfig = async () => {
    setExportingConfig(true);
    try {
      const res = await api.get('/runtime-config/export', { responseType: 'blob' });
      const blobUrl = window.URL.createObjectURL(new Blob([res.data], { type: 'application/zip' }));
      const link = document.createElement('a');
      const disposition = res.headers['content-disposition'] || '';
      const fileNameMatch = disposition.match(/filename="?([^"]+)"?/);
      link.href = blobUrl;
      link.download = fileNameMatch?.[1] || `oci-migrator-runtime-${Date.now()}.zip`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.URL.revokeObjectURL(blobUrl);
      showSuccess('Runtime config export created.');
    } catch (err) {
      showError('Failed to export runtime config', err);
    }
    setExportingConfig(false);
  };

  const handleDownloadRunLog = async (run) => {
    try {
      const res = await api.get(`/job-history/${encodeURIComponent(run.id)}/log/download`, { responseType: 'blob' });
      const blobUrl = window.URL.createObjectURL(new Blob([res.data], { type: 'text/plain' }));
      const link = document.createElement('a');
      const disposition = res.headers['content-disposition'] || '';
      const fileNameMatch = disposition.match(/filename="?([^"]+)"?/);
      const fallbackName = `${run.job_name || run.kind || 'job'}-${run.id}.log`.replace(/[^A-Za-z0-9._-]/g, '_');
      link.href = blobUrl;
      link.download = fileNameMatch?.[1] || fallbackName;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.URL.revokeObjectURL(blobUrl);
    } catch (err) {
      showError('Failed to download job log', err);
    }
  };

  useEffect(() => {
    if (!isAuthenticated) return;
    fetchProfiles();
    fetchRemotes();
    fetchJobs();
    fetchHealth();
    fetchJobRuns();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isAuthenticated, api]);

  useEffect(() => {
    let interval;
    if (activeLogJob) {
      interval = setInterval(async () => {
        try {
          const res = await api.get(`/job-log/${activeLogJob}`);
          setLiveLogData(res.data.log);
          fetchJobRuns();
        } catch {
          setLiveLogData("Error fetching logs or job finished.");
        }
      }, 2000);
    } else {
      setLiveLogData("");
    }
    return () => clearInterval(interval);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeLogJob]);

  useEffect(() => {
    if (!activeRunLogId) {
      setRunLogData("");
      return;
    }

    const fetchRunLog = async () => {
      try {
        const res = await api.get(`/job-history/${encodeURIComponent(activeRunLogId)}/log`);
        setRunLogData(res.data.log || "No log output yet.");
        fetchJobRuns();
      } catch (err) {
        setRunLogData(formatApiError(err, 'Failed to load job log.'));
      }
    };

    fetchRunLog();
    const interval = setInterval(fetchRunLog, 5000);
    return () => clearInterval(interval);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeRunLogId, api]);

  useEffect(() => {
    const taskIds = Object.keys(vmTasks);
    if (taskIds.length === 0) return;

    const interval = setInterval(() => {
      taskIds.forEach(async (taskId) => {
        if (vmTasks[taskId].status !== 'SUCCESS' && vmTasks[taskId].status !== 'FAILURE') {
          try {
            const res = await api.get(`/migration-status/${taskId}`);
            setVmTasks(prev => ({
              ...prev,
              [taskId]: { ...prev[taskId], status: res.data.status, details: res.data.details || res.data.status }
            }));
          } catch (err) {
            console.error(err);
          }
        }
      });
    }, 5000);

    return () => clearInterval(interval);
  }, [vmTasks, api]);

  const fetchProfiles = async () => { try { const res = await api.get(`/list-profiles`); setProfiles(res.data.profiles); } catch (err) { showError('Failed to load OCI profiles', err); } };
  const fetchRemotes = async () => {
    try {
      const res = await api.get(`/list-remotes`);
      setRemotes(res.data.remotes || []);
      setRemoteDetails(res.data.remote_details || []);
    } catch (err) {
      showError('Failed to load rclone remotes', err);
    }
  };
  const fetchJobs = async () => { try { const res = await api.get(`/list-jobs`); setJobs(res.data); } catch (err) { showError('Failed to load jobs', err); } };

  // --- OCI Profile (Saved Profiles) Management ---
  const handleSaveProfile = async () => {
    if (!formData.profileName || !formData.compartmentOcid) {
      setNotice({ type: 'error', title: 'Missing required fields', message: 'Profile Name and Compartment OCID are required.' });
      return;
    }
    const safeProfileName = formData.profileName.toLowerCase().replace(/[^a-z0-9]/g, '_');
    const secureFileName = `${safeProfileName}_api_key.pem`;
    let finalFile;
    if (keyInputMode === 'paste') {
      const blob = new Blob([pastedKey], { type: 'text/plain' });
      finalFile = new File([blob], secureFileName, { type: 'text/plain' });
    } else {
      finalFile = new File([file], secureFileName, { type: 'text/plain' });
    }
    setLoading(true);
    try {
      const fData = new FormData();
      fData.append("file", finalFile);
      const uploadRes = await api.post(`/upload-key`, fData);
      setLastKeySavedPath(uploadRes?.data?.saved_path || '');
      await api.post(`/save-config`, {
        profile_name: formData.profileName,
        user_ocid: formData.userOcid,
        tenancy_ocid: formData.tenancyOcid,
        fingerprint: formData.fingerprint,
        region: formData.region,
        compartment_ocid: formData.compartmentOcid,
        storage_compartment_ocid: formData.storageCompartmentOcid,
        key_file_name: secureFileName
      });
      fetchProfiles();
      showSuccess('OCI profile saved.');
      setFormData({ profileName: '', userOcid: '', tenancyOcid: '', fingerprint: '', region: 'eu-stockholm-1', compartmentOcid: '', storageCompartmentOcid: '' });
      setPastedKey(''); setFile(null); setKeyInputMode('upload');
    } catch (err) { showError('Failed to save OCI profile', err); }
    setLoading(false);
  };

  const handleEditProfile = async (profileName) => {
    try {
      const res = await api.get(`/get-profile/${profileName}`);
      setFormData({
        profileName: res.data.profileName, userOcid: res.data.userOcid, tenancyOcid: res.data.tenancyOcid,
        fingerprint: res.data.fingerprint, region: res.data.region, compartmentOcid: res.data.compartmentOcid,
        storageCompartmentOcid: res.data.storageCompartmentOcid || ''
      });
      setKeyInputMode('paste'); setView('keys');
    } catch (err) { showError('Failed to load profile details', err); }
  };

  const handleDeleteProfile = async (profileName) => {
    if (!window.confirm(`Delete profile: ${profileName}?`)) return;
    try {
      await api.delete(`/delete-profile/${profileName}`);
      fetchProfiles();
    } catch (err) { showError('Failed to delete OCI profile', err); }
  };

  // --- Universal Remote Management ---
  const handleSaveRemote = async () => {
    if (remoteConfig.provider === 'oci') {
        return handleSaveProfile();
    }
    if (!remoteConfig.name) {
      setNotice({ type: 'error', title: 'Missing remote name', message: 'Please provide a name for the remote.' });
      return;
    }
    if (remoteConfig.provider === 'local' && remoteConfig.localMode === 'server_folder' && !remoteConfig.localFolderName) {
      setNotice({ type: 'error', title: 'Missing folder name', message: 'Folder name is required for server local folders.' });
      return;
    }
    if (remoteConfig.provider === 'local' && remoteConfig.localMode === 'mounted_share' && !remoteConfig.localMountPath) {
      setNotice({ type: 'error', title: 'Missing mount path', message: 'Mount path is required for mounted external shares.' });
      return;
    }
    if (remoteConfig.provider === 'local' && remoteConfig.localMode === 'server_folder' && remoteConfig.localShareAccess === 'user') {
      if (!remoteConfig.localShareUsername || !remoteConfig.localSharePassword) {
        setNotice({ type: 'error', title: 'Missing SMB credentials', message: 'SMB username and password are required for user access.' });
        return;
      }
      if (remoteConfig.localSharePassword.length < 8) {
        setNotice({ type: 'error', title: 'Weak SMB password', message: 'SMB password must be at least 8 characters.' });
        return;
      }
    }
    setLoading(true);
    try {
      const rData = new FormData();
      rData.append("name", remoteConfig.name);
      rData.append("provider", remoteConfig.provider);
      if (remoteConfig.provider === 's3') {
        rData.append("access_key", remoteConfig.accessKey);
        rData.append("secret_key", remoteConfig.secretKey);
        rData.append("region", remoteConfig.region);
      } else if (remoteConfig.provider === 'azureblob') {
        rData.append("account_name", remoteConfig.accountName);
        rData.append("account_key", remoteConfig.accountKey);
      } else if (remoteConfig.provider === 'google cloud storage') {
        rData.append("gcp_object_acl", remoteConfig.gcpObjectAcl);
        rData.append("gcp_bucket_acl", remoteConfig.gcpBucketAcl);
        rData.append("gcp_location", remoteConfig.gcpLocation);
        if (gcpKeyFile) rData.append("gcp_file", gcpKeyFile);
      } else if (remoteConfig.provider === 'local') {
        rData.append("local_mode", remoteConfig.localMode);
        rData.append("local_folder_name", remoteConfig.localFolderName);
        rData.append("local_mount_path", remoteConfig.localMountPath);
        rData.append("local_share_access", remoteConfig.localShareAccess);
        rData.append("local_share_name", remoteConfig.localShareName);
        rData.append("local_share_username", remoteConfig.localShareUsername);
        rData.append("local_share_password", remoteConfig.localSharePassword);
      }
      const res = await api.post(`/save-remote`, rData);
      fetchRemotes();
      if (res.data.share) {
        const shareUser = res.data.share.username ? `\nUser: ${res.data.share.username}` : '\nAccess: everyone';
        showSuccess(`Remote saved: ${res.data.local_path}\nSMB: ${res.data.share.unc_path}\nMac: ${res.data.share.smb_url}${shareUser}`);
      } else {
        showSuccess(res.data.local_path ? `Remote saved: ${res.data.local_path}` : 'Remote saved.');
      }
      setRemoteConfig(DEFAULT_REMOTE_CONFIG);
      setGcpKeyFile(null);
    } catch (err) { showError('Failed to save rclone remote', err); }
    setLoading(false);
  };

  const handleDeleteRemote = async (remoteName) => {
    if (!window.confirm(`Delete remote: ${remoteName}?`)) return;
    try {
      await api.delete(`/delete-remote/${remoteName}`);
      fetchRemotes();
    } catch (err) { showError('Failed to delete rclone remote', err); }
  };

  const fetchVms = async (p) => { setLoading(true); try { const res = await api.get(`/list-vms/${p}`); setVms(res.data); setActiveSourceProfile(p); setView('explorer'); } catch (err) { showError('Failed to list VMs', err); } setLoading(false); };

  // --- Job Management ---
  const handleSaveJob = async () => {
    if (!syncJob.name || !syncJob.source_remote || !syncJob.dest_bucket) {
      setNotice({ type: 'error', title: 'Missing job fields', message: 'Job name, source remote, and destination bucket are required.' });
      return;
    }
    setLoading(true);
    try {
      await api.post(`/save-job`, syncJob);
      showSuccess('Job saved.');
      fetchJobs(); setView('datasync');
    } catch (err) { showError('Failed to save job', err); }
    setLoading(false);
  };

  const handleDeleteJob = async (name) => {
    if (!window.confirm("Delete this job?")) return;
    try {
      await api.delete(`/delete-job/${name}`);
      fetchJobs(); if (activeLogJob === name) setActiveLogJob(null);
      showSuccess('Job deleted.');
    } catch (err) {
      showError('Failed to delete job', err);
    }
  };

  const handleRunManual = async (job) => {
    try {
      await api.post(`/start-data-sync-manual`, job);
      setActiveLogJob(job.name);
      fetchJobRuns();
      showSuccess(`Started ${job.name}.`);
    } catch (err) { showError('Failed to start data sync job', err); }
  };

  // --- Storage Explorer ---
  const handleStorageProfileChange = async (p) => { 
      setStorageProfile(p); setSelectedBucket(''); setStorageObjects([]);
      try { const res = await api.get(`/list-buckets/${p}`); setStorageBuckets(res.data); } catch (err) { showError('Failed to list buckets', err); }
  };
  const handleBucketClick = async (b) => { 
      setSelectedBucket(b); try { const res = await api.get(`/list-objects/${storageProfile}/${b}`); setStorageObjects(res.data); } catch (err) { showError('Failed to list bucket objects', err); }
  };
  const handleCreateBucket = async () => {
      if (!newBucketName) return;
      try { await api.post(`/create-bucket`, { profile_name: storageProfile, bucket_name: newBucketName }); setNewBucketName(''); handleStorageProfileChange(storageProfile); showSuccess('Bucket created.'); } catch (err) { showError('Failed to create bucket', err); }
  };
  const handleCreateFolder = async () => {
      if (!newFolderName || !selectedBucket) return;
      try { await api.post(`/create-folder`, { profile_name: storageProfile, bucket_name: selectedBucket, folder_name: newFolderName }); setNewFolderName(''); handleBucketClick(selectedBucket); showSuccess('Folder created.'); } catch (err) { showError('Failed to create folder', err); }
  };
  const handleDeleteObject = async (objectName) => {
      if (!window.confirm(`Delete: ${objectName}?`)) return;
      try { await api.delete(`/delete-object/${storageProfile}/${selectedBucket}/${encodeURIComponent(objectName)}`); handleBucketClick(selectedBucket); showSuccess('Object deleted.'); } catch (err) { showError('Failed to delete object', err); }
  };

  const handleLogin = async (event) => {
    event.preventDefault();
    setLoginError('');
    setAuthLoading(true);
    try {
      const res = await axios.post(`${API_BASE}/auth/login`, {
        username: loginForm.username,
        password: loginForm.password
      });
      localStorage.setItem(SESSION_TOKEN_KEY, res.data.token);
      localStorage.setItem(SESSION_USERNAME_KEY, res.data.username || loginForm.username);
      setAuthState({ token: res.data.token, mode: 'session', username: res.data.username || loginForm.username });
      setLoginForm(prev => ({ ...prev, password: '' }));
    } catch (err) {
      console.error(err);
      setLoginError(formatApiError(err, 'Login failed.'));
    }
    setAuthLoading(false);
  };

  const handleLogout = async () => {
    try {
      if (authState.mode === 'session') {
        await api.post('/auth/logout');
      }
    } catch (err) {
      console.error(err);
    } finally {
      localStorage.removeItem(SESSION_TOKEN_KEY);
      localStorage.removeItem(SESSION_USERNAME_KEY);
      setAuthState({ token: '', mode: '', username: 'admin' });
      setShowPasswordPanel(false);
    }
  };

  const handleChangePassword = async (event) => {
    event.preventDefault();
    setPasswordMessage('');

    if (passwordForm.newPassword.length < 12) {
      setPasswordMessage('New password must be at least 12 characters.');
      return;
    }

    if (passwordForm.newPassword !== passwordForm.confirmPassword) {
      setPasswordMessage('New passwords do not match.');
      return;
    }

    setAuthLoading(true);
    try {
      const res = await api.post('/auth/change-password', {
        current_password: passwordForm.currentPassword,
        new_password: passwordForm.newPassword
      });
      localStorage.setItem(SESSION_TOKEN_KEY, res.data.token);
      localStorage.setItem(SESSION_USERNAME_KEY, res.data.username || authState.username);
      setAuthState({ token: res.data.token, mode: 'session', username: res.data.username || authState.username });
      setPasswordForm({ currentPassword: '', newPassword: '', confirmPassword: '' });
      setPasswordMessage('Password changed.');
    } catch (err) {
      console.error(err);
      setPasswordMessage(formatApiError(err, 'Failed to change password.'));
    }
    setAuthLoading(false);
  };

  const filteredVms = vms.filter(vm => vm.name.toLowerCase().includes(searchTerm.toLowerCase()) || vm.id.includes(searchTerm));

  const getStatusColor = (status) => {
    if (status === 'SUCCESS') return 'text-green-500';
    if (status === 'FAILURE') return 'text-red-500';
    if (status === 'PROGRESS') return 'text-blue-500';
    return 'text-orange-500';
  };

  if (!isAuthenticated) {
    return (
      <div className="min-h-screen bg-gray-50 flex items-center justify-center p-6 font-sans">
        <form onSubmit={handleLogin} className="w-full max-w-sm bg-white border border-gray-200 rounded-md shadow-sm p-6">
          <div className="flex items-center gap-3 mb-6">
            <div className="bg-[#9c3029] p-2 rounded-md"><Lock size={20} className="text-white" /></div>
            <div>
              <h1 className="text-lg font-bold text-gray-900">OCI Migrator Pro</h1>
              <p className="text-xs text-gray-500">Admin login</p>
            </div>
          </div>
          <div className="space-y-4 text-left">
            <div>
              <label className="text-[11px] font-bold text-gray-500 uppercase tracking-wider mb-1 block">Username</label>
              <input
                value={loginForm.username}
                onChange={e => setLoginForm({ ...loginForm, username: e.target.value })}
                className="w-full bg-white border border-gray-200 p-2.5 rounded-md text-sm text-gray-800 focus:outline-none focus:border-[#9c3029]"
                autoComplete="username"
              />
            </div>
            <div>
              <label className="text-[11px] font-bold text-gray-500 uppercase tracking-wider mb-1 block">Password</label>
              <input
                type="password"
                value={loginForm.password}
                onChange={e => setLoginForm({ ...loginForm, password: e.target.value })}
                className="w-full bg-white border border-gray-200 p-2.5 rounded-md text-sm text-gray-800 focus:outline-none focus:border-[#9c3029]"
                autoComplete="current-password"
              />
            </div>
            {loginError && <div className="text-xs text-red-600 bg-red-50 border border-red-100 rounded-md p-2">{loginError}</div>}
            <button type="submit" disabled={authLoading} className="w-full bg-[#9c3029] text-white py-2.5 rounded-md font-semibold hover:bg-[#7a2520] transition-colors shadow-sm flex items-center justify-center gap-2">
              {authLoading ? <Loader2 className="animate-spin" size={18} /> : <><Lock size={16} /> Login</>}
            </button>
          </div>
        </form>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-white text-gray-800 flex overflow-hidden font-sans">
      {/* Sidebar - MJUK SALVIAGRÖN (#e1ebd5) */}
      <nav className="w-64 bg-[#e1ebd5] flex flex-col p-6 z-10 border-r border-[#d1dcca]">
        <div className="flex items-center gap-3 mb-10 px-2">
          <div className="bg-[#9c3029] p-1.5 rounded-md"><Cpu size={20} className="text-white" /></div>
          <h1 className="text-lg font-bold tracking-tight text-gray-900">OCI Migrator Pro</h1>
        </div>
        <div className="space-y-1 font-medium text-sm text-gray-700">
          <button onClick={() => setView('keys')} className={`w-full flex items-center gap-3 p-3 rounded-md transition-colors ${view === 'keys' ? 'bg-[#cddac0] font-semibold text-gray-900' : 'hover:bg-[#d5e2c8]'}`}><Key size={18} /> <span>Credentials</span></button>
          <button onClick={() => setView('datasync')} className={`w-full flex items-center gap-3 p-3 rounded-md transition-colors ${view === 'datasync' ? 'bg-[#cddac0] font-semibold text-gray-900' : 'hover:bg-[#d5e2c8]'}`}><Activity size={18} /> <span>Job Dashboard</span></button>
          <button onClick={() => setView('builder')} className={`w-full flex items-center gap-3 p-3 rounded-md transition-colors ${view === 'builder' ? 'bg-[#cddac0] font-semibold text-gray-900' : 'hover:bg-[#d5e2c8]'}`}><Plus size={18} /> <span>New Sync Job</span></button>
          <button onClick={() => setView('explorer')} className={`w-full flex items-center gap-3 p-3 rounded-md transition-colors ${view === 'explorer' ? 'bg-[#cddac0] font-semibold text-gray-900' : 'hover:bg-[#d5e2c8]'}`}><Database size={18} /> <span>VM Migration</span></button>
          <button onClick={() => setView('storage')} className={`w-full flex items-center gap-3 p-3 rounded-md transition-colors ${view === 'storage' ? 'bg-[#cddac0] font-semibold text-gray-900' : 'hover:bg-[#d5e2c8]'}`}><Archive size={18} /> <span>Storage Explorer</span></button>
        </div>
      </nav>

      <main className="flex-1 flex flex-col relative overflow-y-auto bg-gray-50/50">
        <header className="h-16 flex items-center justify-between px-10 bg-white sticky top-0 z-20 shadow-sm border-b border-gray-100">
          <div className="text-xs font-semibold text-gray-500">Signed in as <span className="text-gray-800">{authState.username}</span></div>
          <div className="flex items-center gap-3">
            {view === 'explorer' && (
              <div className="relative">
                <Search className="absolute left-3 top-2 text-gray-400" size={16} />
                <input className="bg-white border border-gray-200 rounded-md py-1.5 pl-9 pr-4 w-64 text-sm text-gray-800 focus:outline-none focus:border-[#9c3029] focus:ring-1 focus:ring-[#9c3029]" placeholder="Search VMs..." value={searchTerm} onChange={e => setSearchTerm(e.target.value)} />
              </div>
            )}
            <button onClick={fetchHealth} className={`px-2.5 py-2 border rounded-md text-xs font-semibold flex items-center gap-2 ${!health?.status ? 'border-gray-200 text-gray-600 bg-white' : health.status === 'ok' ? 'border-green-200 text-green-700 bg-green-50' : health.status === 'warn' ? 'border-amber-200 text-amber-700 bg-amber-50' : 'border-red-200 text-red-700 bg-red-50'}`} title="Refresh health status">
              <HeartPulse size={15} />
              {health?.status || 'health'}
            </button>
            <button onClick={handleExportRuntimeConfig} disabled={exportingConfig} className="p-2 bg-white border border-gray-200 text-gray-600 rounded-md hover:text-[#9c3029] hover:bg-gray-50 disabled:opacity-60" title="Export runtime config backup">
              {exportingConfig ? <Loader2 className="animate-spin" size={16} /> : <Download size={16} />}
            </button>
            {authState.mode === 'session' && (
              <button onClick={() => setShowPasswordPanel(prev => !prev)} className="p-2 bg-white border border-gray-200 text-gray-600 rounded-md hover:text-[#9c3029] hover:bg-gray-50" title="Change password">
                <Lock size={16} />
              </button>
            )}
            <button onClick={handleLogout} className="p-2 bg-white border border-gray-200 text-gray-600 rounded-md hover:text-[#9c3029] hover:bg-gray-50" title="Logout">
              <LogOut size={16} />
            </button>
          </div>
        </header>

        {notice && (
          <div className="mx-8 mt-4 bg-white border border-gray-200 rounded-md shadow-sm p-4 flex items-start gap-3 text-left">
            <AlertCircle size={18} className={notice.type === 'success' ? 'text-green-600 mt-0.5' : 'text-red-600 mt-0.5'} />
            <div className="flex-1 min-w-0">
              <div className={`text-sm font-bold ${notice.type === 'success' ? 'text-green-700' : 'text-red-700'}`}>{notice.title}</div>
              <pre className="mt-1 text-xs text-gray-600 whitespace-pre-wrap font-sans break-words">{notice.message}</pre>
            </div>
            <button onClick={() => setNotice(null)} className="p-1 text-gray-400 hover:text-gray-700 rounded-md" title="Dismiss">
              <X size={16} />
            </button>
          </div>
        )}

        {showPasswordPanel && (
          <div className="absolute right-10 top-20 z-30 w-80 bg-white border border-gray-200 rounded-md shadow-lg p-5 text-left">
            <h2 className="text-sm font-bold text-gray-800 mb-4 flex items-center gap-2"><Lock size={16} className="text-[#9c3029]" /> Change Password</h2>
            <form onSubmit={handleChangePassword} className="space-y-3">
              <input
                type="password"
                value={passwordForm.currentPassword}
                onChange={e => setPasswordForm({ ...passwordForm, currentPassword: e.target.value })}
                placeholder="Current password"
                className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm focus:outline-none focus:border-[#9c3029]"
                autoComplete="current-password"
              />
              <input
                type="password"
                value={passwordForm.newPassword}
                onChange={e => setPasswordForm({ ...passwordForm, newPassword: e.target.value })}
                placeholder="New password"
                className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm focus:outline-none focus:border-[#9c3029]"
                autoComplete="new-password"
              />
              <input
                type="password"
                value={passwordForm.confirmPassword}
                onChange={e => setPasswordForm({ ...passwordForm, confirmPassword: e.target.value })}
                placeholder="Confirm new password"
                className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm focus:outline-none focus:border-[#9c3029]"
                autoComplete="new-password"
              />
              {passwordMessage && <div className={`text-xs rounded-md p-2 border ${passwordMessage === 'Password changed.' ? 'text-green-700 bg-green-50 border-green-100' : 'text-red-600 bg-red-50 border-red-100'}`}>{passwordMessage}</div>}
              <div className="flex gap-2">
                <button type="submit" disabled={authLoading} className="flex-1 bg-[#9c3029] text-white py-2 rounded-md font-semibold text-sm hover:bg-[#7a2520] flex items-center justify-center gap-2">
                  {authLoading ? <Loader2 className="animate-spin" size={16} /> : 'Save'}
                </button>
                <button type="button" onClick={() => setShowPasswordPanel(false)} className="px-3 bg-white border border-gray-200 text-gray-600 rounded-md text-sm hover:bg-gray-50">Close</button>
              </div>
            </form>
          </div>
        )}

        <div className="p-8 pb-40 min-h-screen">
          {/* VIEW: CREDENTIALS */}
          {view === 'keys' && (
             <div className="max-w-7xl animate-in fade-in">
                <div className="grid grid-cols-1 xl:grid-cols-2 gap-6 mb-8">
                  
                  {/* --- COMBINED ADD REMOTE (OCI + BIG 5) --- */}
                  <div className="bg-white border border-gray-200 rounded-md p-6 shadow-sm">
                    <h2 className="text-lg font-bold mb-5 flex items-center gap-2 text-gray-800"><Plus className="text-[#9c3029]" size={20} /> Add Remote</h2>
                    {lastKeySavedPath && (
                      <div className="mb-4 p-3 rounded-md border border-green-200 bg-green-50 text-left">
                        <div className="flex items-start justify-between gap-3">
                          <div className="text-[11px] font-bold text-green-700 uppercase tracking-wider">Key stored securely</div>
                          <button
                            type="button"
                            onClick={() => setLastKeySavedPath('')}
                            className="text-green-700/70 hover:text-green-900 text-xs font-bold leading-none"
                            aria-label="Dismiss"
                            title="Dismiss"
                          >
                            ×
                          </button>
                        </div>
                        <div className="mt-1 text-[11px] font-mono text-green-900 break-all">{lastKeySavedPath}</div>
                        <div className="mt-1 text-[10px] text-green-700">Permissions are set to 600.</div>
                      </div>
                    )}
                    <div className="space-y-4 text-left">
                      <div>
                        <label className="text-[11px] font-bold text-gray-500 uppercase tracking-wider mb-1 block">Provider</label>
                        <select 
                          value={remoteConfig.provider} 
                          onChange={e => {
                            setRemoteConfig({...remoteConfig, provider: e.target.value});
                            // Reset name when changing OCI vs Others
                            setFormData({...formData, profileName: ''});
                          }} 
                          className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm text-gray-800 focus:outline-none focus:border-[#9c3029]"
                        >
                          <option value="oci">Oracle Cloud Infrastructure (OCI)</option>
                          <option value="s3">AWS S3 (or S3 Clone)</option>
                          <option value="azureblob">Azure Blob Storage</option>
                          <option value="google cloud storage">Google Cloud Storage</option>
                          <option value="local">Local / Mounted Share</option>
                        </select>
                      </div>

                      {/* --- OCI FIELDS --- */}
                      {remoteConfig.provider === 'oci' && (
                        <>
                          <div>
                            <label className="text-[11px] font-bold text-gray-500 uppercase tracking-wider mb-1 block">Profile Name</label>
                            <input value={formData.profileName} onChange={e => setFormData({...formData, profileName: e.target.value})} placeholder="e.g. Prod_Environment" className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm text-gray-800 focus:outline-none focus:border-[#9c3029]" />
                          </div>
                          <div className="grid grid-cols-2 gap-4">
                            <div>
                              <label className="text-[11px] font-bold text-gray-500 uppercase tracking-wider mb-1 block">Compute Compartment</label>
                              <input value={formData.compartmentOcid} onChange={e => setFormData({...formData, compartmentOcid: e.target.value})} placeholder="ocid1.compartment..." className="w-full bg-white border border-gray-200 p-2 rounded-md text-[11px] font-mono text-gray-500 focus:outline-none focus:border-[#9c3029]" />
                            </div>
                            <div>
                              <label className="text-[11px] font-bold text-gray-500 uppercase tracking-wider mb-1 block">Storage Compartment</label>
                              <input value={formData.storageCompartmentOcid} onChange={e => setFormData({...formData, storageCompartmentOcid: e.target.value})} placeholder="ocid1.compartment..." className="w-full bg-white border border-gray-200 p-2 rounded-md text-[11px] font-mono text-gray-500 focus:outline-none focus:border-[#9c3029]" />
                            </div>
                          </div>
                          <div>
                            <label className="text-[11px] font-bold text-gray-500 uppercase tracking-wider mb-1 block">Tenancy OCID</label>
                            <input value={formData.tenancyOcid} onChange={e => setFormData({...formData, tenancyOcid: e.target.value})} placeholder="ocid1.tenancy..." className="w-full bg-white border border-gray-200 p-2 rounded-md text-[11px] font-mono text-gray-500 focus:outline-none focus:border-[#9c3029]" />
                          </div>
                          <div>
                            <label className="text-[11px] font-bold text-gray-500 uppercase tracking-wider mb-1 block">User OCID</label>
                            <input value={formData.userOcid} onChange={e => setFormData({...formData, userOcid: e.target.value})} placeholder="ocid1.user..." className="w-full bg-white border border-gray-200 p-2 rounded-md text-[11px] font-mono text-gray-500 focus:outline-none focus:border-[#9c3029]" />
                          </div>
                          <div className="grid grid-cols-2 gap-4">
                            <div>
                              <label className="text-[11px] font-bold text-gray-500 uppercase tracking-wider mb-1 block">Fingerprint</label>
                              <input value={formData.fingerprint} onChange={e => setFormData({...formData, fingerprint: e.target.value})} placeholder="aa:bb:cc:dd..." className="w-full bg-white border border-gray-200 p-2 rounded-md text-[11px] font-mono text-gray-500 focus:outline-none focus:border-[#9c3029]" />
                            </div>
                            <div>
                              <label className="text-[11px] font-bold text-gray-500 uppercase tracking-wider mb-1 block">Region</label>
                              <input value={formData.region} onChange={e => setFormData({...formData, region: e.target.value})} placeholder="eu-stockholm-1" className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm text-gray-800 focus:outline-none focus:border-[#9c3029]" />
                            </div>
                          </div>
                          <div className="pt-2 border-t border-gray-100">
                            <div className="flex gap-2 mb-2">
                               <button onClick={() => setKeyInputMode('upload')} className={`px-4 py-1.5 text-xs rounded-md font-semibold border ${keyInputMode === 'upload' ? 'bg-[#9c3029] border-[#9c3029] text-white' : 'bg-gray-50 border-gray-200 text-gray-600 hover:bg-gray-100'}`}>Upload API Key</button>
                               <button onClick={() => setKeyInputMode('paste')} className={`px-4 py-1.5 text-xs rounded-md font-semibold border ${keyInputMode === 'paste' ? 'bg-[#9c3029] border-[#9c3029] text-white' : 'bg-gray-50 border-gray-200 text-gray-600 hover:bg-gray-100'}`}>Paste Key</button>
                            </div>
                            {keyInputMode === 'upload' ? (
                               <input type="file" onChange={e => setFile(e.target.files[0])} className="text-xs text-gray-600 file:mr-4 file:py-1 file:px-3 file:rounded file:border-0 file:text-xs file:bg-gray-100 file:text-gray-700" />
                            ) : (
                               <textarea value={pastedKey} onChange={e => setPastedKey(e.target.value)} className="w-full h-20 bg-gray-50 border border-gray-200 p-2 text-[11px] font-mono text-gray-600 rounded-md focus:outline-none focus:border-[#9c3029]" placeholder="-----BEGIN PRIVATE KEY-----" />
                            )}
                          </div>
                        </>
                      )}

                      {/* --- OTHER CLOUD FIELDS --- */}
                      {remoteConfig.provider !== 'oci' && (
                        <>
                          <div>
                            <label className="text-[11px] font-bold text-gray-500 uppercase tracking-wider mb-1 block">Remote Name</label>
                            <input value={remoteConfig.name} onChange={e => setRemoteConfig({...remoteConfig, name: e.target.value})} placeholder="e.g. Backup_Target" className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm text-gray-800 focus:outline-none focus:border-[#9c3029]" />
                          </div>
                          {remoteConfig.provider === 's3' && (
                            <>
                              <div className="grid grid-cols-2 gap-4">
                                <div>
                                  <label className="text-[11px] font-bold text-gray-500 uppercase tracking-wider mb-1 block">Access Key ID</label>
                                  <input value={remoteConfig.accessKey} onChange={e => setRemoteConfig({...remoteConfig, accessKey: e.target.value})} className="w-full bg-white border border-gray-200 p-2 rounded-md text-[11px] font-mono focus:outline-none focus:border-[#9c3029]" />
                                </div>
                                <div>
                                  <label className="text-[11px] font-bold text-gray-500 uppercase tracking-wider mb-1 block">Region</label>
                                  <input value={remoteConfig.region} onChange={e => setRemoteConfig({...remoteConfig, region: e.target.value})} className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm focus:outline-none focus:border-[#9c3029]" />
                                </div>
                              </div>
                              <div>
                                <label className="text-[11px] font-bold text-gray-500 uppercase tracking-wider mb-1 block">Secret Access Key</label>
                                <input type="password" value={remoteConfig.secretKey} onChange={e => setRemoteConfig({...remoteConfig, secretKey: e.target.value})} className="w-full bg-white border border-gray-200 p-2 rounded-md text-[11px] font-mono focus:outline-none focus:border-[#9c3029]" />
                              </div>
                            </>
                          )}
                          {remoteConfig.provider === 'azureblob' && (
                            <div className="grid grid-cols-1 gap-4">
                                <input value={remoteConfig.accountName} onChange={e => setRemoteConfig({...remoteConfig, accountName: e.target.value})} placeholder="Account Name" className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm focus:outline-none focus:border-[#9c3029]" />
                                <input type="password" value={remoteConfig.accountKey} onChange={e => setRemoteConfig({...remoteConfig, accountKey: e.target.value})} placeholder="Account Key" className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm focus:outline-none focus:border-[#9c3029]" />
                            </div>
                          )}
                          {remoteConfig.provider === 'google cloud storage' && (
                            <div className="space-y-3 p-3 bg-gray-50 rounded-md border border-gray-200">
                               <label className="text-[10px] font-bold text-gray-400 uppercase">GCP Advanced Config</label>
                               <input value={remoteConfig.gcpObjectAcl} onChange={e => setRemoteConfig({...remoteConfig, gcpObjectAcl: e.target.value})} placeholder="Object ACL" className="w-full bg-white border border-gray-200 p-2 rounded-md text-xs focus:outline-none focus:border-[#9c3029]" />
                               <input value={remoteConfig.gcpBucketAcl} onChange={e => setRemoteConfig({...remoteConfig, gcpBucketAcl: e.target.value})} placeholder="Bucket ACL" className="w-full bg-white border border-gray-200 p-2 rounded-md text-xs focus:outline-none focus:border-[#9c3029]" />
                               <input value={remoteConfig.gcpLocation} onChange={e => setRemoteConfig({...remoteConfig, gcpLocation: e.target.value})} placeholder="Location" className="w-full bg-white border border-gray-200 p-2 rounded-md text-xs focus:outline-none focus:border-[#9c3029]" />
                               <input type="file" accept=".json" onChange={e => setGcpKeyFile(e.target.files[0])} className="text-xs" />
                            </div>
                          )}
                          {remoteConfig.provider === 'local' && (
                            <div className="space-y-3 p-3 bg-gray-50 rounded-md border border-gray-200">
                              <div>
                                <label className="text-[11px] font-bold text-gray-500 uppercase tracking-wider mb-1 block">Local Type</label>
                                <select value={remoteConfig.localMode} onChange={e => setRemoteConfig({...remoteConfig, localMode: e.target.value, localShareAccess: e.target.value === 'server_folder' ? remoteConfig.localShareAccess : 'none'})} className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm focus:outline-none focus:border-[#9c3029]">
                                  <option value="server_folder">Server Local Folder</option>
                                  <option value="mounted_share">Mounted External Share</option>
                                </select>
                              </div>
                              {remoteConfig.localMode === 'server_folder' && (
                                <div className="space-y-3">
                                  <div>
                                    <label className="text-[11px] font-bold text-gray-500 uppercase tracking-wider mb-1 block">Folder Name</label>
                                    <input value={remoteConfig.localFolderName} onChange={e => setRemoteConfig({...remoteConfig, localFolderName: e.target.value})} placeholder="customer-a" className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm focus:outline-none focus:border-[#9c3029]" />
                                  </div>
                                  <div>
                                    <label className="text-[11px] font-bold text-gray-500 uppercase tracking-wider mb-1 block">SMB Share</label>
                                    <select value={remoteConfig.localShareAccess} onChange={e => setRemoteConfig({...remoteConfig, localShareAccess: e.target.value})} className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm focus:outline-none focus:border-[#9c3029]">
                                      <option value="none">Do Not Share</option>
                                      <option value="everyone">Share to Everyone</option>
                                      <option value="user">Share to User</option>
                                    </select>
                                  </div>
                                  {remoteConfig.localShareAccess !== 'none' && (
                                    <div>
                                      <label className="text-[11px] font-bold text-gray-500 uppercase tracking-wider mb-1 block">Share Name</label>
                                      <input value={remoteConfig.localShareName} onChange={e => setRemoteConfig({...remoteConfig, localShareName: e.target.value})} placeholder={remoteConfig.localFolderName || remoteConfig.name || 'customer-a'} className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm focus:outline-none focus:border-[#9c3029]" />
                                      <div className="mt-1 text-[10px] text-gray-500">TCP 445 opens when the share is created.</div>
                                    </div>
                                  )}
                                  {remoteConfig.localShareAccess === 'user' && (
                                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                                      <div>
                                        <label className="text-[11px] font-bold text-gray-500 uppercase tracking-wider mb-1 block">SMB User</label>
                                        <input value={remoteConfig.localShareUsername} onChange={e => setRemoteConfig({...remoteConfig, localShareUsername: e.target.value})} placeholder="migratoruser" className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm focus:outline-none focus:border-[#9c3029]" />
                                      </div>
                                      <div>
                                        <label className="text-[11px] font-bold text-gray-500 uppercase tracking-wider mb-1 block">SMB Password</label>
                                        <input type="password" value={remoteConfig.localSharePassword} onChange={e => setRemoteConfig({...remoteConfig, localSharePassword: e.target.value})} className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm focus:outline-none focus:border-[#9c3029]" autoComplete="new-password" />
                                      </div>
                                    </div>
                                  )}
                                </div>
                              )}
                              {remoteConfig.localMode === 'mounted_share' && (
                                <div>
                                  <label className="text-[11px] font-bold text-gray-500 uppercase tracking-wider mb-1 block">Mount Path</label>
                                  <input value={remoteConfig.localMountPath} onChange={e => setRemoteConfig({...remoteConfig, localMountPath: e.target.value})} placeholder="/mnt/customer-share" className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm font-mono focus:outline-none focus:border-[#9c3029]" />
                                </div>
                              )}
                            </div>
                          )}
                        </>
                      )}
                      
                      <button onClick={handleSaveRemote} disabled={loading} className="w-full bg-[#9c3029] text-white py-2.5 rounded-md font-semibold hover:bg-[#7a2520] transition-colors shadow-sm">
                         {loading ? <Loader2 className="animate-spin mx-auto" /> : (remoteConfig.provider === 'oci' ? "Save Profile" : "Add Remote")}
                      </button>
                    </div>
                  </div>

                  {/* --- RIGHT COLUMN: SAVED PROFILES (OCI + Remotes) --- */}
                  <div className="bg-white border border-gray-200 rounded-md p-6 shadow-sm">
                     <h2 className="text-lg font-bold mb-5 flex items-center gap-2 text-gray-800"><Database className="text-[#9c3029]" size={20} /> Saved Profiles</h2>
                     <div className="space-y-3 max-h-[600px] overflow-y-auto pr-2">
                        {profiles.map(p => (
                          <div key={p} className="p-4 bg-white border border-gray-200 rounded-md flex justify-between items-center hover:shadow-md transition-shadow">
                             <span className="font-semibold text-gray-800">{p}</span>
                             <div className="flex gap-2 items-center">
                               <button onClick={() => handleEditProfile(p)} className="p-1.5 text-white bg-[#9c3029] hover:bg-[#7a2520] rounded-md transition-colors"><Edit size={14}/></button>
                               <button onClick={() => handleDeleteProfile(p)} className="p-1.5 text-gray-500 border border-gray-200 hover:text-[#9c3029] hover:bg-red-50 rounded-md transition-colors"><Trash2 size={14}/></button>
                               <button onClick={() => fetchVms(p)} className="text-xs bg-white text-gray-700 border border-gray-300 font-semibold px-3 py-1.5 rounded-md hover:bg-gray-50 transition-colors ml-2">Scan VMs</button>
                             </div>
                          </div>
                        ))}
                        {profiles.length === 0 && <p className="text-gray-500 italic text-sm">No profiles found.</p>}
                        
                        {localRemotes.length > 0 && (
                          <div className="pt-4 border-t border-gray-100 mt-4">
                            <label className="text-[10px] font-bold text-gray-400 uppercase mb-2 block">Local Sources</label>
                            {localRemotes.map(remote => (
                               <div key={remote.name} className="p-3 bg-gray-50 border border-gray-200 rounded-md flex justify-between items-center mb-2">
                                  <div className="min-w-0">
                                    <span className="text-sm text-gray-600 font-medium">{remote.name}</span>
                                    {remote.share_name && <div className="text-[10px] text-gray-400 truncate">SMB: {remote.share_name}</div>}
                                  </div>
                                  <button onClick={() => handleDeleteRemote(remote.name)} className="p-1.5 text-gray-400 hover:text-[#9c3029]"><Trash2 size={14}/></button>
                               </div>
                            ))}
                          </div>
                        )}

                        {externalRemotes.length > 0 && (
                          <div className="pt-4 border-t border-gray-100 mt-4">
                            <label className="text-[10px] font-bold text-gray-400 uppercase mb-2 block">External Remotes</label>
                            {externalRemotes.map(remote => (
                               <div key={remote.name} className="p-3 bg-gray-50 border border-gray-200 rounded-md flex justify-between items-center mb-2">
                                  <span className="text-sm text-gray-600 font-medium">{remote.name}</span>
                                  <button onClick={() => handleDeleteRemote(remote.name)} className="p-1.5 text-gray-400 hover:text-[#9c3029]"><Trash2 size={14}/></button>
                               </div>
                            ))}
                          </div>
                        )}
                     </div>
                  </div>
                </div>
             </div>
          )}

          {/* VIEW: JOB DASHBOARD */}
          {view === 'datasync' && (
            <div className="max-w-5xl mx-auto space-y-6 animate-in fade-in">
              <h2 className="text-xl font-bold mb-6 flex items-center gap-2 text-gray-800"><Activity size={24} className="text-[#9c3029]"/> Active Sync Jobs</h2>
              <div className="grid grid-cols-1 gap-4">
                {jobs.map(job => (
                  <div key={job.name} className="flex flex-col gap-2">
                    <div className="bg-white border border-gray-200 p-5 rounded-md flex items-center justify-between shadow-sm">
                      <div className="flex items-center gap-5 text-left">
                        <div className="bg-gray-50 border border-gray-100 p-2.5 rounded-md"><RefreshCw className="text-[#9c3029]" size={20} /></div>
                        <div>
                          <h3 className="font-bold text-md text-gray-800">{job.name}</h3>
                          <div className="flex items-center gap-3 text-[11px] text-gray-500 font-mono mt-1">
                            <span>{job.source_remote}</span>
                            <ArrowRight size={10} className="text-gray-400" />
                            <span className="text-gray-700 font-semibold">{job.dest_bucket}</span>
                          </div>
                          <div className="mt-2 text-[10px] text-gray-500 uppercase font-bold tracking-wider flex gap-4">
                            <span className="flex items-center gap-1"><Clock size={12}/> {job.schedule.frequency} @ {job.schedule.time}</span>
                            <span className="flex items-center gap-1"><Cpu size={12}/> {job.transfers} Transfers</span>
                          </div>
                          {latestRunByJob[job.name] && (
                            <div className="mt-2 flex items-center gap-2">
                              <span className={`text-[10px] px-2 py-0.5 rounded-full border font-bold uppercase ${runStatusClass(latestRunByJob[job.name].status)}`}>
                                {latestRunByJob[job.name].status}
                              </span>
                              <span className="text-[11px] text-gray-500 max-w-md truncate inline-block align-bottom">{latestRunByJob[job.name].error || latestRunByJob[job.name].details || formatTimestamp(latestRunByJob[job.name].updated_at)}</span>
                            </div>
                          )}
                        </div>
                      </div>
                      <div className="flex gap-2">
                        <button onClick={() => activeLogJob === job.name ? setActiveLogJob(null) : setActiveLogJob(job.name)} className={`p-2 rounded-md transition-colors ${activeLogJob === job.name ? 'bg-gray-100 text-gray-800' : 'bg-white border border-gray-200 text-gray-500 hover:bg-gray-50'}`}><Terminal size={16}/></button>
                        <button onClick={() => handleRunManual(job)} className="px-4 py-2 bg-[#9c3029] text-white rounded-md font-semibold text-sm shadow-sm hover:bg-[#a63d2e]">Run</button>
                        <button onClick={() => handleDeleteJob(job.name)} className="p-2 bg-white border border-gray-200 text-gray-500 rounded-md hover:text-[#9c3029]"><Trash2 size={16}/></button>
                      </div>
                    </div>
                    {activeLogJob === job.name && (
                      <div className="bg-gray-900 border border-gray-800 rounded-md p-4 relative animate-in slide-in-from-top-2 shadow-inner">
                        <pre className="text-[11px] font-mono text-gray-300 h-32 overflow-y-auto text-left">{liveLogData || "Awaiting process..."}</pre>
                      </div>
                    )}
                  </div>
                ))}
                {jobs.length === 0 && <div className="text-center p-12 bg-white border border-gray-200 rounded-md text-gray-500 shadow-sm">No jobs saved.</div>}
              </div>
              <div className="bg-white border border-gray-200 rounded-md shadow-sm overflow-hidden">
                <div className="flex items-center justify-between p-4 border-b border-gray-100">
                  <h3 className="font-bold text-sm text-gray-800 flex items-center gap-2"><Clock size={16} className="text-[#9c3029]" /> Recent Runs</h3>
                  <button onClick={fetchJobRuns} className="p-1.5 border border-gray-200 rounded-md text-gray-500 hover:text-[#9c3029] hover:bg-gray-50" title="Refresh job history">
                    <RefreshCw size={14} />
                  </button>
                </div>
                <div className="divide-y divide-gray-100">
                  {jobRuns.slice(0, 12).map(run => {
                    const isDataSyncRun = run.kind === 'data_sync';
                    return (
                      <div key={run.id}>
                        <div className="p-4 flex items-start justify-between gap-4 text-left">
                          <div className="min-w-0">
                            <div className="flex items-center gap-2">
                              <span className={`text-[10px] px-2 py-0.5 rounded-full border font-bold uppercase ${runStatusClass(run.status)}`}>{run.status}</span>
                              <span className="font-semibold text-sm text-gray-800 truncate">{run.job_name || run.kind}</span>
                              <span className="text-[11px] text-gray-400 uppercase">{run.trigger || 'manual'}</span>
                            </div>
                            <div className="mt-1 text-xs text-gray-500 truncate">{run.error || run.details || `${run.source || ''} -> ${run.destination || ''}`}</div>
                            <div className="mt-1 text-[10px] text-gray-400 font-mono truncate">{run.id}</div>
                          </div>
                          <div className="flex items-center gap-2 shrink-0">
                            {isDataSyncRun && (
                              <>
                                <button
                                  type="button"
                                  onClick={() => setActiveRunLogId(activeRunLogId === run.id ? null : run.id)}
                                  className={`p-1.5 border rounded-md ${activeRunLogId === run.id ? 'bg-gray-100 text-gray-800 border-gray-300' : 'text-gray-500 border-gray-200 hover:text-[#9c3029] hover:bg-gray-50'}`}
                                  title="View job log"
                                >
                                  <Terminal size={14} />
                                </button>
                                <button
                                  type="button"
                                  onClick={() => handleDownloadRunLog(run)}
                                  className="p-1.5 border border-gray-200 rounded-md text-gray-500 hover:text-[#9c3029] hover:bg-gray-50"
                                  title="Download job log"
                                >
                                  <Download size={14} />
                                </button>
                              </>
                            )}
                            <div className="text-[11px] text-gray-500 whitespace-nowrap">{formatTimestamp(run.updated_at || run.created_at)}</div>
                          </div>
                        </div>
                        {activeRunLogId === run.id && (
                          <div className="px-4 pb-4">
                            <div className="bg-gray-900 border border-gray-800 rounded-md p-4 shadow-inner">
                              <pre className="text-[11px] font-mono text-gray-300 h-44 overflow-y-auto text-left whitespace-pre-wrap">{runLogData || "Loading log..."}</pre>
                            </div>
                          </div>
                        )}
                      </div>
                    );
                  })}
                  {jobRuns.length === 0 && <div className="p-8 text-center text-sm text-gray-500">No run history yet.</div>}
                </div>
              </div>
            </div>
          )}

          {/* VIEW: JOB BUILDER */}
          {view === 'builder' && (
            <div className="max-w-3xl mx-auto animate-in slide-in-from-bottom-4">
              <div className="bg-white border border-gray-200 rounded-md p-8 shadow-sm text-left">
                <h2 className="text-xl font-bold mb-8 flex items-center gap-2 text-gray-800"><Plus className="text-[#9c3029]"/> Build Sync Pipeline</h2>
                <div className="grid grid-cols-2 gap-6 mb-6">
                  <div className="space-y-1">
                    <label className="text-[11px] uppercase font-bold text-gray-500">Job Name</label>
                    <input value={syncJob.name} onChange={e => setSyncJob({...syncJob, name: e.target.value})} placeholder="e.g. Daily_Web_Sync" className="w-full bg-white border border-gray-200 p-2.5 rounded-md text-sm focus:outline-none focus:border-[#9c3029]" />
                  </div>
                  <div className="space-y-1">
                    <label className="text-[11px] uppercase font-bold text-gray-500">Sync Mode</label>
                    <select value={syncJob.sync_mode} onChange={e => setSyncJob({...syncJob, sync_mode: e.target.value})} className="w-full bg-white border border-gray-200 p-2.5 rounded-md text-sm focus:outline-none focus:border-[#9c3029]">
                      <option value="copy">COPY (Safe)</option>
                      <option value="sync">SYNC (Mirror)</option>
                    </select>
                  </div>
                </div>
                <div className="grid grid-cols-2 gap-6 mb-6 bg-gray-50 p-6 rounded-md border border-gray-200">
                  <div className="space-y-3 text-left">
                    <h4 className="text-sm font-bold text-gray-800 flex items-center gap-2"><Globe size={16}/> Source</h4>
                    <select onChange={async e => {
                      const r = e.target.value;
                      setSyncJob(prev => ({...prev, source_remote: r}));
                      try {
                        const res = await api.get(`/list-remote-buckets/${r}`);
                        setSourceBuckets((res.data.buckets || []).map(item => (
                          typeof item === 'string' ? { name: item, value: item } : item
                        )));
                      } catch (err) {
                        showError('Failed to list buckets for remote', err);
                        setSourceBuckets([]);
                      }
                    }} className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm focus:outline-none focus:border-[#9c3029]">
                      <option value="">Select Remote...</option>
                      {remotes.map(r => <option key={r} value={r}>{r}</option>)}
                    </select>
                    <select onChange={e => {
                      const bucket = e.target.value;
                      setSyncJob(prev => {
                        const remoteName = (prev.source_remote || '').split(':')[0];
                        return {...prev, source_remote: remoteName && bucket ? `${remoteName}:${bucket}` : remoteName};
                      });
                    }} className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm focus:outline-none focus:border-[#9c3029]" disabled={!sourceBuckets.length}>
                      <option value="">Select Bucket/Folder...</option>
                      {sourceBuckets.map(b => <option key={b.value} value={b.value}>{b.name}</option>)}
                    </select>
                  </div>
                  <div className="space-y-3 text-left">
                    <h4 className="text-sm font-bold text-gray-800 flex items-center gap-2"><Shield size={16}/> Destination</h4>
                    <select onChange={async e => {
                      const profile = e.target.value;
                      setSyncJob(prev => ({...prev, dest_profile: profile}));
                      try {
                        const res = await api.get(`/list-buckets/${profile}`);
                        setDestBuckets(res.data);
                      } catch (err) {
                        showError('Failed to list buckets for destination profile', err);
                        setDestBuckets([]);
                      }
                    }} className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm focus:outline-none focus:border-[#9c3029]">
                      <option value="">Select OCI Profile...</option>
                      {profiles.map(p => <option key={p} value={p}>{p}</option>)}
                    </select>
                    <select onChange={e => setSyncJob({...syncJob, dest_bucket: e.target.value})} className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm focus:outline-none focus:border-[#9c3029]" disabled={!destBuckets.length}>
                      <option value="">Select Target Bucket...</option>
                      {destBuckets.map(b => <option key={b.name} value={b.name}>{b.name}</option>)}
                    </select>
                  </div>
                </div>
                
                {/* SCHEMALÄGGNING & OPTIMERING (Transfers, Checkers, Buffer, Time) */}
                <div className="grid grid-cols-3 gap-4 mb-6 text-left">
                  <div className="space-y-1">
                    <label className="text-[11px] uppercase font-bold text-gray-500">Transfers</label>
                    <input type="number" value={syncJob.transfers} onChange={e => setSyncJob({...syncJob, transfers: parseInt(e.target.value)})} className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm font-mono focus:outline-none focus:border-[#9c3029]" />
                  </div>
                  <div className="space-y-1">
                    <label className="text-[11px] uppercase font-bold text-gray-500">Checkers</label>
                    <input type="number" value={syncJob.checkers} onChange={e => setSyncJob({...syncJob, checkers: parseInt(e.target.value)})} className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm font-mono focus:outline-none focus:border-[#9c3029]" />
                  </div>
                  <div className="space-y-1">
                    <label className="text-[11px] uppercase font-bold text-gray-500">Buffer Size</label>
                    <select value={syncJob.buffer_size} onChange={e => setSyncJob({...syncJob, buffer_size: e.target.value})} className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm font-mono focus:outline-none focus:border-[#9c3029]">
                      <option value="16M">16M</option><option value="128M">128M</option><option value="512M">512M</option>
                    </select>
                  </div>
                </div>

                <div className="p-4 bg-gray-50 rounded-md border border-gray-200 mb-6 grid grid-cols-3 gap-4 text-left">
                  <div className="space-y-1">
                    <label className="text-[11px] uppercase font-bold text-gray-500">Frequency</label>
                    <select value={syncJob.schedule.frequency} onChange={e => setSyncJob({...syncJob, schedule: {...syncJob.schedule, frequency: e.target.value}})} className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm focus:outline-none focus:border-[#9c3029]">
                      <option value="none">Manual Only</option><option value="daily">Daily</option><option value="weekly">Weekly</option>
                    </select>
                  </div>
                  <div className="space-y-1">
                    <label className="text-[11px] uppercase font-bold text-gray-500">Time</label>
                    <input type="time" value={syncJob.schedule.time} onChange={e => setSyncJob({...syncJob, schedule: {...syncJob.schedule, time: e.target.value}})} className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm focus:outline-none focus:border-[#9c3029]" />
                  </div>
                  {syncJob.schedule.frequency === 'weekly' && (
                    <div className="space-y-1">
                      <label className="text-[11px] uppercase font-bold text-gray-500">Day</label>
                      <select value={syncJob.schedule.day_of_week} onChange={e => setSyncJob({...syncJob, schedule: {...syncJob.schedule, day_of_week: e.target.value}})} className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm focus:outline-none focus:border-[#9c3029]">
                        <option value="monday">Monday</option><option value="sunday">Sunday</option>
                      </select>
                    </div>
                  )}
                </div>

                <button onClick={handleSaveJob} disabled={loading} className="w-full bg-[#9c3029] text-white py-3 rounded-md font-semibold hover:bg-[#a63d2e] transition-colors flex items-center justify-center gap-2 shadow-sm">
                  {loading ? <Loader2 className="animate-spin" /> : <><CheckCircle size={18}/> Save Pipeline</>}
                </button>
              </div>
            </div>
          )}

          {/* VIEW: VM EXPLORER */}
          {view === 'explorer' && (
             <div className="space-y-6 animate-in slide-in-from-right-4 max-w-7xl mx-auto">
                {activeSourceProfile ? (
                  <>
                    <div className="bg-white p-4 rounded-md border border-gray-200 shadow-sm flex items-center justify-between">
                      <h2 className="text-md font-bold text-gray-800">Source: <span className="text-[#9c3029]">{activeSourceProfile}</span></h2>
                      <span className="text-xs font-semibold text-gray-500 bg-gray-100 px-3 py-1 rounded-full">{vms.length} VMs</span>
                    </div>
                    {loading ? ( <div className="flex justify-center p-20"><Loader2 className="animate-spin text-gray-400" size={40} /></div>
                    ) : (
                      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                        {filteredVms.map(vm => {
                          const taskData = Object.values(vmTasks).find(t => t.vm_id === vm.id);
                          const isMigrating = !!taskData && taskData.status !== 'SUCCESS' && taskData.status !== 'FAILURE';
                          return (
                            <div key={vm.id} onClick={() => { if (!isMigrating) setSelectedVms(prev => prev.includes(vm.id) ? prev.filter(i => i !== vm.id) : [...prev, vm.id]); }}
                                  className={`p-5 rounded-md border transition-all relative ${selectedVms.includes(vm.id) ? 'border-[#9c3029] bg-red-50' : 'border-gray-200 bg-white hover:shadow-md'} ${isMigrating ? 'opacity-70 cursor-not-allowed' : 'cursor-pointer'}`}>
                                <div className="flex items-start gap-4">
                                  <Cloud className={`mt-1 ${selectedVms.includes(vm.id) ? 'text-[#9c3029]' : 'text-gray-400'}`} size={24}/>
                                  <div className="overflow-hidden">
                                    <h4 className="font-bold text-gray-800 truncate text-sm">{vm.name}</h4>
                                    <p className="text-[11px] text-gray-500 font-mono mt-1 truncate">{vm.id}</p>
                                  </div>
                                </div>
                                {taskData && (
                                  <div className={`mt-4 pt-3 border-t border-gray-100 text-[10px] uppercase font-bold tracking-widest ${getStatusColor(taskData.status)}`}>
                                    {isMigrating && <Loader2 size={12} className="inline animate-spin mr-1"/>}
                                    {taskData.details}
                                  </div>
                                )}
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </>
                ) : (
                  <div className="flex flex-col items-center justify-center h-64 bg-white border border-gray-200 rounded-md text-gray-500">
                    <Database size={32} className="mb-3 text-gray-300"/>
                    <p className="text-sm">No profile selected. Scan VMs from Credentials.</p>
                  </div>
                )}
             </div>
          )}

          {/* VIEW: STORAGE EXPLORER */}
          {view === 'storage' && (
             <div className="max-w-6xl mx-auto space-y-6 animate-in fade-in">
                <div className="bg-white p-1 rounded-md border border-gray-200 shadow-sm">
                  <select value={storageProfile} onChange={e => handleStorageProfileChange(e.target.value)} className="w-full bg-transparent p-2 text-gray-800 outline-none font-semibold text-sm">
                     <option value="">Select Profile to Explore...</option>
                     {profiles.map(p => <option key={p} value={p}>{p}</option>)}
                  </select>
                </div>
                {storageProfile && (
                   <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
                      <div className="bg-white border border-gray-200 rounded-md p-5 h-[600px] flex flex-col text-left shadow-sm">
                         <h3 className="font-bold text-gray-800 mb-4 flex items-center gap-2 text-sm"><Database size={16} className="text-[#9c3029]"/> Buckets</h3>
                         <div className="flex gap-2 mb-4">
                             <input value={newBucketName} onChange={e => setNewBucketName(e.target.value)} placeholder="New bucket name" className="w-full bg-gray-50 border border-gray-200 p-2 rounded-md text-xs focus:outline-none focus:border-[#9c3029]" />
                             <button onClick={handleCreateBucket} className="bg-[#9c3029] text-white px-3 py-2 rounded-md hover:bg-[#a63d2e]"><Plus size={14}/></button>
                         </div>
                         <div className="flex-1 overflow-y-auto pr-1">
                             {storageBuckets.map(b => (
                               <div key={b.name} onClick={() => handleBucketClick(b.name)} className={`p-2.5 rounded-md cursor-pointer transition-colors text-sm border ${selectedBucket === b.name ? 'bg-red-50 border-red-200 text-[#9c3029]' : 'hover:bg-gray-50 border-transparent text-gray-600'}`}>{b.name}</div>
                             ))}
                         </div>
                      </div>
                      <div className="col-span-2 bg-white border border-gray-200 rounded-md p-0 h-[600px] flex flex-col shadow-sm overflow-hidden text-left">
                         <div className="flex justify-between items-center p-4 border-b border-gray-200 bg-gray-50">
                             <h3 className="font-bold text-gray-800 flex items-center gap-2 text-sm"><Folder size={16} className={selectedBucket ? "text-[#9c3029]" : "text-gray-400"}/> {selectedBucket || 'Select a bucket'}</h3>
                             {selectedBucket && (
                               <div className="flex gap-2">
                                   <input value={newFolderName} onChange={e => setNewFolderName(e.target.value)} placeholder="New Folder..." className="bg-white border border-gray-200 p-1.5 rounded-md text-xs w-40 focus:outline-none focus:border-[#9c3029]" />
                                   <button onClick={handleCreateFolder} className="bg-white border border-gray-200 text-[#9c3029] px-3 rounded-md text-xs font-bold shadow-sm hover:bg-gray-50">Folder</button>
                               </div>
                             )}
                         </div>
                         <div className="flex-1 overflow-y-auto">
                             <table className="w-full text-left text-sm">
                                <thead className="text-gray-500 text-[10px] uppercase font-bold sticky top-0 bg-white border-b border-gray-200">
                                  <tr><th className="py-2.5 px-5">Name</th><th className="py-2.5 px-5 text-right">Size</th><th className="py-2.5 px-5 text-center w-16">Action</th></tr>
                                </thead>
                                <tbody className="divide-y divide-gray-100 text-sm text-gray-800">
                                  {storageObjects.map(obj => (
                                    <tr key={obj.name} className="hover:bg-gray-50 group transition-colors">
                                      <td className="py-3 px-5 flex items-center gap-3 font-medium text-xs">
                                        {obj.name.endsWith('/') ? <Folder size={14} className="text-[#9c3029]"/> : <FileText size={14} className="text-gray-400"/>}
                                        {obj.name}
                                      </td>
                                      <td className="py-3 px-5 text-right text-gray-500 font-mono text-[11px]">{obj.name.endsWith('/') ? '--' : `${Math.round(obj.size/1024)} KB`}</td>
                                      <td className="py-3 px-5 text-center">
                                        <button onClick={() => handleDeleteObject(obj.name)} className="text-gray-400 hover:text-[#9c3029] transition-all opacity-0 group-hover:opacity-100"><Trash2 size={14} /></button>
                                      </td>
                                    </tr>
                                  ))}
                                </tbody>
                             </table>
                         </div>
                      </div>
                   </div>
                )}
             </div>
          )}
        </div>

        {/* Bulk Migration Drawer */}
        {selectedVms.length > 0 && view === 'explorer' && (
          <div className="absolute bottom-0 left-0 w-full bg-white border-t border-gray-200 p-4 flex flex-col items-center shadow-[0_-4px_6px_-1px_rgba(0,0,0,0.05)] z-50 animate-in slide-in-from-bottom-full text-left">
             <div className="w-full max-w-5xl flex justify-between items-center gap-6">
                <div className="flex-1">
                   <label className="text-[11px] font-bold text-gray-500 uppercase mb-1 block">Dest Profile</label>
                   <select value={vmMigrationConfig.destProfile} onChange={e => {
                      setVmMigrationConfig({...vmMigrationConfig, destProfile: e.target.value});
                      api.get(`/list-buckets/${e.target.value}`).then(res => setDestBuckets(res.data));
                   }} className="w-full bg-white border border-gray-200 p-2.5 rounded-md text-sm focus:outline-none focus:border-[#9c3029]">
                      <option value="">Select Target...</option>
                      {profiles.map(p => <option key={p} value={p}>{p}</option>)}
                   </select>
                </div>
                <div className="flex-1">
                   <label className="text-[11px] font-bold text-gray-500 uppercase mb-1 block">Storage Bucket</label>
                   <select value={vmMigrationConfig.destBucket} onChange={e => setVmMigrationConfig({...vmMigrationConfig, destBucket: e.target.value})} className="w-full bg-white border border-gray-200 p-2.5 rounded-md text-sm focus:outline-none focus:border-[#9c3029]">
                      <option value="">Select Bucket...</option>
                      {destBuckets.map(b => <option key={b.name} value={b.name}>{b.name}</option>)}
                   </select>
                </div>
                <div className="pt-5">
                  <button onClick={async () => {
                    if (!vmMigrationConfig.destProfile || !vmMigrationConfig.destBucket) {
                      setNotice({ type: 'error', title: 'Missing migration target', message: 'Select destination profile and bucket.' });
                      return;
                    }
                    try {
                      const res = await api.post(`/start-bulk-migration`, {
                        vm_ids: selectedVms, source_profile: activeSourceProfile, dest_profile: vmMigrationConfig.destProfile, bucket_name: vmMigrationConfig.destBucket
                      });
                      const newTasks = {}; res.data.tasks.forEach(t => { newTasks[t.task_id] = { vm_id: t.vm_id, status: 'PENDING', details: 'Starting...' }; });
                      setVmTasks(prev => ({ ...prev, ...newTasks })); setSelectedVms([]); 
                      fetchJobRuns();
                      showSuccess('VM migration queued.');
                    } catch (err) { showError('Failed to start VM migration', err); }
                  }} className="bg-[#9c3029] text-white px-6 py-2.5 rounded-md font-semibold text-sm flex items-center gap-2 hover:bg-[#a63d2e] transition-colors shadow-sm">Execute Migration <ArrowRight size={16} /></button>
                </div>
             </div>
          </div>
        )}
      </main>
    </div>
  );
}
