import { useMemo, useRef, useState, useEffect } from 'react';
import axios from 'axios';
import loginHeroImage from './assets/oci-migrator-pro-hero.png';
import { 
  Cloud, Shield, Database, Search, Key, Loader2, CheckCircle,
  ArrowRight, FileText, Archive, Edit, Trash2,
  Folder, Plus, RefreshCw, Globe, Cpu, Clock, Activity, Terminal,
  Lock, LogOut, Download, Upload, HeartPulse, AlertCircle, X, Settings, Save, Tags, HardDrive, Moon, Network,
  LayoutDashboard, Menu, Play, Sun, TrendingUp, TrendingDown, Minus
} from 'lucide-react';

const API_BASE = import.meta.env.VITE_API_BASE || (
  window.location.port === '5173'
    ? `http://${window.location.hostname}:8000`
    : window.location.origin
);
const SESSION_TOKEN_KEY = 'OCI_MIGRATOR_SESSION_TOKEN';
const SESSION_USERNAME_KEY = 'OCI_MIGRATOR_SESSION_USERNAME';
const THEME_KEY = 'OCI_MIGRATOR_THEME';
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
  localSharePassword: '',
  localNfsEnabled: false,
  localNfsClients: ''
};
const DEFAULT_LOCAL_RETENTION = {
  enabled: false,
  delete_after_days: 30,
  min_file_age_hours: 24
};
const createDefaultSyncJob = () => ({
  name: '',
  source_remote: '',
  dest_profile: '',
  dest_bucket: '',
  sync_mode: 'copy',
  transfers: 16,
  checkers: 32,
  buffer_size: '128M',
  bwlimit: '',
  tpslimit: '',
  metadata_tags: [],
  local_retention: { ...DEFAULT_LOCAL_RETENTION },
  schedule: { frequency: 'none', time: '02:00', day_of_week: 'monday', day_of_month: '1' }
});
const createDefaultLifecyclePolicy = () => ({
  enabled: false,
  prefix: '',
  filters: [],
  rules: [],
  infrequent_access_after_days: null,
  archive_after_days: null,
  delete_after_days: null,
  previous_versions_delete_after_days: null
});
const createLifecycleFilter = () => ({ type: 'include_prefix', value: '' });
const createLifecycleRule = (action = 'ARCHIVE') => ({
  name: `lifecycle-rule-${Date.now().toString(36)}`,
  target: 'objects',
  action,
  days: action === 'INFREQUENT_ACCESS' ? 30 : action === 'ARCHIVE' ? 90 : 365,
  enabled: true,
  filters: []
});
const LIFECYCLE_FILTER_LABELS = {
  include_prefix: 'Include by prefix',
  include_pattern: 'Include by pattern',
  exclude_pattern: 'Exclude by pattern'
};
const LIFECYCLE_TARGET_LABELS = {
  objects: 'Objects',
  'previous-object-versions': 'Previous Versions',
  'multipart-uploads': 'Uncommitted Multipart Uploads'
};
const LIFECYCLE_ACTION_LABELS = {
  INFREQUENT_ACCESS: 'Move to Infrequent Access',
  ARCHIVE: 'Move to Archive',
  DELETE: 'Delete',
  ABORT: 'Abort Multipart Uploads'
};
const DEFAULT_NEW_BUCKET_CONFIG = {
  storageTier: 'Standard',
  autoTiering: 'Disabled',
  versioning: 'Disabled'
};
const VIEW_META = {
  keys: { group: 'Infrastructure', title: 'Credentials' },
  datasync: { group: 'Operations', title: 'Job Dashboard' },
  jobs: { group: 'Operations', title: 'Backup Jobs' },
  builder: { group: 'Operations', title: 'Backup Job' },
  explorer: { group: 'Infrastructure', title: 'VM Image Migration' },
  storage: { group: 'Infrastructure', title: 'OCI Object Storage' },
  settings: { group: 'System', title: 'Settings' }
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

function getInitialTheme() {
  return localStorage.getItem(THEME_KEY) === 'dark' ? 'dark' : 'light';
}

function formatApiError(err, fallback = 'Request failed.') {
  const detail = err?.response?.data?.detail;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    return detail.map(item => {
      const path = Array.isArray(item?.loc) ? item.loc.filter(part => part !== 'body').join('.') : '';
      return [path, item?.msg].filter(Boolean).join(': ');
    }).filter(Boolean).join('\n') || fallback;
  }
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

function isTransientUpgradeConnectionError(err) {
  const status = err?.response?.status;
  return !err?.response || status === 502 || status === 503 || status === 504;
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

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let size = bytes;
  let unitIndex = 0;
  while (size >= 1024 && unitIndex < units.length - 1) {
    size /= 1024;
    unitIndex += 1;
  }
  return `${unitIndex === 0 ? Math.round(size) : size.toFixed(1)} ${units[unitIndex]}`;
}

function formatChartBytes(value) {
  return formatBytes(value).replace(/\.0(?=\s)/, '');
}

function formatCompactNumber(value) {
  const number = Number(value || 0);
  if (!Number.isFinite(number) || number <= 0) return '0';
  return new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 }).format(number);
}

function formatRate(value) {
  const bytes = Number(value || 0);
  return bytes > 0 ? `${formatBytes(bytes)}/s` : '';
}

function formatDurationSeconds(value) {
  const rawSeconds = Number(value || 0);
  if (!Number.isFinite(rawSeconds) || rawSeconds <= 0) return '';
  if (rawSeconds < 1) return '<1s';
  const seconds = Math.round(rawSeconds);
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const secs = seconds % 60;
  if (hours) return `${hours}h ${minutes}m`;
  if (minutes) return `${minutes}m ${secs}s`;
  return `${secs}s`;
}

function backupSummaryParts(summary = {}) {
  const parts = [];
  if (Number(summary.bytes) > 0) parts.push(`${formatBytes(summary.bytes)} transferred`);
  if (Number(summary.files_transferred) > 0) parts.push(`${summary.files_transferred} files`);
  if (Number(summary.deletes) > 0) parts.push(`${summary.deletes} deleted`);
  const duration = formatDurationSeconds(summary.elapsed_seconds);
  if (duration) parts.push(duration);
  const speed = formatRate(summary.speed_bps);
  if (speed) parts.push(`${speed} avg`);
  if (Number(summary.errors) > 0) parts.push(`${summary.errors} errors`);
  return parts;
}

function localDateKey(value) {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

function buildBackupActivity(runs = [], dayCount = 7, now = new Date()) {
  const firstDay = new Date(now);
  firstDay.setHours(0, 0, 0, 0);
  firstDay.setDate(firstDay.getDate() - (dayCount - 1));

  const days = Array.from({ length: dayCount }, (_, index) => {
    const date = new Date(firstDay);
    date.setDate(firstDay.getDate() + index);
    return {
      key: localDateKey(date),
      label: date.toLocaleDateString(undefined, { weekday: 'short' }),
      dateLabel: date.toLocaleDateString(undefined, { day: 'numeric', month: 'short' }),
      bytes: 0,
      files: 0,
      success: 0,
      failed: 0,
      other: 0
    };
  });
  const daysByKey = new Map(days.map(day => [day.key, day]));

  runs.forEach(run => {
    if (run.kind !== 'data_sync') return;
    const timestamp = run.finished_at || run.updated_at || run.created_at;
    const day = daysByKey.get(localDateKey(timestamp));
    if (!day) return;

    day.bytes += Math.max(0, Number(run.rclone_summary?.bytes) || 0);
    day.files += Math.max(0, Number(run.rclone_summary?.files_transferred) || 0);
    const status = String(run.status || '').toLowerCase();
    if (status === 'success') day.success += 1;
    else if (status === 'failed' || status === 'timeout') day.failed += 1;
    else day.other += 1;
  });

  return {
    days,
    totalBytes: days.reduce((total, day) => total + day.bytes, 0),
    totalFiles: days.reduce((total, day) => total + day.files, 0),
    success: days.reduce((total, day) => total + day.success, 0),
    failed: days.reduce((total, day) => total + day.failed, 0),
    other: days.reduce((total, day) => total + day.other, 0),
    maxBytes: Math.max(...days.map(day => day.bytes), 0),
    maxFiles: Math.max(...days.map(day => day.files), 0)
  };
}

function cleanJobMessage(value = '') {
  const message = String(value || '').replace(/^rclone\s+/i, '').trim();
  return message ? `${message.charAt(0).toUpperCase()}${message.slice(1)}` : '';
}

function remoteNameFromPath(value = '') {
  return String(value || '').split(':')[0] || '';
}

function remoteTargetFromPath(value = '') {
  const rawValue = String(value || '');
  const separatorIndex = rawValue.indexOf(':');
  return separatorIndex >= 0 ? rawValue.slice(separatorIndex + 1) : '';
}

function bucketNameFromPath(value = '') {
  return String(value || '').trim().split('/')[0] || '';
}

function normalizeObjectMetadataName(value = '') {
  const key = String(value || '').trim().toLowerCase();
  if (!key) return '';
  return key.startsWith('opc-meta-') ? key.slice('opc-meta-'.length) : key;
}

function normalizeMetadataTags(tags = []) {
  return tags
    .map(tag => ({
      key: normalizeObjectMetadataName(tag?.key),
      value: String(tag?.value || '').trim()
    }))
    .filter(tag => tag.key || tag.value);
}

function normalizeLifecycleDays(value) {
  if (value === '' || value === null || value === undefined) return null;
  const days = Number(value);
  return Number.isInteger(days) && days > 0 ? days : Number.NaN;
}

function normalizeLocalRetention(policy = {}) {
  return {
    enabled: Boolean(policy.enabled),
    delete_after_days: Number(policy.delete_after_days || DEFAULT_LOCAL_RETENTION.delete_after_days),
    min_file_age_hours: Number(policy.min_file_age_hours || DEFAULT_LOCAL_RETENTION.min_file_age_hours)
  };
}

function normalizeRcloneLimits(job = {}) {
  const bwlimit = String(job.bwlimit || '').trim();
  const tpsRaw = job.tpslimit;
  const tpslimit = tpsRaw === '' || tpsRaw === null || tpsRaw === undefined ? null : Number(tpsRaw);
  return { bwlimit, tpslimit };
}

function normalizeLifecycleFilters(policy = {}) {
  const rawFilters = Array.isArray(policy.filters) ? policy.filters : [];
  const filters = rawFilters.map(filter => ({
    type: Object.prototype.hasOwnProperty.call(LIFECYCLE_FILTER_LABELS, filter?.type) ? filter.type : 'include_prefix',
    value: String(filter?.value || '').trim()
  }));
  if (!filters.length && policy.prefix) {
    filters.push({
      type: 'include_prefix',
      value: String(policy.prefix || '').trim().replace(/^\/+/, '')
    });
  }
  return filters;
}

function lifecycleActionsForTarget(target = 'objects') {
  if (target === 'multipart-uploads') return ['ABORT'];
  return ['INFREQUENT_ACCESS', 'ARCHIVE', 'DELETE'];
}

function normalizeLifecycleAction(action = 'ARCHIVE', target = 'objects') {
  const normalized = String(action || '').trim().toUpperCase();
  const actions = lifecycleActionsForTarget(target);
  return actions.includes(normalized) ? normalized : actions[0];
}

function normalizeLifecycleTarget(target = 'objects') {
  return Object.prototype.hasOwnProperty.call(LIFECYCLE_TARGET_LABELS, target) ? target : 'objects';
}

function normalizeLifecycleRule(rule = {}) {
  const target = normalizeLifecycleTarget(rule.target);
  return {
    name: String(rule.name || '').trim(),
    target,
    action: normalizeLifecycleAction(rule.action, target),
    days: normalizeLifecycleDays(rule.days),
    enabled: rule.enabled !== false,
    filters: normalizeLifecycleFilters(rule)
  };
}

function legacyLifecycleRules(policy = {}) {
  const filters = normalizeLifecycleFilters(policy);
  return [
    { action: 'INFREQUENT_ACCESS', days: policy.infrequent_access_after_days },
    { action: 'ARCHIVE', days: policy.archive_after_days },
    { action: 'DELETE', days: policy.delete_after_days },
    { action: 'DELETE', target: 'previous-object-versions', days: policy.previous_versions_delete_after_days }
  ]
    .map((rule, index) => ({
      name: `legacy-rule-${index + 1}`,
      target: rule.target || 'objects',
      action: rule.action,
      days: normalizeLifecycleDays(rule.days),
      enabled: true,
      filters
    }))
    .filter(rule => rule.days);
}

function normalizeLifecyclePolicy(policy = {}) {
  const filters = normalizeLifecycleFilters(policy);
  const rules = Array.isArray(policy.rules) && policy.rules.length
    ? policy.rules.map(normalizeLifecycleRule)
    : legacyLifecycleRules(policy);
  const findLegacyDays = (target, action) => rules.find(rule => rule.target === target && rule.action === action)?.days || null;
  return {
    enabled: Boolean(policy.enabled),
    prefix: filters.find(filter => filter.type === 'include_prefix')?.value || '',
    filters,
    rules,
    infrequent_access_after_days: findLegacyDays('objects', 'INFREQUENT_ACCESS'),
    archive_after_days: findLegacyDays('objects', 'ARCHIVE'),
    delete_after_days: findLegacyDays('objects', 'DELETE'),
    previous_versions_delete_after_days: findLegacyDays('previous-object-versions', 'DELETE')
  };
}

export default function App() {
  const mainScrollRef = useRef(null);
  const [authState, setAuthState] = useState(getInitialAuth);
  const [theme, setTheme] = useState(getInitialTheme);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [loginForm, setLoginForm] = useState({ username: 'admin', password: '' });
  const [loginError, setLoginError] = useState('');
  const [authLoading, setAuthLoading] = useState(false);
  const [passwordForm, setPasswordForm] = useState({ currentPassword: '', newPassword: '', confirmPassword: '' });
  const [passwordMessage, setPasswordMessage] = useState('');
  const [notice, setNotice] = useState(null);
  const [health, setHealth] = useState(null);
  const [jobRuns, setJobRuns] = useState([]);
  const [exportingConfig, setExportingConfig] = useState(false);
  const [importingConfig, setImportingConfig] = useState(false);
  const [confirmDialog, setConfirmDialog] = useState(null);
  const [jobLogSettings, setJobLogSettings] = useState(null);
  const [jobLogSettingsForm, setJobLogSettingsForm] = useState({ retentionDays: 14, maxSize: '10M' });
  const [savingJobLogSettings, setSavingJobLogSettings] = useState(false);
  const [localDiskSettings, setLocalDiskSettings] = useState(null);
  const [localDiskSettingsForm, setLocalDiskSettingsForm] = useState({ warningPercent: 80, criticalPercent: 90 });
  const [savingLocalDiskSettings, setSavingLocalDiskSettings] = useState(false);
  const [timeSettings, setTimeSettings] = useState(null);
  const [timeSettingsForm, setTimeSettingsForm] = useState({ timezone: 'UTC', ntpServers: '0.pool.ntp.org 1.pool.ntp.org' });
  const [savingTimeSettings, setSavingTimeSettings] = useState(false);
  const [networkSettings, setNetworkSettings] = useState(null);
  const [networkSettingsForm, setNetworkSettingsForm] = useState({ mode: 'dhcp', interface: '', address: '', prefixLength: 24, gateway: '', dnsServers: '' });
  const [savingNetworkSettings, setSavingNetworkSettings] = useState(false);
  const [networkNow, setNetworkNow] = useState(Date.now());
  const [rcloneDefaultSettings, setRcloneDefaultSettings] = useState({ bwlimit: '', tpslimit: null });
  const [rcloneDefaultSettingsForm, setRcloneDefaultSettingsForm] = useState({ bwlimit: '', tpslimit: '' });
  const [savingRcloneDefaultSettings, setSavingRcloneDefaultSettings] = useState(false);
  const [upgradeStatus, setUpgradeStatus] = useState(null);
  const [upgradeCheck, setUpgradeCheck] = useState(null);
  const [upgradeLog, setUpgradeLog] = useState('');
  const [checkingUpgrade, setCheckingUpgrade] = useState(false);
  const [startingUpgrade, setStartingUpgrade] = useState(false);
  const [showUpgradeLog, setShowUpgradeLog] = useState(false);
  const [upgradeReconnecting, setUpgradeReconnecting] = useState(false);
  const isAuthenticated = Boolean(authState.token);

  const latestUpgradeCommit = upgradeCheck?.latest_commit || upgradeStatus?.target_commit || '';
  const latestUpgradeShort = upgradeCheck?.latest_short || latestUpgradeCommit.slice(0, 7);
  const upgradeVersionsMatch = Boolean(
    upgradeStatus?.current_commit
      && latestUpgradeCommit
      && upgradeStatus.current_commit === latestUpgradeCommit
  );
  const upgradeIsCurrent = upgradeCheck?.up_to_date === true || upgradeVersionsMatch;

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
          setNotice(null);
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
  const [view, setView] = useState('datasync');
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
  const [bucketProtection, setBucketProtection] = useState(null);
  const [bucketProtectionLoading, setBucketProtectionLoading] = useState(false);

  useEffect(() => {
    localStorage.setItem(THEME_KEY, theme);
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  const [syncJob, setSyncJob] = useState(createDefaultSyncJob);
  const [editingJobName, setEditingJobName] = useState('');
  const visibleRemoteDetails = useMemo(() => {
    const detailsByName = new Map(remoteDetails.map((remote) => [remote.name, remote]));
    return remotes
      .filter((remoteName) => !remoteName.endsWith('_rclone'))
      .map((remoteName) => detailsByName.get(remoteName) || { name: remoteName, type: '' });
  }, [remotes, remoteDetails]);
  const localRemotes = visibleRemoteDetails.filter((remote) => remote.type === 'local');
  const externalRemotes = visibleRemoteDetails.filter((remote) => remote.type !== 'local');
  const selectedSyncRemoteName = remoteNameFromPath(syncJob.source_remote);
  const selectedSyncSourceValue = remoteTargetFromPath(syncJob.source_remote);
  const selectedSyncRemoteDetail = visibleRemoteDetails.find((remote) => remote.name === selectedSyncRemoteName);
  const selectedSyncSourceIsManagedLocal = selectedSyncRemoteDetail?.type === 'local' && selectedSyncSourceValue.startsWith('/');
  const selectedSyncRetentionConflict = jobs.find((job) => (
    job.name !== editingJobName &&
    job.name !== syncJob.name &&
    job.source_remote === syncJob.source_remote &&
    job.local_retention?.enabled
  ));

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
  const [newBucketConfig, setNewBucketConfig] = useState(DEFAULT_NEW_BUCKET_CONFIG);
  const [newFolderName, setNewFolderName] = useState('');
  const [bucketLifecycleForm, setBucketLifecycleForm] = useState(createDefaultLifecyclePolicy);
  const [bucketLifecycleNotice, setBucketLifecycleNotice] = useState(null);
  const [savingBucketSettings, setSavingBucketSettings] = useState(false);

  const showError = (title, err) => {
    console.error(err);
    if (err?.response?.status === 401) return;
    setNotice({ type: 'error', title, message: formatApiError(err, title) });
  };

  const showSuccess = (message) => {
    setNotice({ type: 'success', title: 'Done', message });
  };

  const closeConfirmDialog = () => {
    setConfirmDialog(null);
  };

  const confirmDialogAction = async () => {
    const action = confirmDialog?.onConfirm;
    setConfirmDialog(null);
    if (action) {
      await action();
    }
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
      const res = await api.get('/job-history?limit=300');
      setJobRuns(res.data.runs || []);
    } catch (err) {
      console.error(err);
    }
  };

  const fetchJobLogSettings = async () => {
    try {
      const res = await api.get('/job-log-settings');
      setJobLogSettings(res.data);
      setJobLogSettingsForm({
        retentionDays: res.data.retention_days || 14,
        maxSize: res.data.max_size || '10M'
      });
    } catch (err) {
      console.error(err);
    }
  };

  const fetchLocalDiskSettings = async () => {
    try {
      const res = await api.get('/local-disk-settings');
      setLocalDiskSettings(res.data);
      setLocalDiskSettingsForm({
        warningPercent: res.data.warning_percent || 80,
        criticalPercent: res.data.critical_percent || 90
      });
    } catch (err) {
      console.error(err);
    }
  };

  const fetchTimeSettings = async () => {
    try {
      const res = await api.get('/time-settings');
      setTimeSettings(res.data);
      setTimeSettingsForm({
        timezone: res.data.configured_timezone || res.data.timezone || 'UTC',
        ntpServers: res.data.ntp_servers || '0.pool.ntp.org 1.pool.ntp.org'
      });
    } catch (err) {
      console.error(err);
    }
  };

  const fetchNetworkSettings = async (resetForm = true) => {
    try {
      const res = await api.get('/network-settings');
      setNetworkSettings(res.data);
      if (resetForm) {
        const selectedInterface = (res.data.interfaces || []).find(item => item.name === res.data.interface)
          || (res.data.interfaces || []).find(item => item.default_route)
          || (res.data.interfaces || [])[0];
        const configuredAddress = res.data.address || selectedInterface?.ipv4_addresses?.[0] || '';
        const [address = '', prefixLength = '24'] = configuredAddress.split('/');
        const dnsServers = (res.data.dns_servers?.length ? res.data.dns_servers : selectedInterface?.dns_servers || []).join(' ');
        setNetworkSettingsForm({
          mode: res.data.mode === 'static' ? 'static' : 'dhcp',
          interface: res.data.interface || selectedInterface?.name || '',
          address,
          prefixLength: Number(prefixLength || 24),
          gateway: res.data.gateway || selectedInterface?.gateway || '',
          dnsServers
        });
      }
      return res.data;
    } catch (err) {
      console.error(err);
      return null;
    }
  };

  const fetchRcloneDefaultSettings = async () => {
    try {
      const res = await api.get('/rclone-default-settings');
      setRcloneDefaultSettings(res.data);
      setRcloneDefaultSettingsForm({
        bwlimit: res.data.bwlimit || '',
        tpslimit: res.data.tpslimit ?? ''
      });
      return res.data;
    } catch (err) {
      console.error(err);
      return { bwlimit: '', tpslimit: null };
    }
  };

  const fetchUpgradeStatus = async () => {
    try {
      const res = await api.get('/upgrade/status');
      setUpgradeStatus(res.data);
      setUpgradeReconnecting(false);
      return res.data;
    } catch (err) {
      if (isTransientUpgradeConnectionError(err)) {
        setUpgradeReconnecting(true);
      }
      console.error(err);
      return null;
    }
  };

  const fetchUpgradeLog = async () => {
    try {
      const res = await api.get('/upgrade/log');
      setUpgradeLog(res.data.log || 'No upgrade log yet.');
      setUpgradeReconnecting(false);
      return res.data;
    } catch (err) {
      if (isTransientUpgradeConnectionError(err)) {
        setUpgradeReconnecting(true);
        return null;
      }
      setUpgradeLog(formatApiError(err, 'Failed to load upgrade log.'));
      return null;
    }
  };

  const fetchUpgradeCheck = async (silent = false) => {
    setCheckingUpgrade(true);
    try {
      const res = await api.post('/upgrade/check');
      setUpgradeCheck(res.data);
      setUpgradeReconnecting(false);
      setUpgradeStatus(prev => ({
        ...(prev || {}),
        ...res.data,
        status: res.data.up_to_date ? 'success' : 'idle',
        message: res.data.up_to_date ? 'You are on the latest version.' : 'A new version is available.'
      }));
      return res.data;
    } catch (err) {
      if (silent) console.error(err);
      else showError('Failed to check for updates', err);
      return null;
    } finally {
      setCheckingUpgrade(false);
    }
  };

  const handleCheckUpgrade = () => fetchUpgradeCheck(false);

  const handleStartUpgrade = async () => {
    if (!window.confirm('Start upgrade from GitHub now? The service may restart during installation.')) {
      return;
    }

    setStartingUpgrade(true);
    try {
      const res = await api.post('/upgrade/start');
      setUpgradeStatus(res.data);
      setShowUpgradeLog(true);
      setUpgradeLog('');
      setUpgradeReconnecting(false);
      window.setTimeout(fetchUpgradeStatus, 1500);
      window.setTimeout(fetchUpgradeLog, 1500);
    } catch (err) {
      showError('Failed to start upgrade', err);
    }
    setStartingUpgrade(false);
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

  const dashboardStats = useMemo(() => {
    const activeLatestRuns = jobs.map(job => latestRunByJob[job.name]).filter(Boolean);
    const running = activeLatestRuns.filter(run => ['queued', 'running', 'retrying'].includes(String(run.status || '').toLowerCase())).length;
    const attention = activeLatestRuns.filter(run => ['failed', 'timeout'].includes(String(run.status || '').toLowerCase())).length;
    const lastSuccess = jobRuns.find(run => run.kind === 'data_sync' && String(run.status || '').toLowerCase() === 'success');
    return {
      total: jobs.length,
      running,
      attention,
      lastSuccessAt: lastSuccess?.finished_at || lastSuccess?.updated_at || lastSuccess?.created_at || ''
    };
  }, [jobRuns, jobs, latestRunByJob]);

  const backupActivity = useMemo(() => buildBackupActivity(jobRuns), [jobRuns]);
  const activityChart = useMemo(() => {
    const width = 840;
    const height = 280;
    const top = 128;
    const baseline = height;
    const plotHeight = baseline - top;
    const byteScaleMax = backupActivity.maxBytes || 1;
    const fileScaleMax = backupActivity.maxFiles || 1;
    const columnWidth = width / Math.max(backupActivity.days.length, 1);
    const points = backupActivity.days.map((day, index) => ({
      ...day,
      x: columnWidth * index + columnWidth / 2,
      byteY: baseline - (day.bytes / byteScaleMax) * plotHeight,
      fileY: baseline - (day.files / fileScaleMax) * plotHeight
    }));
    const buildPaths = (yKey) => {
      const linePath = points.map((point, index) => `${index === 0 ? 'M' : 'L'} ${point.x} ${point[yKey]}`).join(' ');
      const areaPath = points.length
        ? `M 0 ${baseline} L 0 ${points[0][yKey]} L ${points.map(point => `${point.x} ${point[yKey]}`).join(' L ')} L ${width} ${points[points.length - 1][yKey]} L ${width} ${baseline} Z`
        : '';
      return { linePath, areaPath };
    };
    return {
      width,
      height,
      points,
      bytes: buildPaths('byteY'),
      files: buildPaths('fileY')
    };
  }, [backupActivity]);

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

  const handleImportRuntimeConfig = async (event) => {
    const selectedFile = event.target.files?.[0];
    event.target.value = '';
    if (!selectedFile) return;

    if (!window.confirm('Restore runtime config from this ZIP? Current config will be backed up first. This may replace admin credentials, OCI profiles, rclone remotes, jobs, and job history.')) {
      return;
    }

    const uploadData = new FormData();
    uploadData.append('file', selectedFile);
    setImportingConfig(true);
    try {
      const res = await api.post('/runtime-config/import', uploadData, {
        headers: { 'Content-Type': 'multipart/form-data' }
      });

      if (res.data.token) {
        localStorage.setItem(SESSION_TOKEN_KEY, res.data.token);
        localStorage.setItem(SESSION_USERNAME_KEY, res.data.username || authState.username);
        setAuthState({ token: res.data.token, mode: 'session', username: res.data.username || authState.username });
      }

      const warningText = (res.data.warnings || []).length ? `\nWarnings: ${res.data.warnings.join(' ')}` : '';
      setNotice({
        type: 'success',
        title: 'Runtime config restored',
        message: `Restored ${res.data.restored_count || 0} item(s). Pre-restore backup: ${res.data.pre_restore_backup || 'created'}.${warningText}`
      });
    } catch (err) {
      showError('Failed to import runtime config', err);
    }
    setImportingConfig(false);
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

  const handleSaveJobLogSettings = async (event) => {
    event.preventDefault();
    const retentionDays = Number(jobLogSettingsForm.retentionDays);
    const maxSize = String(jobLogSettingsForm.maxSize || '').trim().toUpperCase();

    if (!Number.isInteger(retentionDays) || retentionDays < 1 || retentionDays > 365) {
      setNotice({ type: 'error', title: 'Invalid retention', message: 'Retention days must be between 1 and 365.' });
      return;
    }

    if (!/^[1-9][0-9]*[KMG]?$/.test(maxSize)) {
      setNotice({ type: 'error', title: 'Invalid max size', message: 'Max size must look like 10M, 512K, or 1G.' });
      return;
    }

    setSavingJobLogSettings(true);
    try {
      const res = await api.put('/job-log-settings', {
        retention_days: retentionDays,
        max_size: maxSize
      });
      setJobLogSettings(res.data);
      setJobLogSettingsForm({
        retentionDays: res.data.retention_days,
        maxSize: res.data.max_size
      });
      showSuccess('Job log rotation settings saved.');
      fetchHealth();
    } catch (err) {
      showError('Failed to save job log settings', err);
    }
    setSavingJobLogSettings(false);
  };

  const handleSaveLocalDiskSettings = async (event) => {
    event.preventDefault();
    const warningPercent = Number(localDiskSettingsForm.warningPercent);
    const criticalPercent = Number(localDiskSettingsForm.criticalPercent);

    if (!Number.isInteger(warningPercent) || warningPercent < 1 || warningPercent > 99) {
      setNotice({ type: 'error', title: 'Invalid disk warning', message: 'Warning threshold must be between 1 and 99 percent.' });
      return;
    }
    if (!Number.isInteger(criticalPercent) || criticalPercent < 2 || criticalPercent > 100 || criticalPercent <= warningPercent) {
      setNotice({ type: 'error', title: 'Invalid disk critical', message: 'Critical threshold must be higher than warning and at most 100 percent.' });
      return;
    }

    setSavingLocalDiskSettings(true);
    try {
      const res = await api.put('/local-disk-settings', {
        warning_percent: warningPercent,
        critical_percent: criticalPercent
      });
      setLocalDiskSettings(res.data);
      setLocalDiskSettingsForm({
        warningPercent: res.data.warning_percent,
        criticalPercent: res.data.critical_percent
      });
      showSuccess('Local disk thresholds saved.');
      fetchHealth();
    } catch (err) {
      showError('Failed to save local disk settings', err);
    }
    setSavingLocalDiskSettings(false);
  };

  const handleSaveTimeSettings = async (event) => {
    event.preventDefault();
    const timezone = String(timeSettingsForm.timezone || '').trim();
    const ntpServers = String(timeSettingsForm.ntpServers || '').trim().replace(/,/g, ' ').replace(/\s+/g, ' ');

    if (!/^[A-Za-z0-9_+.-]+(\/[A-Za-z0-9_+.-]+)*$/.test(timezone)) {
      setNotice({ type: 'error', title: 'Invalid time settings', message: 'Timezone must be an IANA name, for example Europe/Stockholm or Asia/Singapore.' });
      return;
    }

    if (!ntpServers) {
      setNotice({ type: 'error', title: 'Invalid time settings', message: 'At least one NTP server is required.' });
      return;
    }

    if (ntpServers.split(' ').some(server => !/^[A-Za-z0-9][A-Za-z0-9._:-]*$/.test(server))) {
      setNotice({ type: 'error', title: 'Invalid time settings', message: 'NTP servers may contain hostnames or IP addresses separated by spaces or commas.' });
      return;
    }

    setSavingTimeSettings(true);
    try {
      const res = await api.put('/time-settings', {
        timezone,
        ntp_servers: ntpServers
      });
      setTimeSettings(res.data);
      setTimeSettingsForm({
        timezone: res.data.configured_timezone || timezone,
        ntpServers: res.data.ntp_servers || ntpServers
      });
      showSuccess('Time sync settings saved.');
      fetchHealth();
    } catch (err) {
      showError('Failed to save time sync settings', err);
    }
    setSavingTimeSettings(false);
  };

  const stageNetworkSettings = async () => {
    setSavingNetworkSettings(true);
    try {
      const res = await api.put('/network-settings', {
        mode: networkSettingsForm.mode,
        interface: networkSettingsForm.interface,
        address: networkSettingsForm.address.trim(),
        prefix_length: Number(networkSettingsForm.prefixLength),
        gateway: networkSettingsForm.gateway.trim(),
        dns_servers: networkSettingsForm.dnsServers.trim()
      });
      setNetworkSettings(res.data);
      setNetworkNow(Date.now());
      showSuccess('Network change staged. Confirm it before the rollback timer expires.');
    } catch (err) {
      showError('Failed to apply network settings', err);
    }
    setSavingNetworkSettings(false);
  };

  const handleSaveNetworkSettings = (event) => {
    event.preventDefault();
    if (!networkSettingsForm.interface) {
      setNotice({ type: 'error', title: 'Invalid network settings', message: 'Select a network interface.' });
      return;
    }
    if (networkSettingsForm.mode === 'static' && (!networkSettingsForm.address.trim() || !networkSettingsForm.gateway.trim() || !networkSettingsForm.dnsServers.trim())) {
      setNotice({ type: 'error', title: 'Invalid network settings', message: 'Static IPv4 requires an address, prefix length, gateway, and at least one DNS server.' });
      return;
    }

    const target = networkSettingsForm.mode === 'static'
      ? `${networkSettingsForm.address}/${networkSettingsForm.prefixLength}`
      : 'DHCP';
    setConfirmDialog({
      icon: 'shield',
      title: 'Apply network configuration?',
      message: `${networkSettingsForm.interface} will be changed to ${target}. The current connection may be interrupted.`,
      detail: 'The previous configuration is restored automatically after three minutes unless the new configuration is confirmed. Reserve or assign a static address in the cloud VNIC or local network first.',
      confirmLabel: 'Apply & Test',
      onConfirm: stageNetworkSettings
    });
  };

  const handleConfirmNetworkSettings = async () => {
    setSavingNetworkSettings(true);
    try {
      const res = await api.post('/network-settings/confirm');
      setNetworkSettings(res.data);
      await fetchNetworkSettings(true);
      showSuccess('Network configuration confirmed.');
    } catch (err) {
      showError('Failed to confirm network settings', err);
    }
    setSavingNetworkSettings(false);
  };

  const handleRollbackNetworkSettings = async () => {
    setSavingNetworkSettings(true);
    try {
      const res = await api.post('/network-settings/rollback');
      setNetworkSettings(res.data);
      await fetchNetworkSettings(true);
      showSuccess('Previous network configuration restored.');
    } catch (err) {
      showError('Failed to restore network settings', err);
    }
    setSavingNetworkSettings(false);
  };

  const handleSaveRcloneDefaultSettings = async (event) => {
    event.preventDefault();
    const limits = normalizeRcloneLimits(rcloneDefaultSettingsForm);
    if (limits.bwlimit && !/^(off|\d+(\.\d+)?[KkMmGgTtPp]?)$/.test(limits.bwlimit)) {
      setNotice({ type: 'error', title: 'Invalid bandwidth limit', message: 'Bandwidth limit must be empty, off, or a value like 700M, 1G, or 500K.' });
      return;
    }
    if (limits.tpslimit !== null && (!Number.isFinite(limits.tpslimit) || limits.tpslimit < 0 || limits.tpslimit > 10000)) {
      setNotice({ type: 'error', title: 'Invalid TPS limit', message: 'TPS limit must be empty or a number between 0 and 10000.' });
      return;
    }

    setSavingRcloneDefaultSettings(true);
    try {
      const res = await api.put('/rclone-default-settings', {
        bwlimit: limits.bwlimit,
        tpslimit: limits.tpslimit
      });
      setRcloneDefaultSettings(res.data);
      setRcloneDefaultSettingsForm({
        bwlimit: res.data.bwlimit || '',
        tpslimit: res.data.tpslimit ?? ''
      });
      showSuccess('Backup job defaults saved.');
    } catch (err) {
      showError('Failed to save backup job defaults', err);
    }
    setSavingRcloneDefaultSettings(false);
  };

  useEffect(() => {
    if (!isAuthenticated) return;
    fetchProfiles();
    fetchRemotes();
    fetchJobs();
    fetchHealth();
    fetchJobRuns();
    fetchJobLogSettings();
    fetchLocalDiskSettings();
    fetchTimeSettings();
    fetchNetworkSettings();
    fetchRcloneDefaultSettings();
    fetchUpgradeStatus();
    fetchUpgradeCheck(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isAuthenticated, api]);

  useEffect(() => {
    if (!isAuthenticated || !networkSettings?.pending) return;
    const clockInterval = setInterval(() => setNetworkNow(Date.now()), 1000);
    const statusInterval = setInterval(() => fetchNetworkSettings(false), 3000);
    return () => {
      clearInterval(clockInterval);
      clearInterval(statusInterval);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isAuthenticated, Boolean(networkSettings?.pending)]);

  useEffect(() => {
    if (!isAuthenticated || (!showUpgradeLog && upgradeStatus?.status !== 'running')) return;

    const interval = setInterval(() => {
      fetchUpgradeStatus();
      if (showUpgradeLog) fetchUpgradeLog();
    }, upgradeStatus?.status === 'running' ? 3000 : 8000);

    return () => clearInterval(interval);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isAuthenticated, showUpgradeLog, upgradeStatus?.status, api]);

  useEffect(() => {
    if (!isAuthenticated || upgradeStatus?.status !== 'success' || !upgradeStatus?.finished_at) return;
    if (upgradeCheck?.current_commit === upgradeStatus?.current_commit && upgradeCheck?.latest_commit) return;

    const refreshCompletedUpgrade = async () => {
      await fetchUpgradeLog();
      try {
        const res = await api.post('/upgrade/check');
        setUpgradeCheck(res.data);
        setUpgradeReconnecting(false);
      } catch (err) {
        if (isTransientUpgradeConnectionError(err)) {
          setUpgradeReconnecting(true);
        } else {
          console.error(err);
        }
      }
    };

    refreshCompletedUpgrade();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isAuthenticated, upgradeStatus?.status, upgradeStatus?.finished_at, upgradeStatus?.current_commit, upgradeCheck?.current_commit, upgradeCheck?.latest_commit, api]);

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
    if (remoteConfig.provider === 'local' && remoteConfig.localMode === 'server_folder' && remoteConfig.localNfsEnabled && !remoteConfig.localNfsClients.trim()) {
      setNotice({ type: 'error', title: 'Missing NFS clients', message: 'Add at least one allowed client IP, hostname, or CIDR range for NFSv4.' });
      return;
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
        rData.append("local_nfs_enabled", remoteConfig.localNfsEnabled ? 'true' : 'false');
        rData.append("local_nfs_clients", remoteConfig.localNfsClients);
      }
      const res = await api.post(`/save-remote`, rData);
      fetchRemotes();
      if (res.data.share || res.data.nfs_share) {
        const details = [`Remote saved: ${res.data.local_path}`];
        if (res.data.share) {
          const shareUser = res.data.share.username ? `User: ${res.data.share.username}` : 'Access: everyone';
          details.push(`SMB: ${res.data.share.unc_path}`);
          details.push(`Mac: ${res.data.share.smb_url}`);
          details.push(shareUser);
        }
        if (res.data.nfs_share) {
          details.push(`NFSv4: ${res.data.nfs_share.mount}`);
          details.push(`Mount: ${res.data.nfs_share.mount_command}`);
          details.push(`Allowed clients: ${res.data.nfs_share.clients}`);
        }
        showSuccess(details.join('\n'));
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

  const fetchVms = async (profile) => {
    if (!profile) {
      setActiveSourceProfile('');
      setVms([]);
      setSelectedVms([]);
      setView('explorer');
      return;
    }

    setLoading(true);
    setSelectedVms([]);
    try {
      const res = await api.get(`/list-vms/${encodeURIComponent(profile)}`);
      setVms(res.data);
      setActiveSourceProfile(profile);
      setView('explorer');
    } catch (err) {
      showError('Failed to list VMs', err);
    }
    setLoading(false);
  };

  const loadSourceBuckets = async (remoteName) => {
    if (!remoteName) {
      setSourceBuckets([]);
      return [];
    }

    try {
      const res = await api.get(`/list-remote-buckets/${encodeURIComponent(remoteName)}`);
      const buckets = (res.data.buckets || []).map(item => (
        typeof item === 'string' ? { name: item, value: item } : item
      ));
      setSourceBuckets(buckets);
      return buckets;
    } catch (err) {
      showError('Failed to list buckets for remote', err);
      setSourceBuckets([]);
      return [];
    }
  };

  const loadDestBuckets = async (profile) => {
    if (!profile) {
      setDestBuckets([]);
      setBucketProtection(null);
      return [];
    }

    try {
      const res = await api.get(`/list-buckets/${encodeURIComponent(profile)}`);
      setDestBuckets(res.data);
      return res.data;
    } catch (err) {
      showError('Failed to list buckets for destination profile', err);
      setDestBuckets([]);
      return [];
    }
  };

  const fetchBucketProtection = async (profile, bucket) => {
    const bucketName = bucketNameFromPath(bucket);
    if (!profile || !bucketName) {
      setBucketProtection(null);
      return null;
    }

    setBucketProtectionLoading(true);
    try {
      const res = await api.get('/bucket-protection', {
        params: { profile_name: profile, bucket_name: bucketName }
      });
      setBucketProtection(res.data);
      return res.data;
    } catch (err) {
      showError('Failed to load bucket protection', err);
      setBucketProtection(null);
      return null;
    } finally {
      setBucketProtectionLoading(false);
    }
  };

  const fetchBucketLifecyclePolicy = async (profile, bucket) => {
    const bucketName = bucketNameFromPath(bucket);
    if (!profile || !bucketName) {
      setBucketLifecycleForm(createDefaultLifecyclePolicy());
      return null;
    }

    try {
      const res = await api.get('/bucket-lifecycle-policy', {
        params: { profile_name: profile, bucket_name: bucketName }
      });
      const policy = res.data.lifecycle_policy || createDefaultLifecyclePolicy();
      setBucketLifecycleForm(normalizeLifecyclePolicy({ ...createDefaultLifecyclePolicy(), ...policy }));
      return policy;
    } catch (err) {
      showError('Failed to load bucket lifecycle policy', err);
      setBucketLifecycleForm(createDefaultLifecyclePolicy());
      return null;
    }
  };

  const loadSelectedBucketSettings = async (profile, bucket) => {
    await Promise.all([
      fetchBucketProtection(profile, bucket),
      fetchBucketLifecyclePolicy(profile, bucket)
    ]);
  };

  const handleSetBucketVersioning = async (versioning) => {
    const bucketName = bucketNameFromPath(selectedBucket);
    if (!storageProfile || !bucketName) return;
    const isEnabling = versioning === 'Enabled';
    setConfirmDialog({
      title: isEnabling ? 'Enable Object Versioning' : 'Suspend Object Versioning',
      message: isEnabling
        ? `This enables Object Versioning on bucket "${bucketName}" in OCI. Previous object versions are kept when data is overwritten or deleted.`
        : `Suspend Object Versioning on bucket "${bucketName}"? Existing object versions remain available, but new overwrites and deletes will no longer create new versions.`,
      detail: isEnabling
        ? 'This is a bucket-level protection setting.'
        : 'OCI versioning cannot return to Disabled after it has been enabled; it can only be suspended.',
      confirmLabel: isEnabling ? 'Enable Versioning' : 'Suspend Versioning',
      icon: 'shield',
      onConfirm: async () => {
        setBucketProtectionLoading(true);
        try {
          await api.post('/bucket-versioning', {
            profile_name: storageProfile,
            bucket_name: bucketName,
            versioning
          });
          showSuccess(isEnabling ? 'Object Versioning enabled.' : 'Object Versioning suspended.');
          await fetchBucketProtection(storageProfile, bucketName);
        } catch (err) {
          showError('Failed to update Object Versioning', err);
        } finally {
          setBucketProtectionLoading(false);
        }
      }
    });
  };

  const handleSetBucketAutoTiering = async (autoTiering) => {
    const bucketName = bucketNameFromPath(selectedBucket);
    if (!storageProfile || !bucketName) return;
    const isEnabling = autoTiering === 'InfrequentAccess';
    setConfirmDialog({
      title: isEnabling ? 'Enable Auto-Tiering' : 'Disable Auto-Tiering',
      message: `${isEnabling ? 'Enable' : 'Disable'} Auto-Tiering on bucket "${bucketName}"?`,
      detail: isEnabling
        ? 'OCI can automatically move eligible Standard objects to Infrequent Access based on access patterns.'
        : 'OCI will stop automatically moving objects between Standard and Infrequent Access for this bucket.',
      confirmLabel: isEnabling ? 'Enable Auto-Tiering' : 'Disable Auto-Tiering',
      icon: 'archive',
      onConfirm: async () => {
        setBucketProtectionLoading(true);
        try {
          await api.post('/bucket-auto-tiering', {
            profile_name: storageProfile,
            bucket_name: bucketName,
            auto_tiering: autoTiering
          });
          showSuccess(isEnabling ? 'Auto-Tiering enabled.' : 'Auto-Tiering disabled.');
          await fetchBucketProtection(storageProfile, bucketName);
        } catch (err) {
          showError('Failed to update Auto-Tiering', err);
        } finally {
          setBucketProtectionLoading(false);
        }
      }
    });
  };

  const startNewSyncJob = () => {
    setEditingJobName('');
    setSyncJob({
      ...createDefaultSyncJob(),
      bwlimit: rcloneDefaultSettings?.bwlimit || '',
      tpslimit: rcloneDefaultSettings?.tpslimit ?? ''
    });
    setSourceBuckets([]);
    setDestBuckets([]);
    setView('builder');
  };

  // --- Job Management ---
  const handleSaveJob = async () => {
    if (!syncJob.name || !syncJob.source_remote || !syncJob.dest_bucket) {
      setNotice({ type: 'error', title: 'Missing job fields', message: 'Job name, source remote, and destination bucket are required.' });
      return;
    }

    const metadataTags = normalizeMetadataTags(syncJob.metadata_tags);
    if (metadataTags.some(tag => !tag.key || !tag.value)) {
      setNotice({ type: 'error', title: 'Invalid metadata', message: 'Metadata tags need both a key and a value.' });
      return;
    }
    if (metadataTags.some(tag => !/^[a-z0-9][a-z0-9._-]{0,118}$/.test(tag.key))) {
      setNotice({ type: 'error', title: 'Invalid metadata', message: 'Metadata names may contain lowercase letters, numbers, dot, underscore, and dash. OCI will store them as opc-meta-*.' });
      return;
    }
    const metadataKeys = metadataTags.map(tag => tag.key);
    if (new Set(metadataKeys).size !== metadataKeys.length) {
      setNotice({ type: 'error', title: 'Invalid metadata', message: 'Metadata tag keys must be unique.' });
      return;
    }
    if (metadataTags.some(tag => tag.value.length > 1024 || /[\r\n\0]/.test(tag.value))) {
      setNotice({ type: 'error', title: 'Invalid metadata', message: 'Metadata values must be single-line text up to 1024 characters.' });
      return;
    }
    if (syncJob.schedule.frequency === 'monthly') {
      const dayOfMonth = Number(syncJob.schedule.day_of_month);
      if (!Number.isInteger(dayOfMonth) || dayOfMonth < 1 || dayOfMonth > 31) {
        setNotice({ type: 'error', title: 'Invalid schedule', message: 'Monthly day must be between 1 and 31.' });
        return;
      }
    }
    const rcloneLimits = normalizeRcloneLimits(syncJob);
    if (rcloneLimits.bwlimit && !/^(off|\d+(\.\d+)?[KkMmGgTtPp]?)$/.test(rcloneLimits.bwlimit)) {
      setNotice({ type: 'error', title: 'Invalid bandwidth limit', message: 'Bandwidth limit must be empty, off, or a value like 700M, 1G, or 500K.' });
      return;
    }
    if (rcloneLimits.tpslimit !== null && (!Number.isFinite(rcloneLimits.tpslimit) || rcloneLimits.tpslimit < 0 || rcloneLimits.tpslimit > 10000)) {
      setNotice({ type: 'error', title: 'Invalid TPS limit', message: 'TPS limit must be empty or a number between 0 and 10000.' });
      return;
    }
    const localRetention = normalizeLocalRetention(syncJob.local_retention);
    if (localRetention.enabled) {
      if (!selectedSyncSourceIsManagedLocal) {
        setNotice({ type: 'error', title: 'Invalid local cleanup', message: 'Local cleanup can only be enabled for managed server local folders.' });
        return;
      }
      if (!Number.isInteger(localRetention.delete_after_days) || localRetention.delete_after_days < 1 || localRetention.delete_after_days > 3650) {
        setNotice({ type: 'error', title: 'Invalid local cleanup', message: 'Retention days must be between 1 and 3650.' });
        return;
      }
      if (!Number.isInteger(localRetention.min_file_age_hours) || localRetention.min_file_age_hours < 1 || localRetention.min_file_age_hours > 720) {
        setNotice({ type: 'error', title: 'Invalid local cleanup', message: 'Minimum file age must be between 1 and 720 hours.' });
        return;
      }
      if (selectedSyncRetentionConflict) {
        setNotice({ type: 'error', title: 'Local cleanup conflict', message: `Job "${selectedSyncRetentionConflict.name}" already has local cleanup enabled for this source.` });
        return;
      }
    }
    setLoading(true);
    try {
      await api.post(`/save-job`, {
        ...syncJob,
        previous_name: editingJobName,
        metadata_tags: metadataTags,
        local_retention: localRetention,
        lifecycle_policy: createDefaultLifecyclePolicy(),
        bwlimit: rcloneLimits.bwlimit,
        tpslimit: rcloneLimits.tpslimit
      });
      if (editingJobName && editingJobName !== syncJob.name) {
        await api.delete(`/delete-job/${encodeURIComponent(editingJobName)}`);
      }
      showSuccess(editingJobName ? 'Job updated.' : 'Job saved.');
      setEditingJobName('');
      fetchJobs(); setView('datasync');
    } catch (err) { showError('Failed to save job', err); }
    setLoading(false);
  };

  const handleEditJob = async (job) => {
    const normalizedJob = {
      ...createDefaultSyncJob(),
      ...job,
      schedule: {
        ...createDefaultSyncJob().schedule,
        ...(job.schedule || {})
      },
      metadata_tags: normalizeMetadataTags(job.metadata_tags),
      local_retention: {
        ...DEFAULT_LOCAL_RETENTION,
        ...(job.local_retention || {})
      }
    };
    setEditingJobName(job.name);
    setSyncJob(normalizedJob);
    setView('builder');

    await Promise.all([
      loadSourceBuckets(remoteNameFromPath(normalizedJob.source_remote)),
      loadDestBuckets(normalizedJob.dest_profile)
    ]);
  };

  const handleDeleteJob = async (name) => {
    if (!window.confirm("Delete this job?")) return;
    try {
      await api.delete(`/delete-job/${encodeURIComponent(name)}`);
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
    } catch (err) { showError('Failed to start backup job', err); }
  };

  // --- Storage Explorer ---
  const handleStorageProfileChange = async (p) => { 
      setStorageProfile(p); setSelectedBucket(''); setStorageObjects([]); setBucketProtection(null); setBucketLifecycleForm(createDefaultLifecyclePolicy());
      if (!p) {
        setStorageBuckets([]);
        return;
      }
      try { const res = await api.get(`/list-buckets/${p}`); setStorageBuckets(res.data); } catch (err) { showError('Failed to list buckets', err); }
  };
  const handleBucketClick = async (b) => { 
      setSelectedBucket(b);
      try {
        const res = await api.get(`/list-objects/${storageProfile}/${b}`);
        setStorageObjects(res.data);
        await loadSelectedBucketSettings(storageProfile, b);
      } catch (err) { showError('Failed to list bucket objects', err); }
  };
  const handleCreateBucket = async () => {
      if (!newBucketName) return;
      try {
        await api.post(`/create-bucket`, {
          profile_name: storageProfile,
          bucket_name: newBucketName,
          storage_tier: newBucketConfig.storageTier,
          auto_tiering: newBucketConfig.storageTier === 'Standard' ? newBucketConfig.autoTiering : 'Disabled',
          versioning: newBucketConfig.versioning
        });
        setNewBucketName('');
        setNewBucketConfig(DEFAULT_NEW_BUCKET_CONFIG);
        handleStorageProfileChange(storageProfile);
        showSuccess('Bucket created.');
      } catch (err) { showError('Failed to create bucket', err); }
  };
  const addLifecycleRule = () => {
      setBucketLifecycleForm(prev => ({
        ...prev,
        enabled: true,
        rules: [...(Array.isArray(prev.rules) ? prev.rules : []), createLifecycleRule()]
      }));
  };
  const updateLifecycleRule = (index, updates) => {
      setBucketLifecycleForm(prev => ({
        ...prev,
        rules: (Array.isArray(prev.rules) ? prev.rules : []).map((rule, currentIndex) => {
          if (currentIndex !== index) return rule;
          const nextRule = { ...rule, ...updates };
          if (updates.target) {
            nextRule.action = normalizeLifecycleAction(nextRule.action, updates.target);
          }
          return nextRule;
        })
      }));
  };
  const removeLifecycleRule = (index) => {
      setBucketLifecycleForm(prev => ({
        ...prev,
        rules: (Array.isArray(prev.rules) ? prev.rules : []).filter((_, currentIndex) => currentIndex !== index)
      }));
  };
  const addLifecycleRuleFilter = (ruleIndex) => {
      setBucketLifecycleForm(prev => ({
        ...prev,
        rules: (Array.isArray(prev.rules) ? prev.rules : []).map((rule, currentIndex) => (
          currentIndex === ruleIndex
            ? { ...rule, filters: [...normalizeLifecycleFilters(rule), createLifecycleFilter()] }
            : rule
        ))
      }));
  };
  const updateLifecycleRuleFilter = (ruleIndex, filterIndex, updates) => {
      setBucketLifecycleForm(prev => ({
        ...prev,
        rules: (Array.isArray(prev.rules) ? prev.rules : []).map((rule, currentIndex) => (
          currentIndex === ruleIndex
            ? {
                ...rule,
                filters: normalizeLifecycleFilters(rule).map((filter, currentFilterIndex) => (
                  currentFilterIndex === filterIndex ? { ...filter, ...updates } : filter
                ))
              }
            : rule
        ))
      }));
  };
  const removeLifecycleRuleFilter = (ruleIndex, filterIndex) => {
      setBucketLifecycleForm(prev => ({
        ...prev,
        rules: (Array.isArray(prev.rules) ? prev.rules : []).map((rule, currentIndex) => (
          currentIndex === ruleIndex
            ? { ...rule, filters: normalizeLifecycleFilters(rule).filter((_, currentFilterIndex) => currentFilterIndex !== filterIndex) }
            : rule
        ))
      }));
  };
  const handleSaveBucketLifecyclePolicy = async () => {
      if (!storageProfile || !selectedBucket) return;
      setBucketLifecycleNotice(null);
      const lifecyclePolicy = normalizeLifecyclePolicy(bucketLifecycleForm);
      const lifecycleRules = lifecyclePolicy.rules || [];
      if (lifecyclePolicy.enabled && !lifecycleRules.length) {
        setBucketLifecycleNotice({ type: 'error', message: 'Create at least one lifecycle rule or disable lifecycle management.' });
        return;
      }
      if (lifecycleRules.some(rule => !rule.name)) {
        setBucketLifecycleNotice({ type: 'error', message: 'Each lifecycle rule needs a name.' });
        return;
      }
      const lifecycleRuleNames = lifecycleRules.map(rule => rule.name.toLowerCase());
      if (new Set(lifecycleRuleNames).size !== lifecycleRuleNames.length) {
        setBucketLifecycleNotice({ type: 'error', message: 'Lifecycle rule names must be unique.' });
        return;
      }
      if (lifecycleRules.some(rule => Number.isNaN(rule.days))) {
        setBucketLifecycleNotice({ type: 'error', message: 'Lifecycle days must be positive whole numbers.' });
        return;
      }
      if (lifecycleRules.some(rule => !Number.isInteger(rule.days) || rule.days < 1 || rule.days > 36500)) {
        setBucketLifecycleNotice({ type: 'error', message: 'Lifecycle days must be between 1 and 36500.' });
        return;
      }
      if (lifecycleRules.some(rule => normalizeLifecycleFilters(rule).length > 20)) {
        setBucketLifecycleNotice({ type: 'error', message: 'OCI allows at most 20 object name filters per lifecycle rule.' });
        return;
      }
      if (lifecycleRules.some(rule => normalizeLifecycleFilters(rule).some(filter => !filter.value))) {
        setBucketLifecycleNotice({ type: 'error', message: 'Lifecycle object name filters need a value.' });
        return;
      }
      if (lifecycleRules.some(rule => normalizeLifecycleFilters(rule).some(filter => filter.value.length > 1024 || /[\r\n\0]/.test(filter.value)))) {
        setBucketLifecycleNotice({ type: 'error', message: 'Lifecycle filter values must be single-line text up to 1024 characters.' });
        return;
      }
      setSavingBucketSettings(true);
      try {
        await api.put('/bucket-lifecycle-policy', {
          profile_name: storageProfile,
          bucket_name: selectedBucket,
          lifecycle_policy: lifecyclePolicy
        });
        setBucketLifecycleNotice({ type: 'success', message: 'Bucket lifecycle policy updated.' });
        await loadSelectedBucketSettings(storageProfile, selectedBucket);
      } catch (err) {
        console.error(err);
        if (err?.response?.status !== 401) {
          setBucketLifecycleNotice({ type: 'error', message: formatApiError(err, 'Failed to update bucket lifecycle policy.') });
        }
      } finally {
        setSavingBucketSettings(false);
      }
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
      setNotice(null);
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
      setNotice(null);
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

  const filteredVms = vms.filter(vm => {
    const query = searchTerm.trim().toLowerCase();
    if (!query) return true;

    const includeOcid = query.startsWith('ocid') || query.length >= 16;
    const searchText = [
      vm.name,
      vm.os,
      vm.shape,
      vm.public_ip,
      vm.private_ip,
      vm.state,
      vm.boot_volume?.name,
      vm.boot_volume?.id,
      ...(Array.isArray(vm.data_volumes) ? vm.data_volumes.flatMap(volume => [volume.name, volume.id]) : []),
      includeOcid ? vm.id : '',
    ].filter(Boolean).join(' ').toLowerCase();
    return searchText.includes(query);
  });

  const getStatusColor = (status) => {
    if (status === 'SUCCESS') return 'text-green-500';
    if (status === 'FAILURE') return 'text-red-500';
    if (status === 'PROGRESS') return 'text-blue-500';
    return 'text-orange-500';
  };
  const autoTieringBlockedByLifecycle = Boolean(
    bucketProtection
    && !bucketProtection.auto_tiering_enabled
    && bucketProtection.has_infrequent_access_lifecycle_rule
  );
  const currentViewMeta = VIEW_META[view] || VIEW_META.datasync;
  const healthChecks = Object.values(health?.checks || {});
  const healthIssueCount = healthChecks.filter(check => check?.status !== 'ok').length;
  const updateAvailable = upgradeCheck?.up_to_date === false;
  const upgradeRunning = upgradeStatus?.status === 'running';
  const navigationGroups = [
    {
      label: 'Operations',
      items: [
        { id: 'datasync', label: 'Job Dashboard', icon: LayoutDashboard },
        { id: 'jobs', label: 'Backup Jobs', icon: Activity },
        { id: 'builder', label: 'New Backup Job', icon: Plus }
      ]
    },
    {
      label: 'Infrastructure',
      items: [
        { id: 'keys', label: 'Credentials', icon: Key },
        { id: 'storage', label: 'OCI Object Storage', icon: Archive },
        { id: 'explorer', label: 'VM Image Migration', icon: Database }
      ]
    },
    {
      label: 'System',
      items: [
        { id: 'settings', label: 'Settings', icon: Settings }
      ]
    }
  ];

  const handleNavigate = (targetView) => {
    if (targetView === 'builder') {
      startNewSyncJob();
    } else {
      setView(targetView);
    }
    setSidebarOpen(false);
    window.requestAnimationFrame(() => mainScrollRef.current?.scrollTo({ top: 0, left: 0 }));
  };

  if (!isAuthenticated) {
    return (
      <div data-theme={theme} className="min-h-screen bg-[#f5f6f8] flex items-center justify-center p-5 sm:p-8 font-sans">
        <div className="login-shell grid min-h-[560px] w-full max-w-7xl overflow-hidden rounded-2xl border border-gray-200 bg-white lg:grid-cols-[0.7fr_1.3fr]">
          <section className="login-form-panel flex min-h-[500px] items-center justify-center px-7 py-12 sm:px-12 lg:px-14">
            <form onSubmit={handleLogin} className="w-full max-w-sm text-left">
              <div className="mb-10 flex items-center gap-3">
                <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-[#9c3029] shadow-sm">
                  <Lock size={19} className="text-white" />
                </div>
                <div>
                  <h1 className="text-lg font-bold tracking-tight text-gray-900">OCI Migrator Pro</h1>
                  <p className="text-xs text-gray-500">Cloud migration console</p>
                </div>
              </div>
              <div className="mb-7">
                <p className="text-sm text-gray-500">Welcome back</p>
                <h2 className="mt-1 text-2xl font-bold tracking-tight text-gray-900">Sign in to your dashboard</h2>
              </div>
              <div className="space-y-4">
                <div>
                  <label className="mb-1.5 block text-xs font-semibold text-gray-700">Username</label>
                  <input
                    value={loginForm.username}
                    onChange={e => setLoginForm({ ...loginForm, username: e.target.value })}
                    className="w-full rounded-lg border border-gray-200 bg-white px-3.5 py-3 text-sm text-gray-800 outline-none transition focus:border-[#9c3029] focus:ring-4 focus:ring-[#9c3029]/10"
                    autoComplete="username"
                    required
                  />
                </div>
                <div>
                  <label className="mb-1.5 block text-xs font-semibold text-gray-700">Password</label>
                  <input
                    type="password"
                    value={loginForm.password}
                    onChange={e => setLoginForm({ ...loginForm, password: e.target.value })}
                    className="w-full rounded-lg border border-gray-200 bg-white px-3.5 py-3 text-sm text-gray-800 outline-none transition focus:border-[#9c3029] focus:ring-4 focus:ring-[#9c3029]/10"
                    autoComplete="current-password"
                    required
                  />
                </div>
                {loginError && <div className="rounded-lg border border-red-100 bg-red-50 p-3 text-xs text-red-700">{loginError}</div>}
                <button type="submit" disabled={authLoading} className="flex w-full items-center justify-center gap-2 rounded-lg bg-[#9c3029] py-3 text-sm font-semibold text-white shadow-sm transition hover:bg-[#7a2520] disabled:cursor-not-allowed disabled:opacity-70">
                  {authLoading ? <Loader2 className="animate-spin" size={18} /> : <><Lock size={16} /> Sign in</>}
                </button>
              </div>
            </form>
          </section>
          <aside className="login-hero relative hidden overflow-hidden bg-[#061b38] lg:block">
            <img src={loginHeroImage} alt="Secure cloud migration from servers to cloud infrastructure" className="absolute inset-0 h-full w-full object-cover object-center" />
            <div className="absolute inset-0 bg-[#03142b]/20" />
            <div className="absolute right-7 top-7 inline-flex items-center gap-2 rounded-full border border-white/25 bg-[#061b38]/75 px-3 py-1.5 text-[10px] font-bold uppercase text-white backdrop-blur-sm">
              <Shield size={13} /> Self-hosted console
            </div>
            <div className="absolute inset-x-0 bottom-0 border-t border-white/15 bg-[#041224]/90 px-8 py-7 text-left text-white backdrop-blur-sm xl:px-10">
              <div className="flex items-end justify-between gap-8">
                <div className="flex min-w-0 items-start gap-3">
                  <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md border border-white/25 bg-white/10"><Cloud size={19} /></div>
                  <div>
                    <p className="text-xl font-semibold">Securely move what matters.</p>
                    <p className="mt-1 max-w-lg text-sm leading-5 text-blue-100/80">Orchestrate, monitor, and control migration workflows from one console.</p>
                  </div>
                </div>
                <div className="hidden shrink-0 grid-cols-2 gap-5 xl:grid">
                  <div className="border-l border-white/20 pl-4">
                    <FileText size={16} className="mb-2 text-blue-200" />
                    <p className="text-[10px] font-bold uppercase text-blue-100/65">Data</p>
                    <p className="mt-0.5 text-xs font-semibold">Files & objects</p>
                  </div>
                  <div className="border-l border-white/20 pl-4">
                    <Database size={16} className="mb-2 text-blue-200" />
                    <p className="text-[10px] font-bold uppercase text-blue-100/65">Compute</p>
                    <p className="mt-0.5 text-xs font-semibold">VM boot images</p>
                  </div>
                </div>
              </div>
            </div>
          </aside>
        </div>
      </div>
    );
  }

  return (
    <div data-theme={theme} className="app-shell flex h-screen overflow-hidden font-sans text-gray-800">
      {confirmDialog && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-gray-950/45 p-4">
          <div className="w-full max-w-lg bg-white border border-gray-200 rounded-md shadow-2xl">
            <div className="flex items-start gap-4 p-5 border-b border-gray-100 text-left">
              <div className="mt-0.5 h-10 w-10 rounded-md bg-red-50 border border-red-100 flex items-center justify-center text-[#9c3029] shrink-0">
                {confirmDialog.icon === 'shield' ? <Shield size={20} /> : <Archive size={20} />}
              </div>
              <div className="min-w-0 flex-1">
                <h3 className="text-base font-bold text-gray-900">{confirmDialog.title}</h3>
                <p className="mt-1 text-sm text-gray-600 leading-6">{confirmDialog.message}</p>
                {confirmDialog.detail && (
                  <p className="mt-2 text-xs text-gray-500 leading-5">{confirmDialog.detail}</p>
                )}
              </div>
              <button
                type="button"
                onClick={closeConfirmDialog}
                className="p-1.5 text-gray-400 hover:text-gray-700 hover:bg-gray-50 rounded-md"
                title="Close"
                aria-label="Close"
              >
                <X size={16} />
              </button>
            </div>
            <div className="flex items-center justify-end gap-3 p-4 bg-gray-50 rounded-b-md">
              <button
                type="button"
                onClick={closeConfirmDialog}
                className="px-4 py-2 bg-white border border-gray-200 rounded-md text-sm font-semibold text-gray-700 hover:bg-gray-100"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={confirmDialogAction}
                className="px-4 py-2 bg-[#9c3029] text-white rounded-md text-sm font-semibold hover:bg-[#7a2520] shadow-sm"
              >
                {confirmDialog.confirmLabel || 'Confirm'}
              </button>
            </div>
          </div>
        </div>
      )}
      {sidebarOpen && (
        <button
          type="button"
          className="fixed inset-0 z-30 bg-gray-950/45 lg:hidden"
          onClick={() => setSidebarOpen(false)}
          aria-label="Close navigation"
        />
      )}

      <nav className={`app-sidebar fixed inset-y-0 left-0 z-40 flex w-[248px] flex-col overflow-hidden border-r px-4 py-5 transition-transform lg:static lg:translate-x-0 ${sidebarOpen ? 'translate-x-0' : '-translate-x-full'}`}>
        <div className="flex items-center justify-between gap-3 px-2 pb-6">
          <div className="flex min-w-0 items-center gap-3">
            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-[#a9342d] shadow-sm"><Cpu size={19} className="text-white" /></div>
            <div className="min-w-0">
              <h1 className="truncate text-sm font-bold text-white">OCI Migrator Pro</h1>
              <p className="mt-0.5 text-[11px] text-gray-500">Backup operations</p>
            </div>
          </div>
          <button type="button" onClick={() => setSidebarOpen(false)} className="app-sidebar-icon mobile-only" title="Close navigation"><X size={17} /></button>
        </div>

        <div className="min-h-0 flex-1 space-y-5 overflow-y-auto pb-5">
          {navigationGroups.map(group => (
            <div key={group.label}>
              <div className="px-3 pb-1.5 text-[10px] font-bold uppercase text-gray-600">{group.label}</div>
              <div className="space-y-1">
                {group.items.map(item => {
                  const Icon = item.icon;
                  const isActive = view === item.id;
                  return (
                    <button
                      key={item.id}
                      type="button"
                      onClick={() => handleNavigate(item.id)}
                      className={`app-nav-link ${isActive ? 'is-active' : ''}`}
                    >
                      <Icon size={17} />
                      <span>{item.label}</span>
                    </button>
                  );
                })}
              </div>
            </div>
          ))}
        </div>

        <div className="shrink-0 border-t border-white/10 pt-4">
          <div className="mb-3 flex items-center gap-3 px-2">
            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-[#a9342d] text-xs font-bold text-white">
              {(authState.username || 'admin').slice(0, 1).toUpperCase()}
            </div>
            <div className="min-w-0">
              <div className="truncate text-sm font-semibold text-white">{authState.username}</div>
              <div className="text-[11px] text-gray-500">Administrator</div>
            </div>
          </div>
          <button
            type="button"
            onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
            role="switch"
            aria-checked={theme === 'dark'}
            className="app-theme-toggle"
            title="Toggle color theme"
          >
            <span className="flex items-center gap-2 text-xs font-semibold">
              {theme === 'dark' ? <Moon size={15} /> : <Sun size={15} />}
              {theme === 'dark' ? 'Dark mode' : 'Light mode'}
            </span>
            <span className={`relative h-5 w-9 rounded-full transition-colors ${theme === 'dark' ? 'bg-gray-300' : 'bg-gray-600'}`}>
              <span className={`absolute left-0 top-0.5 h-4 w-4 rounded-full bg-white shadow-sm transition-transform ${theme === 'dark' ? 'translate-x-[18px]' : 'translate-x-0.5'}`} />
            </span>
          </button>
          <button type="button" onClick={handleLogout} className="app-nav-link mt-1" title="Log out">
            <LogOut size={17} />
            <span>Log out</span>
          </button>
        </div>
      </nav>

      <main ref={mainScrollRef} className="app-main relative flex h-screen min-w-0 flex-1 flex-col overflow-y-auto">
        <header className="app-topbar sticky top-0 z-20 flex h-16 shrink-0 items-center justify-between border-b px-4 sm:px-6 xl:px-8">
          <div className="flex min-w-0 items-center gap-3">
            <button type="button" onClick={() => setSidebarOpen(true)} className="ui-icon-button mobile-only" title="Open navigation"><Menu size={18} /></button>
            <div className="min-w-0">
              <div className="text-[10px] font-semibold uppercase text-gray-400">{currentViewMeta.group}</div>
              <div className="truncate text-sm font-bold text-gray-900">{currentViewMeta.title}</div>
            </div>
          </div>
          <button onClick={fetchHealth} className={`h-9 px-3 border rounded-md text-xs font-semibold flex items-center gap-2 ${!health?.status ? 'border-gray-200 text-gray-600 bg-white' : health.status === 'ok' ? 'border-green-200 text-green-700 bg-green-50' : health.status === 'warn' ? 'border-amber-200 text-amber-700 bg-amber-50' : 'border-red-200 text-red-700 bg-red-50'}`} title="Refresh health status">
            <HeartPulse size={15} />
            <span className="hidden sm:inline">System</span>
            {health?.status || 'health'}
          </button>
        </header>

        {notice && (
          <div className="fixed right-4 top-20 z-30 flex w-[min(420px,calc(100vw-2rem))] items-start gap-3 rounded-md border border-gray-200 bg-white p-4 text-left shadow-xl sm:right-6">
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

        <div className="min-h-screen p-4 pb-32 sm:p-6 sm:pb-32 xl:p-8 xl:pb-40">
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
                          <option value="oci">Oracle Object Storage (OCI)</option>
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
                                <select value={remoteConfig.localMode} onChange={e => setRemoteConfig({...remoteConfig, localMode: e.target.value, localShareAccess: e.target.value === 'server_folder' ? remoteConfig.localShareAccess : 'none', localNfsEnabled: e.target.value === 'server_folder' ? remoteConfig.localNfsEnabled : false})} className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm focus:outline-none focus:border-[#9c3029]">
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
                                  <label className="flex items-center gap-3 rounded-md border border-gray-200 bg-white px-3 py-2 text-sm font-semibold text-gray-700">
                                    <input
                                      type="checkbox"
                                      checked={remoteConfig.localNfsEnabled}
                                      onChange={e => setRemoteConfig({...remoteConfig, localNfsEnabled: e.target.checked})}
                                      className="h-4 w-4 rounded border-gray-300 text-[#9c3029] focus:ring-[#9c3029]"
                                    />
                                    Enable NFSv4 Share
                                  </label>
                                  {remoteConfig.localShareAccess !== 'none' || remoteConfig.localNfsEnabled ? (
                                    <div>
                                      <label className="text-[11px] font-bold text-gray-500 uppercase tracking-wider mb-1 block">Share Name</label>
                                      <input value={remoteConfig.localShareName} onChange={e => setRemoteConfig({...remoteConfig, localShareName: e.target.value})} placeholder={remoteConfig.localFolderName || remoteConfig.name || 'customer-a'} className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm focus:outline-none focus:border-[#9c3029]" />
                                      <div className="mt-1 text-[10px] text-gray-500">
                                        {remoteConfig.localShareAccess !== 'none' && 'SMB opens TCP 445. '}
                                        {remoteConfig.localNfsEnabled && 'NFSv4 opens TCP 2049.'}
                                      </div>
                                    </div>
                                  ) : null}
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
                                  {remoteConfig.localNfsEnabled && (
                                    <div>
                                      <label className="text-[11px] font-bold text-gray-500 uppercase tracking-wider mb-1 block">Allowed NFS Clients</label>
                                      <input value={remoteConfig.localNfsClients} onChange={e => setRemoteConfig({...remoteConfig, localNfsClients: e.target.value})} placeholder="10.0.0.0/24, 10.0.1.25" className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm font-mono focus:outline-none focus:border-[#9c3029]" />
                                      <div className="mt-1 text-[10px] text-gray-500">Use client IPs, hostnames, or CIDR ranges. Wildcards are blocked.</div>
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
                                    {remote.nfs_share_name && <div className="text-[10px] text-gray-400 truncate">NFSv4: {remote.nfs_share_name}</div>}
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

          {/* VIEW: SETTINGS */}
          {view === 'settings' && (
            <div className="max-w-7xl mx-auto space-y-6 animate-in fade-in">
              <h2 className="text-xl font-bold mb-6 flex items-center gap-2 text-gray-800"><Settings size={24} className="text-[#9c3029]"/> Settings</h2>
              <div className="bg-white border border-gray-200 rounded-md shadow-sm p-4 text-left">
                <div className="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-4 mb-4">
                  <h3 className="font-bold text-sm text-gray-800 flex items-center gap-2">
                    <Download size={16} className="text-[#9c3029]" /> System Upgrade
                  </h3>
                  <div className="flex flex-wrap gap-2">
                    <button
                      type="button"
                      onClick={handleCheckUpgrade}
                      disabled={checkingUpgrade || upgradeStatus?.status === 'running'}
                      className="px-3 py-2 bg-white border border-gray-200 text-gray-600 rounded-md font-semibold text-xs hover:text-[#9c3029] hover:bg-gray-50 disabled:opacity-60 flex items-center gap-2"
                    >
                      {checkingUpgrade ? <Loader2 className="animate-spin" size={14} /> : <RefreshCw size={14} />}
                      Check
                    </button>
                    <button
                      type="button"
                      onClick={handleStartUpgrade}
                      disabled={startingUpgrade || upgradeStatus?.status === 'running' || upgradeStatus?.helper_installed === false}
                      className="px-3 py-2 bg-[#9c3029] text-white rounded-md font-semibold text-xs shadow-sm hover:bg-[#7a2520] disabled:opacity-60 flex items-center gap-2"
                    >
                      {startingUpgrade || upgradeStatus?.status === 'running' ? <Loader2 className="animate-spin" size={14} /> : <Download size={14} />}
                      Upgrade
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        const next = !showUpgradeLog;
                        setShowUpgradeLog(next);
                        if (next) fetchUpgradeLog();
                      }}
                      className="px-3 py-2 bg-white border border-gray-200 text-gray-600 rounded-md font-semibold text-xs hover:text-[#9c3029] hover:bg-gray-50 flex items-center gap-2"
                    >
                      <Terminal size={14} />
                      Log
                    </button>
                  </div>
                </div>
                <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                  <div className="bg-gray-50 border border-gray-100 rounded-md p-3">
                    <div className="text-[10px] font-bold text-gray-400 uppercase tracking-wider mb-1">Installed</div>
                    <div className="text-sm font-mono text-gray-800">{upgradeStatus?.current_short || 'unknown'}</div>
                    <div className="text-[11px] text-gray-500 truncate mt-1">{upgradeStatus?.branch || 'main'}</div>
                  </div>
                  <div className="bg-gray-50 border border-gray-100 rounded-md p-3">
                    <div className="text-[10px] font-bold text-gray-400 uppercase tracking-wider mb-1">Latest GitHub</div>
                    <div className="text-sm font-mono text-gray-800">{latestUpgradeShort || 'not checked'}</div>
                    <div className="text-[11px] text-gray-500 truncate mt-1">{upgradeIsCurrent ? 'You are on the latest version.' : upgradeCheck?.up_to_date === false ? 'A new version is available.' : 'Run check when needed'}</div>
                  </div>
                  <div className="bg-gray-50 border border-gray-100 rounded-md p-3">
                    <div className="text-[10px] font-bold text-gray-400 uppercase tracking-wider mb-1">Status</div>
                    <span className={`inline-flex text-[10px] px-2 py-0.5 rounded-full border font-bold uppercase ${runStatusClass(upgradeStatus?.status || 'idle')}`}>
                      {upgradeStatus?.status || 'idle'}
                    </span>
                    <div className="text-[11px] text-gray-500 truncate mt-2" title={upgradeStatus?.message || ''}>{upgradeStatus?.message || 'No upgrade has run yet.'}</div>
                  </div>
                </div>
                {upgradeStatus?.helper_installed === false && (
                  <div className="mt-3 text-xs text-amber-700 bg-amber-50 border border-amber-100 rounded-md p-3">
                    Upgrade helper is missing. Rerun ./install.sh once on the server to enable dashboard upgrades.
                  </div>
                )}
                {showUpgradeLog && (
                  <div className="mt-4 bg-gray-900 border border-gray-800 rounded-md p-4 shadow-inner">
                    {upgradeReconnecting && (
                      <div className="mb-3 flex items-center gap-2 text-[11px] font-semibold text-blue-300">
                        <Loader2 className="animate-spin" size={13} />
                        Service is restarting. Reconnecting automatically...
                      </div>
                    )}
                    <pre className="text-[11px] font-mono text-gray-300 h-44 overflow-y-auto text-left whitespace-pre-wrap">
                      {upgradeLog || (upgradeReconnecting ? 'Waiting for the upgrade service to return...' : 'No upgrade log yet.')}
                    </pre>
                  </div>
                )}
              </div>
              {authState.mode === 'session' && (
                <form onSubmit={handleChangePassword} className="bg-white border border-gray-200 rounded-md shadow-sm p-4 text-left">
                  <div className="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-4 mb-4">
                    <div>
                      <h3 className="font-bold text-sm text-gray-800 flex items-center gap-2"><Lock size={16} className="text-[#9c3029]" /> Change Password</h3>
                      <p className="mt-1 text-[11px] text-gray-500">Update the admin password used for this web console.</p>
                    </div>
                    <button
                      type="submit"
                      disabled={authLoading}
                      className="px-3 py-2 bg-[#9c3029] text-white rounded-md font-semibold text-xs shadow-sm hover:bg-[#7a2520] disabled:opacity-60 flex items-center justify-center gap-2"
                    >
                      {authLoading ? <Loader2 className="animate-spin" size={14} /> : <Save size={14} />}
                      Save Password
                    </button>
                  </div>
                  <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
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
                  </div>
                  {passwordMessage && (
                    <div className={`mt-3 text-xs rounded-md p-2 border ${passwordMessage === 'Password changed.' ? 'text-green-700 bg-green-50 border-green-100' : 'text-red-600 bg-red-50 border-red-100'}`}>
                      {passwordMessage}
                    </div>
                  )}
                </form>
              )}
              <div className="bg-white border border-gray-200 rounded-md shadow-sm p-4 text-left">
                <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-4">
                  <div className="min-w-0">
                    <h3 className="font-bold text-sm text-gray-800 flex items-center gap-2">
                      <Archive size={16} className="text-[#9c3029]" /> Runtime Config Backup
                    </h3>
                    <div className="mt-2 grid grid-cols-1 sm:grid-cols-3 gap-2 text-[11px] text-gray-500">
                      <span className="bg-gray-50 border border-gray-100 rounded-md px-2 py-1 font-mono truncate">~/.oci-migrator.env</span>
                      <span className="bg-gray-50 border border-gray-100 rounded-md px-2 py-1 font-mono truncate">~/.oci/config</span>
                      <span className="bg-gray-50 border border-gray-100 rounded-md px-2 py-1 font-mono truncate">~/.config/rclone/rclone.conf</span>
                    </div>
                    <p className="mt-2 text-[11px] text-amber-700">The ZIP file contains secrets and should be stored securely.</p>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <button
                      type="button"
                      onClick={handleExportRuntimeConfig}
                      disabled={exportingConfig || importingConfig}
                      className="px-3 py-2 bg-[#9c3029] text-white rounded-md font-semibold text-xs shadow-sm hover:bg-[#7a2520] disabled:opacity-60 flex items-center justify-center gap-2"
                    >
                      {exportingConfig ? <Loader2 className="animate-spin" size={14} /> : <Download size={14} />}
                      Export Backup
                    </button>
                    <label className={`px-3 py-2 bg-white border border-gray-200 text-gray-600 rounded-md font-semibold text-xs hover:text-[#9c3029] hover:bg-gray-50 flex items-center justify-center gap-2 ${importingConfig ? 'opacity-60 pointer-events-none' : 'cursor-pointer'}`}>
                      {importingConfig ? <Loader2 className="animate-spin" size={14} /> : <Upload size={14} />}
                      Import Backup
                      <input
                        type="file"
                        accept=".zip,application/zip"
                        onChange={handleImportRuntimeConfig}
                        disabled={importingConfig || exportingConfig}
                        className="hidden"
                      />
                    </label>
                  </div>
                </div>
                <p className="mt-3 text-[11px] text-gray-500">
                  Import restores runtime env, OCI config, rclone config, backup jobs, job history, and bundled key files. A pre-restore backup is created automatically.
                </p>
              </div>
              <form onSubmit={handleSaveNetworkSettings} className="bg-white border border-gray-200 rounded-md shadow-sm p-4 text-left">
                <div className="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-4 mb-4">
                  <div>
                    <h3 className="font-bold text-sm text-gray-800 flex items-center gap-2">
                      <Network size={16} className="text-[#9c3029]" /> Network
                    </h3>
                    <p className="mt-1 text-[11px] text-gray-500">IPv4 configuration is managed with Netplan. DHCP remains the default until a static address is applied.</p>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <button
                      type="button"
                      onClick={() => fetchNetworkSettings(true)}
                      disabled={savingNetworkSettings}
                      className="px-3 py-2 bg-white border border-gray-200 text-gray-600 rounded-md font-semibold text-xs hover:text-[#9c3029] hover:bg-gray-50 disabled:opacity-60 flex items-center gap-2"
                    >
                      <RefreshCw size={14} />
                      Refresh
                    </button>
                    <button
                      type="submit"
                      disabled={savingNetworkSettings || networkSettings?.helper_installed === false || Boolean(networkSettings?.pending)}
                      className="px-3 py-2 bg-[#9c3029] text-white rounded-md font-semibold text-xs shadow-sm hover:bg-[#7a2520] disabled:opacity-60 flex items-center justify-center gap-2"
                    >
                      {savingNetworkSettings ? <Loader2 className="animate-spin" size={14} /> : <Save size={14} />}
                      Apply Network
                    </button>
                  </div>
                </div>

                <div className="grid grid-cols-1 md:grid-cols-[220px_1fr] gap-3">
                  <div>
                    <label className="text-[10px] font-bold text-gray-500 uppercase tracking-wider mb-1 block">IPv4 Mode</label>
                    <div className="grid grid-cols-2 rounded-md border border-gray-200 bg-gray-50 p-1">
                      <button
                        type="button"
                        onClick={() => setNetworkSettingsForm({ ...networkSettingsForm, mode: 'dhcp' })}
                        className={`px-3 py-1.5 rounded text-xs font-semibold transition-colors ${networkSettingsForm.mode === 'dhcp' ? 'bg-white text-[#9c3029] shadow-sm' : 'text-gray-500 hover:text-gray-800'}`}
                      >
                        DHCP
                      </button>
                      <button
                        type="button"
                        onClick={() => setNetworkSettingsForm({ ...networkSettingsForm, mode: 'static' })}
                        className={`px-3 py-1.5 rounded text-xs font-semibold transition-colors ${networkSettingsForm.mode === 'static' ? 'bg-white text-[#9c3029] shadow-sm' : 'text-gray-500 hover:text-gray-800'}`}
                      >
                        Static IPv4
                      </button>
                    </div>
                  </div>
                  <div>
                    <label className="text-[10px] font-bold text-gray-500 uppercase tracking-wider mb-1 block">Network Interface</label>
                    <select
                      value={networkSettingsForm.interface}
                      onChange={event => {
                        const selected = (networkSettings?.interfaces || []).find(item => item.name === event.target.value);
                        const [address = '', prefixLength = '24'] = (selected?.ipv4_addresses?.[0] || '').split('/');
                        setNetworkSettingsForm({
                          ...networkSettingsForm,
                          interface: event.target.value,
                          address,
                          prefixLength: Number(prefixLength || 24),
                          gateway: selected?.gateway || '',
                          dnsServers: (selected?.dns_servers || []).join(' ')
                        });
                      }}
                      className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm font-mono focus:outline-none focus:border-[#9c3029]"
                    >
                      {(networkSettings?.interfaces || []).map(item => (
                        <option key={item.name} value={item.name}>{item.name} ({item.state})</option>
                      ))}
                    </select>
                  </div>
                </div>

                {networkSettingsForm.mode === 'static' && (
                  <div className="mt-3 grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-[1fr_120px_1fr_1.4fr] gap-3">
                    <div>
                      <label className="text-[10px] font-bold text-gray-500 uppercase tracking-wider mb-1 block">IPv4 Address</label>
                      <input
                        value={networkSettingsForm.address}
                        onChange={event => setNetworkSettingsForm({ ...networkSettingsForm, address: event.target.value.trim() })}
                        placeholder="10.0.1.20"
                        inputMode="decimal"
                        className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm font-mono focus:outline-none focus:border-[#9c3029]"
                      />
                    </div>
                    <div>
                      <label className="text-[10px] font-bold text-gray-500 uppercase tracking-wider mb-1 block">Prefix</label>
                      <input
                        type="number"
                        min="1"
                        max="32"
                        value={networkSettingsForm.prefixLength}
                        onChange={event => setNetworkSettingsForm({ ...networkSettingsForm, prefixLength: event.target.value })}
                        className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm font-mono focus:outline-none focus:border-[#9c3029]"
                      />
                    </div>
                    <div>
                      <label className="text-[10px] font-bold text-gray-500 uppercase tracking-wider mb-1 block">Gateway</label>
                      <input
                        value={networkSettingsForm.gateway}
                        onChange={event => setNetworkSettingsForm({ ...networkSettingsForm, gateway: event.target.value.trim() })}
                        placeholder="10.0.1.1"
                        inputMode="decimal"
                        className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm font-mono focus:outline-none focus:border-[#9c3029]"
                      />
                    </div>
                    <div>
                      <label className="text-[10px] font-bold text-gray-500 uppercase tracking-wider mb-1 block">DNS Servers</label>
                      <input
                        value={networkSettingsForm.dnsServers}
                        onChange={event => setNetworkSettingsForm({ ...networkSettingsForm, dnsServers: event.target.value })}
                        placeholder="1.1.1.1 8.8.8.8"
                        className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm font-mono focus:outline-none focus:border-[#9c3029]"
                      />
                    </div>
                  </div>
                )}

                <div className="mt-3 grid grid-cols-1 sm:grid-cols-3 gap-2 text-[11px]">
                  <div className="bg-gray-50 border border-gray-100 rounded-md px-2 py-1">
                    <span className="font-bold text-gray-400 uppercase">Current Mode</span>
                    <div className="font-semibold text-gray-700 uppercase">{networkSettings?.mode || 'unknown'}</div>
                  </div>
                  <div className="bg-gray-50 border border-gray-100 rounded-md px-2 py-1 min-w-0">
                    <span className="font-bold text-gray-400 uppercase">Current IPv4</span>
                    <div className="font-mono text-gray-700 truncate">
                      {(networkSettings?.interfaces || []).find(item => item.name === networkSettingsForm.interface)?.ipv4_addresses?.join(', ') || 'Not assigned'}
                    </div>
                  </div>
                  <div className="bg-gray-50 border border-gray-100 rounded-md px-2 py-1 min-w-0">
                    <span className="font-bold text-gray-400 uppercase">Default Gateway</span>
                    <div className="font-mono text-gray-700 truncate">
                      {(networkSettings?.interfaces || []).find(item => item.name === networkSettingsForm.interface)?.gateway || 'Not detected'}
                    </div>
                  </div>
                </div>

                {networkSettings?.pending && (
                  <div className="mt-4 border border-amber-200 bg-amber-50 rounded-md p-4">
                    <div className="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-4">
                      <div className="min-w-0">
                        <div className="flex items-center gap-2 text-sm font-bold text-amber-900">
                          <AlertCircle size={16} /> Pending Confirmation
                        </div>
                        <p className="mt-1 text-xs text-amber-800">
                          {networkSettings.pending.interface} is testing {networkSettings.pending.mode === 'static' ? networkSettings.pending.address : 'DHCP'}.
                          {' '}Automatic rollback in {Math.max(0, Math.ceil((Number(networkSettings.pending.rollback_at || 0) * 1000 - networkNow) / 1000))} seconds.
                        </p>
                        <div className="mt-2 flex flex-wrap items-center gap-2 text-[11px]">
                          <span className={`px-2 py-1 rounded border font-semibold ${networkSettings.pending.target_active ? 'bg-green-50 border-green-200 text-green-700' : 'bg-white border-amber-200 text-amber-800'}`}>
                            {networkSettings.pending.target_active ? 'Target active' : 'Waiting for target'}
                          </span>
                          {networkSettings.pending.mode === 'static' && networkSettings.pending.address && (
                            <a
                              href={`${window.location.protocol}//${networkSettings.pending.address.split('/')[0]}:${window.location.port || '8000'}`}
                              className="font-mono text-[#9c3029] hover:underline"
                            >
                              Open {networkSettings.pending.address.split('/')[0]}
                            </a>
                          )}
                        </div>
                      </div>
                      <div className="flex flex-wrap gap-2 shrink-0">
                        <button
                          type="button"
                          onClick={handleRollbackNetworkSettings}
                          disabled={savingNetworkSettings}
                          className="px-3 py-2 bg-white border border-amber-200 text-amber-900 rounded-md text-xs font-semibold hover:bg-amber-100 disabled:opacity-60"
                        >
                          Roll Back Now
                        </button>
                        <button
                          type="button"
                          onClick={handleConfirmNetworkSettings}
                          disabled={savingNetworkSettings || !networkSettings.pending.target_active || networkNow - Number(networkSettings.pending.created_at || 0) * 1000 < 5000}
                          className="px-3 py-2 bg-[#9c3029] text-white rounded-md text-xs font-semibold hover:bg-[#7a2520] disabled:opacity-60 flex items-center gap-2"
                        >
                          {savingNetworkSettings ? <Loader2 className="animate-spin" size={14} /> : <CheckCircle size={14} />}
                          Confirm Network
                        </button>
                      </div>
                    </div>
                  </div>
                )}

                {networkSettings?.helper_installed === false && (
                  <div className="mt-3 text-xs text-amber-700 bg-amber-50 border border-amber-100 rounded-md p-3">
                    Network helper is missing. Rerun ./install.sh once on the server to enable dashboard network settings.
                  </div>
                )}
              </form>

              <form onSubmit={handleSaveTimeSettings} className="bg-white border border-gray-200 rounded-md shadow-sm p-4 text-left">
                <div className="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-4 mb-4">
                  <div>
                    <h3 className="font-bold text-sm text-gray-800 flex items-center gap-2">
                      <Clock size={16} className="text-[#9c3029]" /> Time & NTP
                    </h3>
                    <p className="mt-1 text-[11px] text-gray-500">Controls the server timezone and systemd-timesyncd NTP servers used for schedules and timestamps.</p>
                  </div>
                  <button
                    type="submit"
                    disabled={savingTimeSettings || timeSettings?.helper_installed === false}
                    className="px-3 py-2 bg-[#9c3029] text-white rounded-md font-semibold text-xs shadow-sm hover:bg-[#7a2520] disabled:opacity-60 flex items-center justify-center gap-2"
                  >
                    {savingTimeSettings ? <Loader2 className="animate-spin" size={14} /> : <Save size={14} />}
                    Save Time
                  </button>
                </div>
                <div className="grid grid-cols-1 md:grid-cols-[240px_1fr] gap-3">
                  <div>
                    <label className="text-[10px] font-bold text-gray-500 uppercase tracking-wider mb-1 block">Timezone</label>
                    <input
                      value={timeSettingsForm.timezone}
                      onChange={e => setTimeSettingsForm({ ...timeSettingsForm, timezone: e.target.value })}
                      placeholder="Asia/Singapore"
                      className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm font-mono focus:outline-none focus:border-[#9c3029]"
                    />
                  </div>
                  <div>
                    <label className="text-[10px] font-bold text-gray-500 uppercase tracking-wider mb-1 block">NTP Servers</label>
                    <input
                      value={timeSettingsForm.ntpServers}
                      onChange={e => setTimeSettingsForm({ ...timeSettingsForm, ntpServers: e.target.value })}
                      placeholder="0.sg.pool.ntp.org 1.sg.pool.ntp.org"
                      className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm font-mono focus:outline-none focus:border-[#9c3029]"
                    />
                  </div>
                </div>
                <div className="mt-3 grid grid-cols-1 md:grid-cols-4 gap-2 text-[11px]">
                  <div className="bg-gray-50 border border-gray-100 rounded-md px-2 py-1">
                    <span className="font-bold text-gray-400 uppercase">Current</span>
                    <div className="font-mono text-gray-700 truncate">{timeSettings?.timezone || 'unknown'}</div>
                  </div>
                  <div className="bg-gray-50 border border-gray-100 rounded-md px-2 py-1">
                    <span className="font-bold text-gray-400 uppercase">NTP</span>
                    <div className={timeSettings?.ntp_synchronized ? 'text-green-700 font-semibold' : 'text-amber-700 font-semibold'}>
                      {timeSettings?.ntp_synchronized ? 'Synchronized' : 'Not synchronized'}
                    </div>
                  </div>
                  <div className="bg-gray-50 border border-gray-100 rounded-md px-2 py-1">
                    <span className="font-bold text-gray-400 uppercase">Service</span>
                    <div className={timeSettings?.ntp_enabled ? 'text-green-700 font-semibold' : 'text-amber-700 font-semibold'}>
                      {timeSettings?.ntp_enabled ? 'Enabled' : 'Disabled'}
                    </div>
                  </div>
                  <div className="bg-gray-50 border border-gray-100 rounded-md px-2 py-1">
                    <span className="font-bold text-gray-400 uppercase">Config</span>
                    <div className="font-mono text-gray-700 truncate">{timeSettings?.timesyncd_conf || '/etc/systemd/timesyncd.conf.d/oci-migrator.conf'}</div>
                  </div>
                </div>
                {timeSettings?.helper_installed === false && (
                  <div className="mt-3 text-xs text-amber-700 bg-amber-50 border border-amber-100 rounded-md p-3">
                    Time sync helper is missing. Rerun ./install.sh once on the server to enable dashboard time settings.
                  </div>
                )}
              </form>
              <form onSubmit={handleSaveRcloneDefaultSettings} className="bg-white border border-gray-200 rounded-md shadow-sm p-4 text-left">
                <div className="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-4 mb-4">
                  <div>
                    <h3 className="font-bold text-sm text-gray-800 flex items-center gap-2">
                      <Activity size={16} className="text-[#9c3029]" /> Backup Job Defaults
                    </h3>
                    <p className="mt-1 text-[11px] text-gray-500">Used as defaults when creating new backup jobs. Existing jobs keep their own limits.</p>
                  </div>
                  <button
                    type="submit"
                    disabled={savingRcloneDefaultSettings}
                    className="px-3 py-2 bg-[#9c3029] text-white rounded-md font-semibold text-xs shadow-sm hover:bg-[#7a2520] disabled:opacity-60 flex items-center justify-center gap-2"
                  >
                    {savingRcloneDefaultSettings ? <Loader2 className="animate-spin" size={14} /> : <Save size={14} />}
                    Save Defaults
                  </button>
                </div>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                  <div>
                    <label className="text-[10px] font-bold text-gray-500 uppercase tracking-wider mb-1 block">Bandwidth Limit</label>
                    <input
                      value={rcloneDefaultSettingsForm.bwlimit}
                      onChange={e => setRcloneDefaultSettingsForm({ ...rcloneDefaultSettingsForm, bwlimit: e.target.value.trim() })}
                      placeholder="Unlimited"
                      className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm font-mono focus:outline-none focus:border-[#9c3029]"
                    />
                  </div>
                  <div>
                    <label className="text-[10px] font-bold text-gray-500 uppercase tracking-wider mb-1 block">API TPS Limit</label>
                    <input
                      type="number"
                      min="0"
                      max="10000"
                      step="1"
                      value={rcloneDefaultSettingsForm.tpslimit}
                      onChange={e => setRcloneDefaultSettingsForm({ ...rcloneDefaultSettingsForm, tpslimit: e.target.value })}
                      placeholder="Unlimited"
                      className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm font-mono focus:outline-none focus:border-[#9c3029]"
                    />
                  </div>
                </div>
                <div className="mt-3 grid grid-cols-1 md:grid-cols-2 gap-2 text-[11px]">
                  <div className="bg-gray-50 border border-gray-100 rounded-md px-2 py-1">
                    <span className="font-bold text-gray-400 uppercase">Current Bandwidth</span>
                    <div className="font-mono text-gray-700">{rcloneDefaultSettings?.bwlimit || 'Unlimited'}</div>
                  </div>
                  <div className="bg-gray-50 border border-gray-100 rounded-md px-2 py-1">
                    <span className="font-bold text-gray-400 uppercase">Current TPS</span>
                    <div className="font-mono text-gray-700">{rcloneDefaultSettings?.tpslimit ?? 'Unlimited'}</div>
                  </div>
                </div>
              </form>
              <form onSubmit={handleSaveLocalDiskSettings} className="bg-white border border-gray-200 rounded-md shadow-sm p-4 text-left">
                <div className="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-4 mb-4">
                  <div>
                    <h3 className="font-bold text-sm text-gray-800 flex items-center gap-2">
                      <HardDrive size={16} className="text-[#9c3029]" /> Local Disk Usage
                    </h3>
                    <p className="mt-1 text-[11px] text-gray-500 font-mono truncate">{localDiskSettings?.local_data_root || '/var/lib/oci-migrator/local'}</p>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <button
                      type="button"
                      onClick={fetchLocalDiskSettings}
                      className="px-3 py-2 bg-white border border-gray-200 text-gray-600 rounded-md font-semibold text-xs hover:text-[#9c3029] hover:bg-gray-50 flex items-center gap-2"
                    >
                      <RefreshCw size={14} />
                      Refresh
                    </button>
                    <button
                      type="submit"
                      disabled={savingLocalDiskSettings}
                      className="px-3 py-2 bg-[#9c3029] text-white rounded-md font-semibold text-xs shadow-sm hover:bg-[#7a2520] disabled:opacity-60 flex items-center gap-2"
                    >
                      {savingLocalDiskSettings ? <Loader2 className="animate-spin" size={14} /> : <Save size={14} />}
                      Save Disk
                    </button>
                  </div>
                </div>
                <div className="grid grid-cols-1 lg:grid-cols-[1fr_120px_120px] gap-3 items-end">
                  <div>
                    <div className="grid grid-cols-3 gap-2 text-[11px] mb-3">
                      <div className="bg-gray-50 border border-gray-100 rounded-md px-2 py-1">
                        <span className="font-bold text-gray-400 uppercase">Used</span>
                        <div className="font-semibold text-gray-800">{localDiskSettings?.used || formatBytes(localDiskSettings?.used_bytes)}</div>
                      </div>
                      <div className="bg-gray-50 border border-gray-100 rounded-md px-2 py-1">
                        <span className="font-bold text-gray-400 uppercase">Free</span>
                        <div className="font-semibold text-gray-800">{localDiskSettings?.free || formatBytes(localDiskSettings?.free_bytes)}</div>
                      </div>
                      <div className="bg-gray-50 border border-gray-100 rounded-md px-2 py-1">
                        <span className="font-bold text-gray-400 uppercase">Total</span>
                        <div className="font-semibold text-gray-800">{localDiskSettings?.total || formatBytes(localDiskSettings?.total_bytes)}</div>
                      </div>
                    </div>
                    <div className="h-2 bg-gray-100 rounded-full overflow-hidden">
                      <div
                        className={`h-full ${localDiskSettings?.status === 'error' ? 'bg-red-500' : localDiskSettings?.status === 'warn' ? 'bg-amber-500' : 'bg-green-500'}`}
                        style={{ width: `${Math.min(100, Math.max(0, Number(localDiskSettings?.used_percent || 0)))}%` }}
                      />
                    </div>
                    <div className="mt-2 text-[11px] text-gray-500">{localDiskSettings?.message || 'Local disk usage has not been loaded yet.'}</div>
                  </div>
                  <div>
                    <label className="text-[10px] font-bold text-gray-500 uppercase tracking-wider mb-1 block">Warning %</label>
                    <input
                      type="number"
                      min="1"
                      max="99"
                      value={localDiskSettingsForm.warningPercent}
                      onChange={e => setLocalDiskSettingsForm({...localDiskSettingsForm, warningPercent: e.target.value})}
                      className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm focus:outline-none focus:border-[#9c3029]"
                    />
                  </div>
                  <div>
                    <label className="text-[10px] font-bold text-gray-500 uppercase tracking-wider mb-1 block">Critical %</label>
                    <input
                      type="number"
                      min="2"
                      max="100"
                      value={localDiskSettingsForm.criticalPercent}
                      onChange={e => setLocalDiskSettingsForm({...localDiskSettingsForm, criticalPercent: e.target.value})}
                      className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm focus:outline-none focus:border-[#9c3029]"
                    />
                  </div>
                </div>
              </form>
              <form onSubmit={handleSaveJobLogSettings} className="bg-white border border-gray-200 rounded-md shadow-sm p-4 text-left">
                <div className="flex items-center justify-between gap-4 mb-4">
                  <h3 className="font-bold text-sm text-gray-800 flex items-center gap-2"><Settings size={16} className="text-[#9c3029]" /> Job Log Rotation</h3>
                  <button
                    type="submit"
                    disabled={savingJobLogSettings}
                    className="px-3 py-2 bg-[#9c3029] text-white rounded-md font-semibold text-xs shadow-sm hover:bg-[#7a2520] disabled:opacity-60 flex items-center gap-2"
                  >
                    {savingJobLogSettings ? <Loader2 className="animate-spin" size={14} /> : <Save size={14} />}
                    Save
                  </button>
                </div>
                <div className="grid grid-cols-1 md:grid-cols-[1fr_140px_140px] gap-3">
                  <div>
                    <label className="text-[10px] font-bold text-gray-500 uppercase tracking-wider mb-1 block">Log Directory</label>
                    <input
                      value={jobLogSettings?.job_log_dir || ''}
                      readOnly
                      className="w-full bg-gray-50 border border-gray-200 p-2 rounded-md text-xs font-mono text-gray-600"
                    />
                  </div>
                  <div>
                    <label className="text-[10px] font-bold text-gray-500 uppercase tracking-wider mb-1 block">Retention Days</label>
                    <input
                      type="number"
                      min="1"
                      max="365"
                      value={jobLogSettingsForm.retentionDays}
                      onChange={e => setJobLogSettingsForm({...jobLogSettingsForm, retentionDays: e.target.value})}
                      className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm focus:outline-none focus:border-[#9c3029]"
                    />
                  </div>
                  <div>
                    <label className="text-[10px] font-bold text-gray-500 uppercase tracking-wider mb-1 block">Max Size</label>
                    <input
                      value={jobLogSettingsForm.maxSize}
                      onChange={e => setJobLogSettingsForm({...jobLogSettingsForm, maxSize: e.target.value.toUpperCase()})}
                      placeholder="10M"
                      className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm font-mono focus:outline-none focus:border-[#9c3029]"
                    />
                  </div>
                </div>
                <div className="mt-3 flex flex-wrap gap-2 text-[10px] text-gray-500 font-mono">
                  <span>{jobLogSettings?.rotation_frequency || 'daily'}</span>
                  <span>{jobLogSettings?.logrotate_file || '/etc/logrotate.d/migrator-job-logs'}</span>
                </div>
              </form>
            </div>
          )}

          {/* VIEW: JOB DASHBOARD */}
          {view === 'datasync' && (
            <div className="mx-auto max-w-[1560px] space-y-5 animate-in fade-in">
              <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
                <div className="text-left">
                  <div className="text-[11px] font-bold uppercase text-[#a9342d]">Backup operations</div>
                  <h2 className="mt-1 text-2xl font-bold text-gray-900">Job Dashboard</h2>
                  <p className="mt-1 text-sm text-gray-500">Monitor scheduled backups, run jobs, and inspect recent activity.</p>
                </div>
                <div className="grid grid-cols-2 gap-2 sm:flex">
                  <button
                    type="button"
                    onClick={() => handleNavigate('jobs')}
                    className="inline-flex h-10 items-center justify-center gap-2 rounded-md border border-gray-200 bg-white px-4 text-sm font-semibold text-gray-700 hover:border-[#a9342d] hover:text-[#a9342d]"
                  >
                    <Activity size={16} /> Backup Jobs
                  </button>
                  <button
                    type="button"
                    onClick={() => handleNavigate('builder')}
                    className="inline-flex h-10 items-center justify-center gap-2 rounded-md bg-[#a9342d] px-4 text-sm font-semibold text-white shadow-sm hover:bg-[#8c2924]"
                  >
                    <Plus size={17} /> New Backup Job
                  </button>
                </div>
              </div>

              <section className="ui-panel overflow-hidden" aria-label="System overview">
                <div className="flex items-center justify-between border-b border-gray-100 px-4 py-3 sm:px-5">
                  <div className="flex items-center gap-2.5 text-left">
                    <HeartPulse size={17} className="text-[#a9342d]" />
                    <h3 className="text-sm font-bold text-gray-900">System Overview</h3>
                  </div>
                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      onClick={() => { fetchHealth(); fetchUpgradeCheck(true); }}
                      className="ui-icon-button"
                      disabled={checkingUpgrade}
                      title="Refresh system and update status"
                      aria-label="Refresh system and update status"
                    >
                      <RefreshCw size={15} className={checkingUpgrade ? 'animate-spin' : ''} />
                    </button>
                  </div>
                </div>
                <div className="grid grid-cols-1 md:grid-cols-2">
                  <div className="flex items-start gap-3 border-b border-gray-100 px-4 py-4 text-left md:border-b-0 md:border-r sm:px-5">
                    <div className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-md border ${health?.status === 'ok' ? 'border-green-200 bg-green-50 text-green-700' : health?.status === 'warn' ? 'border-amber-200 bg-amber-50 text-amber-700' : health?.status === 'error' ? 'border-red-200 bg-red-50 text-red-700' : 'border-gray-200 bg-gray-50 text-gray-500'}`}>
                      <HeartPulse size={17} />
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <h4 className="text-xs font-bold text-gray-900">Service Health</h4>
                        <span className={`rounded-full border px-2 py-0.5 text-[9px] font-bold uppercase ${health?.status === 'ok' ? 'border-green-200 bg-green-50 text-green-700' : health?.status === 'warn' ? 'border-amber-200 bg-amber-50 text-amber-700' : health?.status === 'error' ? 'border-red-200 bg-red-50 text-red-700' : 'border-gray-200 bg-gray-50 text-gray-500'}`}>
                          {health?.status || 'checking'}
                        </span>
                      </div>
                      <p className="mt-1 text-xs leading-5 text-gray-500">
                        {!health?.status ? 'Loading service checks...' : health?.status === 'ok' ? `${healthChecks.length} service checks passed.` : `${healthIssueCount || 1} service checks need attention.`}
                      </p>
                    </div>
                  </div>

                  <div className="flex items-start gap-3 px-4 py-4 text-left sm:px-5">
                    <div className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-md border ${upgradeRunning ? 'border-blue-200 bg-blue-50 text-blue-700' : updateAvailable ? 'border-amber-200 bg-amber-50 text-amber-700' : upgradeIsCurrent ? 'border-green-200 bg-green-50 text-green-700' : 'border-gray-200 bg-gray-50 text-gray-500'}`}>
                      {upgradeRunning ? <Loader2 size={17} className="animate-spin" /> : <Download size={17} />}
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <h4 className="text-xs font-bold text-gray-900">Software Update</h4>
                        <span className={`rounded-full border px-2 py-0.5 text-[9px] font-bold uppercase ${upgradeRunning ? 'border-blue-200 bg-blue-50 text-blue-700' : updateAvailable ? 'border-amber-200 bg-amber-50 text-amber-700' : upgradeIsCurrent ? 'border-green-200 bg-green-50 text-green-700' : 'border-gray-200 bg-gray-50 text-gray-500'}`}>
                          {upgradeRunning ? 'running' : updateAvailable ? 'available' : upgradeIsCurrent ? 'current' : checkingUpgrade ? 'checking' : 'unknown'}
                        </span>
                      </div>
                      <p className="mt-1 text-xs leading-5 text-gray-500">
                        {upgradeRunning ? 'The latest version is being installed.' : updateAvailable ? `Version ${latestUpgradeShort || 'latest'} is available in Settings.` : upgradeIsCurrent ? `You are on the latest version${upgradeStatus?.current_short ? ` (${upgradeStatus.current_short})` : ''}.` : checkingUpgrade ? 'Checking GitHub for updates...' : 'Update status is not available.'}
                      </p>
                    </div>
                  </div>
                </div>
              </section>

              <section className="ui-panel grid grid-cols-2 overflow-hidden lg:grid-cols-4" aria-label="Backup overview">
                <div className="border-b border-r border-gray-100 p-4 text-left lg:border-b-0">
                  <div className="text-[10px] font-bold uppercase text-gray-400">Backup jobs</div>
                  <div className="mt-2 flex items-center gap-2">
                    <span className="text-2xl font-bold text-gray-900">{dashboardStats.total}</span>
                    <Archive size={16} className="text-gray-400" />
                  </div>
                </div>
                <div className="border-b border-gray-100 p-4 text-left lg:border-b-0 lg:border-r">
                  <div className="text-[10px] font-bold uppercase text-gray-400">Running now</div>
                  <div className="mt-2 flex items-center gap-2">
                    <span className="text-2xl font-bold text-gray-900">{dashboardStats.running}</span>
                    <Activity size={16} className={dashboardStats.running ? 'text-blue-600' : 'text-gray-400'} />
                  </div>
                </div>
                <div className="border-r border-gray-100 p-4 text-left">
                  <div className="text-[10px] font-bold uppercase text-gray-400">Needs attention</div>
                  <div className="mt-2 flex items-center gap-2">
                    <span className="text-2xl font-bold text-gray-900">{dashboardStats.attention}</span>
                    <AlertCircle size={16} className={dashboardStats.attention ? 'text-red-600' : 'text-gray-400'} />
                  </div>
                </div>
                <div className="p-4 text-left">
                  <div className="text-[10px] font-bold uppercase text-gray-400">Last success</div>
                  <div className="mt-2 flex items-start gap-2">
                    <CheckCircle size={16} className="mt-0.5 shrink-0 text-green-600" />
                    <span className="text-sm font-semibold leading-5 text-gray-700">{formatTimestamp(dashboardStats.lastSuccessAt)}</span>
                  </div>
                </div>
              </section>

              <section className="ui-panel overflow-hidden" aria-labelledby="backup-activity-title">
                <div className="flex flex-col gap-3 border-b border-gray-100 px-4 py-3 sm:flex-row sm:items-center sm:justify-between sm:px-5">
                  <div className="flex items-start gap-2.5 text-left">
                    <Activity size={17} className="mt-0.5 text-[#a9342d]" />
                    <div>
                      <h3 id="backup-activity-title" className="text-sm font-bold text-gray-900">Backup Activity</h3>
                      <p className="mt-0.5 text-xs text-gray-500">Transferred data and run outcomes over the last seven days.</p>
                    </div>
                  </div>
                  <div className="flex flex-wrap items-center gap-2 text-[10px] font-semibold">
                    <span className="ui-mini-tag"><span className="h-2 w-2 rounded-full bg-blue-600" />{formatBytes(backupActivity.totalBytes)} transferred</span>
                    <span className="ui-mini-tag"><span className="h-2 w-2 rounded-full bg-violet-500" />{formatCompactNumber(backupActivity.totalFiles)} files</span>
                    <span className="ui-mini-tag"><span className="h-2 w-2 rounded-full bg-green-600" />{backupActivity.success} successful</span>
                    <span className="ui-mini-tag"><span className="h-2 w-2 rounded-full bg-red-600" />{backupActivity.failed} failed</span>
                  </div>
                </div>

                <div className="overflow-x-auto">
                  <div className="backup-activity-chart relative h-[280px] min-w-[840px]">
                    <div className="absolute inset-0 z-0 grid grid-cols-7" aria-hidden="true">
                      {backupActivity.days.map((day, index) => (
                        <div
                          key={day.key}
                          className={`backup-activity-period ${index === backupActivity.days.length - 1 ? 'is-current' : ''}`}
                          title={`${day.dateLabel}: ${formatBytes(day.bytes)}, ${day.files} files`}
                        />
                      ))}
                    </div>

                    <svg
                      viewBox={`0 0 ${activityChart.width} ${activityChart.height}`}
                      className="pointer-events-none absolute inset-0 z-10 h-full w-full"
                      role="img"
                      aria-label="Transferred backup data and file count for each of the last seven days"
                    >
                      <title>Transferred backup data and file count for each of the last seven days</title>
                      <path d={activityChart.files.areaPath} className="backup-chart-area-files" />
                      <path d={activityChart.files.linePath} className="backup-chart-line-files" />
                      <path d={activityChart.bytes.areaPath} className="backup-chart-area-data" />
                      <path d={activityChart.bytes.linePath} className="backup-chart-line-data" />
                    </svg>

                    <div className="pointer-events-none absolute inset-0 z-20 grid grid-cols-7">
                      {backupActivity.days.map((day, index) => {
                        const runCount = day.success + day.failed + day.other;
                        const previousBytes = index > 0 ? backupActivity.days[index - 1].bytes : day.bytes;
                        const trend = previousBytes > 0
                          ? Math.round(((day.bytes - previousBytes) / previousBytes) * 100)
                          : day.bytes > 0 ? 100 : 0;
                        const TrendIcon = trend > 0 ? TrendingUp : trend < 0 ? TrendingDown : Minus;
                        return (
                          <div key={day.key} className="px-3 py-4 text-left">
                            <div className="text-[10px] font-bold uppercase text-gray-500">{day.label} · {day.dateLabel}</div>
                            <div className="mt-3 flex items-end gap-1.5">
                              <span className="whitespace-nowrap text-lg font-semibold text-gray-900">{formatChartBytes(day.bytes)}</span>
                              <span className={`mb-0.5 inline-flex items-center gap-0.5 text-[10px] font-bold ${trend > 0 ? 'text-green-600' : trend < 0 ? 'text-red-600' : 'text-gray-400'}`}>
                                <TrendIcon size={13} /> {Math.abs(trend)}%
                              </span>
                            </div>
                            <div className={`mt-2 text-[10px] font-semibold ${day.failed ? 'text-red-600' : day.success ? 'text-green-700' : 'text-gray-400'}`}>
                              {runCount === 0 ? 'No runs' : `${runCount} ${runCount === 1 ? 'run' : 'runs'}${day.failed ? ` · ${day.failed} failed` : ''}`}
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                </div>
              </section>
            </div>
          )}

          {/* VIEW: BACKUP JOBS */}
          {view === 'jobs' && (
            <div className="mx-auto max-w-[1560px] space-y-5 animate-in fade-in">
              <div className="flex flex-col gap-4 text-left sm:flex-row sm:items-end sm:justify-between">
                <div>
                  <div className="text-[11px] font-bold uppercase text-[#a9342d]">Backup operations</div>
                  <h2 className="mt-1 text-2xl font-bold text-gray-900">Backup Jobs</h2>
                  <p className="mt-1 text-sm text-gray-500">Manage backup pipelines and review their latest runs.</p>
                </div>
                <button
                  type="button"
                  onClick={() => handleNavigate('builder')}
                  className="inline-flex h-10 items-center justify-center gap-2 rounded-md bg-[#a9342d] px-4 text-sm font-semibold text-white shadow-sm hover:bg-[#8c2924]"
                >
                  <Plus size={17} /> New Backup Job
                </button>
              </div>

              <div className="space-y-5">
                <section className="ui-panel overflow-hidden">
                  <div className="flex items-center justify-between border-b border-gray-100 px-4 py-3.5 sm:px-5">
                    <div className="flex min-w-0 items-center gap-3">
                      <Activity size={18} className="shrink-0 text-[#a9342d]" />
                      <div className="text-left">
                        <h3 className="text-sm font-bold text-gray-900">Active Backup Jobs</h3>
                        <p className="text-[11px] text-gray-500">{jobs.length} configured</p>
                      </div>
                    </div>
                    <button type="button" onClick={fetchJobs} className="ui-icon-button" title="Refresh backup jobs" aria-label="Refresh backup jobs">
                      <RefreshCw size={15} />
                    </button>
                  </div>

                  {jobs.length > 0 && (
                    <div className="hidden grid-cols-[minmax(130px,0.65fr)_minmax(260px,1.35fr)_minmax(170px,0.8fr)_minmax(150px,0.75fr)_152px] gap-4 border-b border-gray-100 bg-gray-50 px-5 py-2.5 text-[10px] font-bold uppercase text-gray-400 lg:grid">
                      <div>Job</div>
                      <div>Pipeline</div>
                      <div>Schedule</div>
                      <div>Last run</div>
                      <div className="text-right">Actions</div>
                    </div>
                  )}

                  <div className="divide-y divide-gray-100">
                    {jobs.map(job => {
                      const latestRun = latestRunByJob[job.name];
                      const latestMessage = cleanJobMessage(latestRun?.error || latestRun?.details || '');
                      const latestStatus = String(latestRun?.status || '').toLowerCase();
                      const showLatestMessage = latestRun && latestStatus !== 'success' && latestMessage;
                      const latestSummaryParts = backupSummaryParts(latestRun?.rclone_summary).slice(0, 2);
                      const scheduleText = (() => {
                        const schedule = job.schedule || {};
                        if (schedule.frequency === 'none') return 'Manual only';
                        if (schedule.frequency === 'weekly') return `Weekly ${schedule.day_of_week || 'monday'} @ ${schedule.time || '02:00'}`;
                        if (schedule.frequency === 'monthly') return `Monthly day ${schedule.day_of_month || '1'} @ ${schedule.time || '02:00'}`;
                        return `${schedule.frequency || 'Manual'} @ ${schedule.time || '02:00'}`;
                      })();
                      const destination = `${job.dest_profile || ''}${job.dest_profile ? ':' : ''}${job.dest_bucket || ''}`;

                      return (
                        <div key={job.name}>
                          <div className="grid grid-cols-1 gap-4 px-4 py-4 text-left sm:px-5 lg:grid-cols-[minmax(130px,0.65fr)_minmax(260px,1.35fr)_minmax(170px,0.8fr)_minmax(150px,0.75fr)_152px] lg:items-center">
                            <div className="flex min-w-0 items-start gap-2.5">
                              <RefreshCw className="mt-0.5 shrink-0 text-[#a9342d]" size={15} />
                              <div className="min-w-0">
                                <div className="mb-0.5 text-[9px] font-bold uppercase text-gray-400 lg:hidden">Job</div>
                                <h4 className="break-words text-sm font-bold text-gray-900">{job.name}</h4>
                                <span className="mt-1 inline-block rounded border border-gray-200 bg-gray-50 px-1.5 py-0.5 text-[9px] font-bold uppercase text-gray-500">{job.sync_mode || 'copy'}</span>
                              </div>
                            </div>

                            <div className="min-w-0 space-y-2 font-mono">
                              <div>
                                <div className="text-[9px] font-bold uppercase text-gray-400 font-sans">Source</div>
                                <div className="mt-0.5 break-all text-[11px] leading-4 text-gray-600">{job.source_remote || 'Not configured'}</div>
                              </div>
                              <div>
                                <div className="text-[9px] font-bold uppercase text-gray-400 font-sans">Destination</div>
                                <div className="mt-0.5 break-all text-[11px] font-semibold leading-4 text-gray-700">{destination || 'Not configured'}</div>
                              </div>
                            </div>

                            <div className="min-w-0">
                              <div className="mb-1 text-[9px] font-bold uppercase text-gray-400 lg:hidden">Schedule</div>
                              <div className="flex items-start gap-1.5 text-xs leading-5 text-gray-600">
                                <Clock size={13} className="mt-0.5 shrink-0" />
                                <span>{scheduleText}</span>
                              </div>
                              <div className="mt-2 flex flex-wrap gap-1">
                                {Boolean(job.metadata_tags?.length) && <span className="ui-mini-tag"><Tags size={10} /> {job.metadata_tags.length}</span>}
                                {job.local_retention?.enabled && <span className="ui-mini-tag text-amber-700">Cleanup {job.local_retention.delete_after_days || 30}d</span>}
                                {job.bwlimit && <span className="ui-mini-tag">BW {job.bwlimit}</span>}
                                {Number(job.tpslimit) > 0 && <span className="ui-mini-tag">TPS {job.tpslimit}</span>}
                              </div>
                            </div>

                            <div className="min-w-0">
                              <div className="mb-1 text-[9px] font-bold uppercase text-gray-400 lg:hidden">Last run</div>
                              {latestRun ? (
                                <div className="flex min-w-0 flex-col items-start gap-1.5">
                                  <span className={`rounded-full border px-2 py-0.5 text-[9px] font-bold uppercase ${runStatusClass(latestRun.status)}`}>{latestRun.status}</span>
                                  <span className="text-[10px] text-gray-500">{formatTimestamp(latestRun.finished_at || latestRun.updated_at || latestRun.created_at)}</span>
                                  {showLatestMessage && <span className="ui-run-message max-w-full break-words text-[10px] text-red-600" title={latestMessage}>{latestMessage}</span>}
                                  {latestSummaryParts.length > 0 && (
                                    <div className="flex flex-wrap gap-1">
                                      {latestSummaryParts.map(part => <span key={part} className="ui-mini-tag">{part}</span>)}
                                    </div>
                                  )}
                                </div>
                              ) : <span className="text-xs text-gray-400">Never run</span>}
                            </div>

                            <div className="flex flex-wrap items-center gap-1.5 lg:justify-end">
                              <button type="button" onClick={() => handleEditJob(job)} className="ui-icon-button" title="Edit job" aria-label={`Edit ${job.name}`}><Edit size={15} /></button>
                              <button type="button" onClick={() => handleRunManual(job)} className="ui-icon-button is-primary" title="Run now" aria-label={`Run ${job.name} now`}><Play size={15} /></button>
                              <button type="button" onClick={() => activeLogJob === job.name ? setActiveLogJob(null) : setActiveLogJob(job.name)} className={`ui-icon-button ${activeLogJob === job.name ? 'is-active' : ''}`} title="View latest log" aria-label={`View log for ${job.name}`}><Terminal size={15} /></button>
                              <button type="button" onClick={() => handleDeleteJob(job.name)} className="ui-icon-button is-danger" title="Delete job" aria-label={`Delete ${job.name}`}><Trash2 size={15} /></button>
                            </div>
                          </div>
                          {activeLogJob === job.name && (
                            <div className="border-t border-gray-100 bg-gray-50 px-4 py-4 sm:px-5">
                              <pre className="h-36 overflow-y-auto whitespace-pre-wrap rounded-md border border-gray-800 bg-gray-900 p-4 text-left font-mono text-[11px] text-gray-300 shadow-inner">{liveLogData || 'Awaiting process...'}</pre>
                            </div>
                          )}
                        </div>
                      );
                    })}
                    {jobs.length === 0 && (
                      <div className="flex min-h-64 flex-col items-center justify-center px-6 py-12 text-center">
                        <div className="flex h-10 w-10 items-center justify-center rounded-md border border-gray-200 bg-gray-50 text-gray-400"><Archive size={19} /></div>
                        <h4 className="mt-3 text-sm font-bold text-gray-800">No backup jobs configured</h4>
                        <p className="mt-1 max-w-sm text-xs leading-5 text-gray-500">Create a job to schedule or manually run a backup pipeline.</p>
                        <button type="button" onClick={() => handleNavigate('builder')} className="mt-4 inline-flex h-9 items-center gap-2 rounded-md border border-gray-200 bg-white px-3 text-xs font-semibold text-gray-700 hover:border-[#a9342d] hover:text-[#a9342d]"><Plus size={14} /> New Backup Job</button>
                      </div>
                    )}
                  </div>
                </section>

                <section className="ui-panel overflow-hidden">
                  <div className="flex items-center justify-between border-b border-gray-100 px-4 py-3.5">
                    <div className="flex items-center gap-3 text-left">
                      <Clock size={17} className="text-[#a9342d]" />
                      <div>
                        <h3 className="text-sm font-bold text-gray-900">Recent Runs</h3>
                        <p className="text-[11px] text-gray-500">Latest backup activity</p>
                      </div>
                    </div>
                    <button type="button" onClick={fetchJobRuns} className="ui-icon-button" title="Refresh job history" aria-label="Refresh job history"><RefreshCw size={15} /></button>
                  </div>
                  <div className="max-h-[720px] divide-y divide-gray-100 overflow-y-auto">
                    {jobRuns.slice(0, 10).map(run => {
                      const isDataSyncRun = run.kind === 'data_sync';
                      const runMessage = cleanJobMessage(run.error || run.details);
                      const showRunMessage = String(run.status || '').toLowerCase() !== 'success' && runMessage;
                      const runSummaryParts = backupSummaryParts(run.rclone_summary).slice(0, 3);
                      return (
                        <div key={run.id}>
                          <div className="p-4 text-left">
                            <div className="flex items-start justify-between gap-3">
                              <div className="min-w-0">
                                <div className="flex min-w-0 flex-wrap items-center gap-2">
                                  <span className={`rounded-full border px-2 py-0.5 text-[9px] font-bold uppercase ${runStatusClass(run.status)}`}>{run.status}</span>
                                  <span className="min-w-0 break-words text-sm font-semibold text-gray-900">{run.job_name || run.kind}</span>
                                </div>
                                <div className="mt-1 text-[10px] uppercase text-gray-400">{run.trigger || 'manual'} · {formatTimestamp(run.updated_at || run.created_at)}</div>
                              </div>
                              {isDataSyncRun && (
                                <div className="flex shrink-0 items-center gap-1.5">
                                  <button type="button" onClick={() => setActiveRunLogId(activeRunLogId === run.id ? null : run.id)} className={`ui-icon-button ${activeRunLogId === run.id ? 'is-active' : ''}`} title="View job log" aria-label="View job log"><Terminal size={14} /></button>
                                  <button type="button" onClick={() => handleDownloadRunLog(run)} className="ui-icon-button" title="Download job log" aria-label="Download job log"><Download size={14} /></button>
                                </div>
                              )}
                            </div>
                            {showRunMessage && <div className="ui-run-message mt-2 break-words text-xs leading-5 text-red-600" title={runMessage}>{runMessage}</div>}
                            {runSummaryParts.length > 0 && (
                              <div className="mt-2 flex flex-wrap gap-1">
                                {runSummaryParts.map(part => <span key={part} className="ui-mini-tag">{part}</span>)}
                              </div>
                            )}
                          </div>
                          {activeRunLogId === run.id && (
                            <div className="border-t border-gray-100 bg-gray-50 p-4">
                              <pre className="h-44 overflow-y-auto whitespace-pre-wrap rounded-md border border-gray-800 bg-gray-900 p-4 text-left font-mono text-[11px] text-gray-300 shadow-inner">{runLogData || 'Loading log...'}</pre>
                            </div>
                          )}
                        </div>
                      );
                    })}
                    {jobRuns.length === 0 && (
                      <div className="flex min-h-64 flex-col items-center justify-center p-8 text-center text-gray-500">
                        <Clock size={20} className="text-gray-300" />
                        <span className="mt-2 text-xs">No run history yet.</span>
                      </div>
                    )}
                  </div>
                </section>
              </div>
            </div>
          )}

          {/* VIEW: JOB BUILDER */}
          {view === 'builder' && (
            <div className="max-w-3xl mx-auto animate-in slide-in-from-bottom-4">
              <div className="bg-white border border-gray-200 rounded-md p-8 shadow-sm text-left">
                <h2 className="text-xl font-bold mb-8 flex items-center gap-2 text-gray-800"><Plus className="text-[#9c3029]"/> {editingJobName ? 'Edit Backup Job' : 'Build Backup Job'}</h2>
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
                    <select value={selectedSyncRemoteName} onChange={async e => {
                      const r = e.target.value;
                      setSyncJob(prev => ({...prev, source_remote: r}));
                      await loadSourceBuckets(r);
                    }} className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm focus:outline-none focus:border-[#9c3029]">
                      <option value="">Select Remote...</option>
                      {remotes.map(r => <option key={r} value={r}>{r}</option>)}
                    </select>
                    <select value={selectedSyncSourceValue} onChange={e => {
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
                    <select value={syncJob.dest_profile} onChange={async e => {
                      const profile = e.target.value;
                      setSyncJob(prev => ({...prev, dest_profile: profile, dest_bucket: ''}));
                      await loadDestBuckets(profile);
                    }} className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm focus:outline-none focus:border-[#9c3029]">
                      <option value="">Select OCI Profile...</option>
                      {profiles.map(p => <option key={p} value={p}>{p}</option>)}
                    </select>
                    <select value={syncJob.dest_bucket} onChange={e => setSyncJob({...syncJob, dest_bucket: e.target.value})} className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm focus:outline-none focus:border-[#9c3029]" disabled={!destBuckets.length}>
                      <option value="">Select Target Bucket...</option>
                      {destBuckets.map(b => <option key={b.name} value={b.name}>{b.name}</option>)}
                    </select>
                  </div>
                </div>
                <div className="mb-6 border border-gray-200 rounded-md overflow-hidden">
                  <div className="px-4 py-3 bg-gray-50 border-b border-gray-100 flex items-center justify-between gap-3">
                    <h4 className="text-sm font-bold text-gray-800 flex items-center gap-2"><HardDrive size={16}/> Local Cleanup</h4>
                    <label className="flex items-center gap-2 text-xs font-semibold text-gray-600">
                      <input
                        type="checkbox"
                        checked={!!syncJob.local_retention?.enabled}
                        onChange={e => setSyncJob(prev => ({
                          ...prev,
                          local_retention: {
                            ...DEFAULT_LOCAL_RETENTION,
                            ...(prev.local_retention || {}),
                            enabled: e.target.checked
                          }
                        }))}
                        className="accent-[#9c3029]"
                      />
                      Enabled
                    </label>
                  </div>
                  {syncJob.local_retention?.enabled ? (
                    <div className="p-4 grid grid-cols-1 md:grid-cols-2 gap-3">
                      <div>
                        <label className="text-[10px] uppercase font-bold text-gray-500">Delete Files Older Than</label>
                        <div className="mt-1 flex items-center gap-2">
                          <input
                            type="number"
                            min="1"
                            max="3650"
                            value={syncJob.local_retention?.delete_after_days ?? DEFAULT_LOCAL_RETENTION.delete_after_days}
                            onChange={e => setSyncJob(prev => ({
                              ...prev,
                              local_retention: {
                                ...DEFAULT_LOCAL_RETENTION,
                                ...(prev.local_retention || {}),
                                delete_after_days: Number(e.target.value)
                              }
                            }))}
                            className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm focus:outline-none focus:border-[#9c3029]"
                          />
                          <span className="text-xs text-gray-500 font-semibold">days</span>
                        </div>
                      </div>
                      <div>
                        <label className="text-[10px] uppercase font-bold text-gray-500">Ignore Modified In Last</label>
                        <div className="mt-1 flex items-center gap-2">
                          <input
                            type="number"
                            min="1"
                            max="720"
                            value={syncJob.local_retention?.min_file_age_hours ?? DEFAULT_LOCAL_RETENTION.min_file_age_hours}
                            onChange={e => setSyncJob(prev => ({
                              ...prev,
                              local_retention: {
                                ...DEFAULT_LOCAL_RETENTION,
                                ...(prev.local_retention || {}),
                                min_file_age_hours: Number(e.target.value)
                              }
                            }))}
                            className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm focus:outline-none focus:border-[#9c3029]"
                          />
                          <span className="text-xs text-gray-500 font-semibold">hours</span>
                        </div>
                      </div>
                      {!selectedSyncSourceIsManagedLocal && (
                        <div className="md:col-span-2 text-xs text-amber-700 bg-amber-50 border border-amber-100 rounded-md p-2">
                          Select a managed server local folder as source before saving local cleanup.
                        </div>
                      )}
                      {selectedSyncRetentionConflict && (
                        <div className="md:col-span-2 text-xs text-red-700 bg-red-50 border border-red-100 rounded-md p-2">
                          {selectedSyncRetentionConflict.name} already has local cleanup enabled for this source.
                        </div>
                      )}
                    </div>
                  ) : (
                    <div className="p-4 text-xs text-gray-400">Disabled for this backup job.</div>
                  )}
                </div>
                <div className="mb-6 border border-gray-200 rounded-md overflow-hidden">
                  <div className="px-4 py-3 bg-gray-50 border-b border-gray-100 flex items-center justify-between gap-3">
                    <h4 className="text-sm font-bold text-gray-800 flex items-center gap-2"><Tags size={16}/> Object Metadata</h4>
                    <button
                      type="button"
                      onClick={() => setSyncJob(prev => ({
                        ...prev,
                        metadata_tags: [...(prev.metadata_tags || []), { key: '', value: '' }]
                      }))}
                      className="px-2.5 py-1.5 bg-white border border-gray-200 text-gray-600 rounded-md text-xs font-semibold hover:text-[#9c3029] hover:bg-gray-50 flex items-center gap-1"
                    >
                      <Plus size={13} /> Add
                    </button>
                  </div>
                  {(syncJob.metadata_tags || []).length > 0 ? (
                    <div className="divide-y divide-gray-100">
                      {(syncJob.metadata_tags || []).map((tag, index) => (
                        <div key={index} className="grid grid-cols-[1fr_1fr_34px] gap-3 p-3 items-center">
                          <input
                            value={tag.key}
                            onChange={e => setSyncJob(prev => {
                              const nextTags = [...(prev.metadata_tags || [])];
                              nextTags[index] = { ...nextTags[index], key: e.target.value };
                              return { ...prev, metadata_tags: nextTags };
                            })}
                            placeholder="site"
                            className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm font-mono focus:outline-none focus:border-[#9c3029]"
                          />
                          <input
                            value={tag.value}
                            onChange={e => setSyncJob(prev => {
                              const nextTags = [...(prev.metadata_tags || [])];
                              nextTags[index] = { ...nextTags[index], value: e.target.value };
                              return { ...prev, metadata_tags: nextTags };
                            })}
                            placeholder="value"
                            className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm focus:outline-none focus:border-[#9c3029]"
                          />
                          <button
                            type="button"
                            onClick={() => setSyncJob(prev => ({
                              ...prev,
                              metadata_tags: (prev.metadata_tags || []).filter((_, tagIndex) => tagIndex !== index)
                            }))}
                            className="p-2 bg-white border border-gray-200 text-gray-500 rounded-md hover:text-[#9c3029] hover:bg-gray-50"
                            title="Remove metadata tag"
                          >
                            <Trash2 size={14} />
                          </button>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div className="p-4 text-xs text-gray-400">No object metadata configured. Enter names like site or ticket-id; OCI stores them as opc-meta-site and opc-meta-ticket-id.</div>
                  )}
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

                <div className="mb-6 border border-gray-200 rounded-md overflow-hidden">
                  <div className="px-4 py-3 bg-gray-50 border-b border-gray-100">
                    <h4 className="text-sm font-bold text-gray-800 flex items-center gap-2"><Activity size={16}/> Traffic Limits</h4>
                  </div>
                  <div className="p-4 grid grid-cols-1 md:grid-cols-2 gap-4 text-left">
                    <div className="space-y-1">
                      <label className="text-[11px] uppercase font-bold text-gray-500">Bandwidth Limit</label>
                      <input
                        value={syncJob.bwlimit || ''}
                        onChange={e => setSyncJob({...syncJob, bwlimit: e.target.value.trim()})}
                        placeholder="Unlimited"
                        className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm font-mono focus:outline-none focus:border-[#9c3029]"
                      />
                    </div>
                    <div className="space-y-1">
                      <label className="text-[11px] uppercase font-bold text-gray-500">API TPS Limit</label>
                      <input
                        type="number"
                        min="0"
                        max="10000"
                        step="1"
                        value={syncJob.tpslimit ?? ''}
                        onChange={e => setSyncJob({...syncJob, tpslimit: e.target.value})}
                        placeholder="Unlimited"
                        className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm font-mono focus:outline-none focus:border-[#9c3029]"
                      />
                    </div>
                  </div>
                </div>

                <div className="p-4 bg-gray-50 rounded-md border border-gray-200 mb-6 grid grid-cols-3 gap-4 text-left">
                  <div className="space-y-1">
                    <label className="text-[11px] uppercase font-bold text-gray-500">Frequency</label>
                    <select value={syncJob.schedule.frequency} onChange={e => setSyncJob({...syncJob, schedule: {...syncJob.schedule, frequency: e.target.value}})} className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm focus:outline-none focus:border-[#9c3029]">
                      <option value="none">Manual Only</option><option value="daily">Daily</option><option value="weekly">Weekly</option><option value="monthly">Monthly</option>
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
                  {syncJob.schedule.frequency === 'monthly' && (
                    <div className="space-y-1">
                      <label className="text-[11px] uppercase font-bold text-gray-500">Day of Month</label>
                      <input
                        type="number"
                        min="1"
                        max="31"
                        value={syncJob.schedule.day_of_month}
                        onChange={e => setSyncJob({...syncJob, schedule: {...syncJob.schedule, day_of_month: e.target.value}})}
                        className="w-full bg-white border border-gray-200 p-2 rounded-md text-sm focus:outline-none focus:border-[#9c3029]"
                      />
                    </div>
                  )}
                </div>

                <button onClick={handleSaveJob} disabled={loading} className="w-full bg-[#9c3029] text-white py-3 rounded-md font-semibold hover:bg-[#a63d2e] transition-colors flex items-center justify-center gap-2 shadow-sm">
                  {loading ? <Loader2 className="animate-spin" /> : <><CheckCircle size={18}/> {editingJobName ? 'Save Changes' : 'Save Pipeline'}</>}
                </button>
              </div>
            </div>
          )}

          {/* VIEW: VM EXPLORER */}
          {view === 'explorer' && (
             <div className="space-y-6 animate-in slide-in-from-right-4 max-w-none">
                <div className="bg-white p-4 rounded-md border border-gray-200 shadow-sm">
                  <div className="grid grid-cols-1 lg:grid-cols-[minmax(220px,320px)_minmax(240px,420px)_1fr] gap-4 items-end">
                    <div className="min-w-0">
                      <label className="text-[10px] font-bold text-gray-500 uppercase tracking-wider mb-1 flex items-center gap-2">
                        <Database size={14} className="text-[#9c3029]" />
                        Source
                      </label>
                      <select value={activeSourceProfile} onChange={e => fetchVms(e.target.value)} className="w-64 max-w-full bg-white border border-gray-200 rounded-md py-2 px-3 text-sm font-semibold text-gray-800 focus:outline-none focus:border-[#9c3029]">
                        <option value="">Select OCI profile...</option>
                        {profiles.map(p => <option key={p} value={p}>{p}</option>)}
                      </select>
                    </div>
                    <div className="min-w-0">
                      <label className="text-[10px] font-bold text-gray-500 uppercase tracking-wider block mb-1">Filter</label>
                      <div className="relative">
                        <Search className="absolute left-3 top-2.5 text-gray-400" size={15} />
                        <input
                          className="w-full bg-white border border-gray-200 rounded-md py-2 pl-9 pr-3 text-sm text-gray-800 focus:outline-none focus:border-[#9c3029] focus:ring-1 focus:ring-[#9c3029] disabled:bg-gray-50 disabled:text-gray-400"
                          placeholder="Search name, OS, shape, IP, OCID..."
                          value={searchTerm}
                          onChange={e => setSearchTerm(e.target.value)}
                          disabled={!activeSourceProfile}
                        />
                      </div>
                    </div>
                    <div className="flex items-center gap-2 justify-start lg:justify-end">
                      <button onClick={() => fetchVms(activeSourceProfile)} disabled={!activeSourceProfile || loading} className="px-3 py-2 bg-white border border-gray-200 text-gray-600 rounded-md hover:text-[#9c3029] hover:bg-gray-50 disabled:opacity-50 flex items-center gap-2 text-xs font-semibold">
                        {loading ? <Loader2 className="animate-spin" size={14} /> : <RefreshCw size={14} />}
                        Refresh
                      </button>
                      <span className="text-xs font-semibold text-gray-500 bg-gray-100 px-3 py-1 rounded-full">
                        {activeSourceProfile ? (searchTerm.trim() ? `${filteredVms.length}/${vms.length} VMs` : `${vms.length} VMs`) : 'No source selected'}
                      </span>
                    </div>
	                  </div>
	                </div>

	                {selectedVms.length > 0 && (
	                  <div className="bg-white p-4 rounded-md border border-gray-200 shadow-sm text-left">
	                    <div className="mb-4 flex items-start gap-2 rounded-md border border-amber-100 bg-amber-50 px-3 py-2 text-xs text-amber-800">
	                      <AlertCircle size={15} className="mt-0.5 shrink-0" />
	                      <div>
	                        <div>This action will shut down the selected server(s) and create a backup in the selected storage bucket.</div>
	                        <div className="mt-1 font-semibold">Boot volume image only. Attached data volumes are not included and must be migrated separately.</div>
	                      </div>
	                    </div>
	                    <div className="grid grid-cols-1 xl:grid-cols-[minmax(220px,1fr)_minmax(220px,1fr)_220px] gap-4 items-end">
	                      <div>
	                        <div className="flex items-center justify-between mb-1">
	                          <label className="text-[10px] font-bold text-gray-500 uppercase tracking-wider">Dest Profile</label>
	                          <span className="text-[10px] font-bold text-[#9c3029] uppercase">{selectedVms.length} selected</span>
	                        </div>
	                        <select value={vmMigrationConfig.destProfile} onChange={e => {
	                          const profile = e.target.value;
	                          setVmMigrationConfig({...vmMigrationConfig, destProfile: profile, destBucket: ''});
	                          if (profile) {
	                            api.get(`/list-buckets/${profile}`).then(res => setDestBuckets(res.data));
	                          } else {
	                            setDestBuckets([]);
	                          }
	                        }} className="w-full bg-white border border-gray-200 p-2.5 rounded-md text-sm focus:outline-none focus:border-[#9c3029]">
	                          <option value="">Select Target...</option>
	                          {profiles.map(p => <option key={p} value={p}>{p}</option>)}
	                        </select>
	                      </div>
	                      <div>
	                        <label className="text-[10px] font-bold text-gray-500 uppercase tracking-wider mb-1 block">Storage Bucket</label>
	                        <select value={vmMigrationConfig.destBucket} onChange={e => setVmMigrationConfig({...vmMigrationConfig, destBucket: e.target.value})} className="w-full bg-white border border-gray-200 p-2.5 rounded-md text-sm focus:outline-none focus:border-[#9c3029]" disabled={!vmMigrationConfig.destProfile || !destBuckets.length}>
	                          <option value="">Select Bucket...</option>
	                          {destBuckets.map(b => <option key={b.name} value={b.name}>{b.name}</option>)}
	                        </select>
	                      </div>
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
	                      }} className="w-full bg-[#9c3029] text-white px-4 py-2.5 rounded-md font-semibold text-sm flex items-center justify-center gap-2 hover:bg-[#a63d2e] transition-colors shadow-sm">
	                        Execute Migration <ArrowRight size={16} />
	                      </button>
	                    </div>
	                  </div>
	                )}
	
	                {activeSourceProfile ? (
	                  loading ? ( <div className="flex justify-center p-20"><Loader2 className="animate-spin text-gray-400" size={40} /></div>
                  ) : (
                    <div className="bg-white border border-gray-200 rounded-md shadow-sm overflow-hidden">
                      <div className="hidden xl:grid grid-cols-[minmax(360px,1.7fr)_minmax(330px,1.35fr)_minmax(110px,0.55fr)_minmax(180px,0.8fr)] gap-4 px-4 py-3 bg-gray-50 border-b border-gray-100 text-[10px] uppercase font-bold tracking-wider text-gray-400">
                        <div>VM</div>
                        <div>OS / Shape</div>
                        <div>OCPU/RAM</div>
                        <div>IPs</div>
                      </div>
                      <div className="divide-y divide-gray-100">
                        {filteredVms.map(vm => {
                          const taskData = Object.values(vmTasks).find(t => t.vm_id === vm.id);
                          const isMigrating = !!taskData && taskData.status !== 'SUCCESS' && taskData.status !== 'FAILURE';
                          const isSelected = selectedVms.includes(vm.id);
                          const dataVolumes = Array.isArray(vm.data_volumes) ? vm.data_volumes : [];
                          const stateClass = vm.state === 'RUNNING'
                            ? 'bg-green-50 text-green-700 border-green-200'
                            : vm.state === 'STOPPED'
                              ? 'bg-gray-100 text-gray-600 border-gray-200'
                              : 'bg-amber-50 text-amber-700 border-amber-200';
                          return (
                            <div
                              key={vm.id}
                              onClick={() => { if (!isMigrating) setSelectedVms(prev => prev.includes(vm.id) ? prev.filter(i => i !== vm.id) : [...prev, vm.id]); }}
                              className={`grid grid-cols-1 xl:grid-cols-[minmax(360px,1.7fr)_minmax(330px,1.35fr)_minmax(110px,0.55fr)_minmax(180px,0.8fr)] gap-3 xl:gap-4 items-start p-4 transition-colors ${isSelected ? 'bg-red-50' : 'bg-white hover:bg-gray-50'} ${isMigrating ? 'opacity-70 cursor-not-allowed' : 'cursor-pointer'}`}
                            >
                              <div className="flex items-start gap-3 min-w-0">
                                <Cloud className={`mt-0.5 shrink-0 ${isSelected ? 'text-[#9c3029]' : 'text-gray-400'}`} size={18}/>
                                <div className="min-w-0">
                                  <h4 className="font-bold text-gray-800 text-sm leading-snug break-words">{vm.name}</h4>
                                  <div className="mt-1">
                                    <span className={`text-[9px] px-2 py-0.5 rounded-full border font-bold uppercase ${stateClass}`}>
                                      {vm.state || '-'}
                                    </span>
                                  </div>
                                  <div className="mt-2 space-y-1 text-[10px] leading-snug text-gray-500">
                                    <div className="break-words" title={vm.boot_volume?.id || ''}>
                                      <span className="font-bold uppercase text-gray-400">Boot:</span>{' '}
                                      {vm.boot_volume
                                        ? `${vm.boot_volume.name}${vm.boot_volume.size_gb ? ` (${vm.boot_volume.size_gb} GB)` : ''}`
                                        : vm.volume_scan_status === 'partial' ? 'Details unavailable' : 'Not found'}
                                    </div>
                                    <div className="break-words" title={dataVolumes.map(volume => volume.id).filter(Boolean).join('\n')}>
                                      <span className="font-bold uppercase text-gray-400">Data:</span>{' '}
                                      {dataVolumes.length
                                        ? dataVolumes.map(volume => `${volume.name}${volume.size_gb ? ` (${volume.size_gb} GB)` : ''}`).join(', ')
                                        : vm.volume_scan_status === 'partial' ? 'Details unavailable' : 'None attached'}
                                    </div>
                                  </div>
                                  {taskData && (
                                    <div className={`mt-2 text-[10px] uppercase font-bold tracking-wider truncate ${getStatusColor(taskData.status)}`}>
                                      {isMigrating && <Loader2 size={12} className="inline animate-spin mr-1"/>}
                                      {taskData.details}
                                    </div>
                                  )}
                                </div>
                              </div>
                              <div className="min-w-0 text-left space-y-1">
                                <div>
                                  <div className="text-[9px] uppercase font-bold text-gray-400 mb-0.5">OS</div>
                                  <div className="text-xs text-gray-700 leading-snug break-words" title={vm.os || 'Unknown'}>{vm.os || 'Unknown'}</div>
                                </div>
                                <div>
                                  <div className="text-[9px] uppercase font-bold text-gray-400 mb-0.5">Shape</div>
                                  <div className="text-xs text-gray-700 leading-snug break-words" title={vm.shape || 'Unknown'}>{vm.shape || 'Unknown'}</div>
                                </div>
                              </div>
                              <div className="text-left space-y-1">
                                <div>
                                  <div className="text-[9px] uppercase font-bold text-gray-400 mb-0.5">OCPU</div>
                                  <div className="text-xs text-gray-700">{vm.ocpus ?? '-'}</div>
                                </div>
                                <div>
                                  <div className="text-[9px] uppercase font-bold text-gray-400 mb-0.5">RAM</div>
                                  <div className="text-xs text-gray-700">{vm.memory_gb ? `${vm.memory_gb} GB` : '-'}</div>
                                </div>
                              </div>
                              <div className="min-w-0 text-left font-mono space-y-1">
                                <div>
                                  <div className="text-[9px] uppercase font-bold text-gray-400 mb-0.5 font-sans">Private</div>
                                  <div className="text-xs text-gray-700 truncate" title={vm.private_ip || '-'}>{vm.private_ip || '-'}</div>
                                </div>
                                <div>
                                  <div className="text-[9px] uppercase font-bold text-gray-400 mb-0.5 font-sans">Public</div>
                                  <div className="text-xs text-gray-700 truncate" title={vm.public_ip || '-'}>{vm.public_ip || '-'}</div>
                                </div>
                              </div>
                            </div>
                          );
                        })}
                        {filteredVms.length === 0 && <div className="text-center p-12 text-gray-500">No VMs match your search.</div>}
                      </div>
                    </div>
                  )
                ) : (
                  <div className="flex flex-col items-center justify-center h-64 bg-white border border-gray-200 rounded-md text-gray-500">
                    <Database size={32} className="mb-3 text-gray-300"/>
                    <p className="text-sm">Select a source profile to list VMs.</p>
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
                   <>
                   <div className="bg-white border border-gray-200 rounded-md p-5 shadow-sm text-left">
                     <h3 className="font-bold text-gray-800 mb-4 flex items-center gap-2 text-sm"><Plus size={16} className="text-[#9c3029]"/> Create Bucket</h3>
                     <div className="grid grid-cols-1 lg:grid-cols-[1.5fr_1fr_1fr_1.3fr_auto] gap-3 items-end">
                       <div>
                         <label className="text-[10px] uppercase font-bold text-gray-500">Bucket Name</label>
                         <input value={newBucketName} onChange={e => setNewBucketName(e.target.value)} placeholder="New bucket name" className="mt-1 w-full bg-white border border-gray-200 p-2 rounded-md text-sm focus:outline-none focus:border-[#9c3029]" />
                       </div>
                       <div>
                         <label className="text-[10px] uppercase font-bold text-gray-500">Default Tier</label>
                         <select
                           value={newBucketConfig.storageTier}
                           onChange={e => setNewBucketConfig(prev => ({
                             ...prev,
                             storageTier: e.target.value,
                             autoTiering: e.target.value === 'Standard' ? prev.autoTiering : 'Disabled'
                           }))}
                           className="mt-1 w-full bg-white border border-gray-200 p-2 rounded-md text-sm focus:outline-none focus:border-[#9c3029]"
                         >
                           <option value="Standard">Standard</option>
                           <option value="Archive">Archive</option>
                         </select>
                       </div>
                       <div>
                         <label className="text-[10px] uppercase font-bold text-gray-500">Versioning</label>
                         <select
                           value={newBucketConfig.versioning}
                           onChange={e => setNewBucketConfig(prev => ({ ...prev, versioning: e.target.value }))}
                           className="mt-1 w-full bg-white border border-gray-200 p-2 rounded-md text-sm focus:outline-none focus:border-[#9c3029]"
                         >
                           <option value="Disabled">Disabled</option>
                           <option value="Enabled">Enabled</option>
                         </select>
                       </div>
                       <div>
                         <label className={`mt-5 flex items-center gap-2 text-xs font-semibold ${newBucketConfig.storageTier === 'Standard' ? 'text-gray-600' : 'text-gray-400'}`}>
                           <input
                             type="checkbox"
                             checked={newBucketConfig.autoTiering === 'InfrequentAccess'}
                             disabled={newBucketConfig.storageTier !== 'Standard'}
                             onChange={e => setNewBucketConfig(prev => ({ ...prev, autoTiering: e.target.checked ? 'InfrequentAccess' : 'Disabled' }))}
                             className="accent-[#9c3029]"
                           />
                           Auto-Tiering to Infrequent Access
                         </label>
                         <div className="mt-1 text-[10px] text-gray-500">Infrequent Access is reached with Auto-Tiering or lifecycle rules.</div>
                       </div>
                       <button onClick={handleCreateBucket} className="bg-[#9c3029] text-white px-4 py-2 rounded-md hover:bg-[#a63d2e] text-sm font-bold flex items-center justify-center gap-2 whitespace-nowrap"><Plus size={15}/> Create Bucket</button>
                     </div>
                   </div>
                   <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
                      <div className="bg-white border border-gray-200 rounded-md p-5 h-[600px] flex flex-col text-left shadow-sm">
                         <h3 className="font-bold text-gray-800 mb-4 flex items-center gap-2 text-sm"><Database size={16} className="text-[#9c3029]"/> Buckets</h3>
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
                   {selectedBucket && (
                     <div className="bg-white border border-gray-200 rounded-md shadow-sm overflow-hidden text-left">
                       <div className="px-5 py-4 bg-gray-50 border-b border-gray-100 flex items-center justify-between gap-3">
                         <h3 className="font-bold text-gray-800 flex items-center gap-2 text-sm"><Settings size={16} className="text-[#9c3029]"/> Bucket Settings</h3>
                         <button
                           type="button"
                           onClick={() => loadSelectedBucketSettings(storageProfile, selectedBucket)}
                           className="px-2.5 py-1.5 bg-white border border-gray-200 rounded-md text-xs font-semibold text-gray-600 hover:text-[#9c3029]"
                         >
                           {bucketProtectionLoading ? 'Checking...' : 'Refresh'}
                         </button>
                       </div>
                       <div className="p-5 space-y-5">
                         {bucketProtection ? (
                           <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
                             <div className="border border-gray-200 rounded-md p-3">
                               <div className="text-[10px] uppercase font-bold text-gray-400">Default Tier</div>
                               <div className="mt-1 text-sm font-bold text-gray-800">{bucketProtection.storage_tier || 'Standard'}</div>
                             </div>
                             <div className="border border-gray-200 rounded-md p-3">
                               <div className="text-[10px] uppercase font-bold text-gray-400">Versioning</div>
                               <div className={`mt-1 inline-flex px-2 py-0.5 rounded-full border text-[10px] font-bold uppercase ${
                                 bucketProtection.versioning_enabled
                                   ? 'text-green-700 bg-green-50 border-green-200'
                                   : bucketProtection.versioning_suspended
                                     ? 'text-amber-700 bg-amber-50 border-amber-200'
                                     : 'text-gray-600 bg-gray-50 border-gray-200'
                               }`}>
                                 {bucketProtection.versioning || 'Disabled'}
                               </div>
                             </div>
                             <div className="border border-gray-200 rounded-md p-3">
                               <div className="text-[10px] uppercase font-bold text-gray-400">Auto-Tiering</div>
                               <div className={`mt-1 inline-flex px-2 py-0.5 rounded-full border text-[10px] font-bold uppercase ${bucketProtection.auto_tiering_enabled ? 'text-green-700 bg-green-50 border-green-200' : 'text-gray-600 bg-gray-50 border-gray-200'}`}>
                                 {bucketProtection.auto_tiering_enabled ? 'ON' : 'OFF'}
                               </div>
                             </div>
                             <div className="border border-gray-200 rounded-md p-3">
                               <div className="text-[10px] uppercase font-bold text-gray-400">Rules</div>
                               <div className="mt-1 text-sm font-bold text-gray-800">{bucketProtection.lifecycle_rule_count || 0} lifecycle / {bucketProtection.retention_rule_count || 0} WORM</div>
                             </div>
                           </div>
                         ) : (
                           <div className="text-xs text-gray-400">{bucketProtectionLoading ? 'Loading bucket settings...' : 'Bucket settings are not loaded.'}</div>
                         )}
                         <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                           <button
                             type="button"
                             onClick={() => handleSetBucketAutoTiering(bucketProtection?.auto_tiering_enabled ? 'Disabled' : 'InfrequentAccess')}
                             disabled={bucketProtectionLoading || !bucketProtection || (!bucketProtection.auto_tiering_enabled && !bucketProtection.can_enable_auto_tiering)}
                             className="bg-white border border-gray-200 text-gray-600 py-2 rounded-md font-semibold hover:text-[#9c3029] hover:bg-gray-50 disabled:opacity-60 flex items-center justify-center gap-2 text-xs"
                           >
                             {bucketProtectionLoading ? <Loader2 className="animate-spin" size={14} /> : <Archive size={14} />}
                             {bucketProtection?.auto_tiering_enabled
                               ? 'Disable Auto-Tiering'
                               : autoTieringBlockedByLifecycle
                                 ? 'Auto-Tiering Unavailable'
                                 : 'Enable Auto-Tiering'}
                           </button>
                           <button
                             type="button"
                             onClick={() => handleSetBucketVersioning(bucketProtection?.versioning_enabled ? 'Suspended' : 'Enabled')}
                             disabled={bucketProtectionLoading || !bucketProtection || (bucketProtection.versioning_enabled ? !bucketProtection.can_suspend_versioning : !bucketProtection.can_enable_versioning)}
                             className={`py-2 rounded-md font-semibold disabled:opacity-60 flex items-center justify-center gap-2 text-xs ${
                               bucketProtection?.versioning_enabled
                                 ? 'bg-white border border-gray-200 text-gray-600 hover:text-[#9c3029] hover:bg-gray-50'
                                 : 'bg-[#9c3029] text-white hover:bg-[#7a2520]'
                             }`}
                           >
                             {bucketProtectionLoading ? <Loader2 className="animate-spin" size={14} /> : <Shield size={14} />}
                             {bucketProtection?.versioning_enabled ? 'Suspend Object Versioning' : 'Enable Object Versioning'}
                           </button>
                         </div>

                         {autoTieringBlockedByLifecycle && (
                           <div className="text-[10px] text-gray-600 bg-gray-50 border border-gray-200 rounded-md p-2">
                             Auto-Tiering is currently off. It cannot be enabled while this bucket has a lifecycle rule that moves objects to Infrequent Access; lifecycle rules can still be saved.
                           </div>
                         )}
                         {bucketProtection && !bucketProtection.can_enable_versioning && !bucketProtection.versioning_enabled && (
                           <div className="text-[10px] text-amber-700 bg-amber-50 border border-amber-100 rounded-md p-2">
                             Object Versioning cannot be enabled while OCI retention rules are active on this bucket.
                           </div>
                         )}

                         <div className="border border-gray-300 rounded-md overflow-hidden bg-gray-100 text-black">
                           <div className="px-4 py-3 border-b border-gray-300 flex items-center justify-between gap-3">
                             <div>
                               <h4 className="text-sm font-bold text-black flex items-center gap-2"><Shield size={16}/> OCI Retention Rules (WORM)</h4>
                               <p className="mt-1 text-[11px] text-black">Immutable/WORM retention rules are managed in the OCI Dashboard. OCI Migrator only shows the current WORM rule count here.</p>
                             </div>
                             <div className="flex items-center gap-2 shrink-0">
                               <span className="px-2 py-1 rounded-full border border-gray-300 bg-white text-[10px] font-bold uppercase text-black">{bucketProtection?.retention_rule_count || 0} WORM</span>
                               <button
                                 type="button"
                                 disabled
                                 className="px-2.5 py-1.5 bg-gray-100 border border-gray-300 rounded-md text-[11px] font-semibold text-black cursor-not-allowed flex items-center gap-1"
                               >
                                 <Plus size={13} /> Create Rule in OCI Dashboard
                               </button>
                             </div>
                           </div>
                           <div className="p-4 text-xs text-black leading-5">
                             <p>Enable or change retention rules from the OCI bucket page when immutable protection is required.</p>
                             <p className="mt-2">
                               If you have active retention rules, you cannot update, overwrite, or delete objects and their metadata, or delete buckets, until the retention duration expires, or the retention rule is deleted.{' '}
                               <a
                                 href="https://docs.oracle.com/en-us/iaas/Content/Object/Tasks/usingretentionrules.htm"
                                 target="_blank"
                                 rel="noreferrer"
                                 className="font-semibold text-black underline underline-offset-2"
                               >
                                 Learn more about data retention rules
                               </a>.
                             </p>
                           </div>
                         </div>

                         <div className="border border-gray-200 rounded-md overflow-hidden">
                           <div className="px-4 py-3 bg-gray-50 border-b border-gray-100 flex items-center justify-between gap-3">
                             <div>
                               <h4 className="text-sm font-bold text-gray-800 flex items-center gap-2"><Archive size={16}/> OCI Lifecycle Policy Rules</h4>
                               <p className="mt-1 text-[11px] text-gray-500">Each action is saved as a separate OCI lifecycle rule with its own object name filters.</p>
                             </div>
                             <div className="flex items-center gap-3">
                               <label className="flex items-center gap-2 text-xs font-semibold text-gray-600">
                                 <input
                                   type="checkbox"
                                   checked={Boolean(bucketLifecycleForm.enabled)}
                                   onChange={e => setBucketLifecycleForm(prev => ({ ...prev, enabled: e.target.checked }))}
                                   className="accent-[#9c3029]"
                                 />
                                 Enabled
                               </label>
                               <button
                                 type="button"
                                 onClick={addLifecycleRule}
                                 className="bg-white border border-gray-200 px-2.5 py-1.5 rounded-md text-[11px] font-semibold text-gray-600 hover:text-[#9c3029] flex items-center gap-1"
                               >
                                 <Plus size={13} /> Create Rule
                               </button>
                             </div>
                           </div>
                           {bucketLifecycleNotice && (
                             <div className={`mx-4 mt-4 rounded-md border px-3 py-2 text-xs font-semibold ${
                               bucketLifecycleNotice.type === 'success'
                                 ? 'border-green-200 bg-green-50 text-green-700'
                                 : 'border-red-200 bg-red-50 text-red-700'
                             }`}>
                               {bucketLifecycleNotice.message}
                             </div>
                           )}
                           {bucketLifecycleForm.enabled ? (
                             <div className="p-4 space-y-3">
                               {(bucketLifecycleForm.rules || []).map((rule, ruleIndex) => {
                                 const filters = normalizeLifecycleFilters(rule);
                                 const targetActions = lifecycleActionsForTarget(rule.target);
                                 return (
                                   <div key={`${rule.name || 'rule'}-${ruleIndex}`} className="border border-gray-200 rounded-md overflow-hidden">
                                     <div className="p-3 bg-gray-50 border-b border-gray-100 grid grid-cols-1 lg:grid-cols-[1.4fr_1fr_1fr_140px_90px_40px] gap-2 items-end">
                                       <div>
                                         <label className="text-[10px] uppercase font-bold text-gray-500">Name</label>
                                         <input
                                           value={rule.name || ''}
                                           onChange={e => updateLifecycleRule(ruleIndex, { name: e.target.value })}
                                           placeholder="lifecycle-rule"
                                           className="mt-1 w-full bg-white border border-gray-200 p-2 rounded-md text-xs font-mono focus:outline-none focus:border-[#9c3029]"
                                         />
                                       </div>
                                       <div>
                                         <label className="text-[10px] uppercase font-bold text-gray-500">Target</label>
                                         <select
                                           value={rule.target || 'objects'}
                                           onChange={e => updateLifecycleRule(ruleIndex, { target: e.target.value })}
                                           className="mt-1 w-full bg-white border border-gray-200 p-2 rounded-md text-xs font-semibold text-gray-700 focus:outline-none focus:border-[#9c3029]"
                                         >
                                           {Object.entries(LIFECYCLE_TARGET_LABELS).map(([value, label]) => (
                                             <option key={value} value={value}>{label}</option>
                                           ))}
                                         </select>
                                       </div>
                                       <div>
                                         <label className="text-[10px] uppercase font-bold text-gray-500">Lifecycle Action</label>
                                         <select
                                           value={normalizeLifecycleAction(rule.action, rule.target)}
                                           onChange={e => updateLifecycleRule(ruleIndex, { action: e.target.value })}
                                           className="mt-1 w-full bg-white border border-gray-200 p-2 rounded-md text-xs font-semibold text-gray-700 focus:outline-none focus:border-[#9c3029]"
                                         >
                                           {targetActions.map(action => (
                                             <option key={action} value={action}>{LIFECYCLE_ACTION_LABELS[action]}</option>
                                           ))}
                                         </select>
                                       </div>
                                       <div>
                                         <label className="text-[10px] uppercase font-bold text-gray-500">Number of Days</label>
                                         <input
                                           type="number"
                                           min="1"
                                           value={rule.days ?? ''}
                                           onChange={e => updateLifecycleRule(ruleIndex, { days: e.target.value })}
                                           placeholder="30"
                                           className="mt-1 w-full bg-white border border-gray-200 p-2 rounded-md text-xs focus:outline-none focus:border-[#9c3029]"
                                         />
                                       </div>
                                       <label className="flex items-center gap-2 text-xs font-semibold text-gray-600 pb-2">
                                         <input
                                           type="checkbox"
                                           checked={rule.enabled !== false}
                                           onChange={e => updateLifecycleRule(ruleIndex, { enabled: e.target.checked })}
                                           className="accent-[#9c3029]"
                                         />
                                         Enabled
                                       </label>
                                       <button
                                         type="button"
                                         onClick={() => removeLifecycleRule(ruleIndex)}
                                         className="h-9 w-9 border border-gray-200 rounded-md text-gray-400 hover:text-[#9c3029] hover:bg-white flex items-center justify-center"
                                         title="Remove rule"
                                       >
                                         <Trash2 size={14} />
                                       </button>
                                     </div>
                                     {rule.target !== 'multipart-uploads' && (
                                       <div className="p-3 space-y-2">
                                         <div className="flex items-center justify-between gap-3">
                                           <div>
                                             <div className="text-[10px] uppercase font-bold text-gray-500">Object Name Filters</div>
                                             <div className="text-[10px] text-gray-500">No filters means all objects. Exclude patterns take precedence.</div>
                                           </div>
                                           <button
                                             type="button"
                                             onClick={() => addLifecycleRuleFilter(ruleIndex)}
                                             className="bg-white border border-gray-200 px-2.5 py-1.5 rounded-md text-[11px] font-semibold text-gray-600 hover:text-[#9c3029] flex items-center gap-1"
                                           >
                                             <Plus size={13} /> Add Filter
                                           </button>
                                         </div>
                                         {filters.length ? (
                                           filters.map((filter, filterIndex) => (
                                             <div key={`${filter.type}-${filterIndex}`} className="grid grid-cols-1 md:grid-cols-[220px_1fr_40px] gap-2 items-center">
                                               <select
                                                 value={filter.type}
                                                 onChange={e => updateLifecycleRuleFilter(ruleIndex, filterIndex, { type: e.target.value })}
                                                 className="bg-white border border-gray-200 p-2 rounded-md text-xs font-semibold text-gray-700 focus:outline-none focus:border-[#9c3029]"
                                               >
                                                 {Object.entries(LIFECYCLE_FILTER_LABELS).map(([value, label]) => (
                                                   <option key={value} value={value}>{label}</option>
                                                 ))}
                                               </select>
                                               <input
                                                 value={filter.value}
                                                 onChange={e => updateLifecycleRuleFilter(ruleIndex, filterIndex, { value: e.target.value })}
                                                 placeholder={filter.type === 'include_prefix' ? 'backup/customer-a/' : '*.tmp'}
                                                 className="bg-white border border-gray-200 p-2 rounded-md text-xs font-mono focus:outline-none focus:border-[#9c3029]"
                                               />
                                               <button
                                                 type="button"
                                                 onClick={() => removeLifecycleRuleFilter(ruleIndex, filterIndex)}
                                                 className="h-9 w-9 border border-gray-200 rounded-md text-gray-400 hover:text-[#9c3029] hover:bg-gray-50 flex items-center justify-center"
                                                 title="Remove filter"
                                               >
                                                 <Trash2 size={14} />
                                               </button>
                                             </div>
                                           ))
                                         ) : (
                                           <div className="text-xs text-gray-400">This rule applies to the whole bucket.</div>
                                         )}
                                       </div>
                                     )}
                                   </div>
                                 );
                               })}
                               {!(bucketLifecycleForm.rules || []).length && (
                                 <div className="p-4 text-xs text-gray-400 border border-gray-200 rounded-md">No managed lifecycle rules yet.</div>
                               )}
                               <div className="text-[10px] text-gray-500">
                                 OCI does not allow Auto-Tiering together with lifecycle rules that move objects to Infrequent Access.
                               </div>
                             </div>
                           ) : (
                             <div className="p-4 text-xs text-gray-400">Managed lifecycle rules are disabled for this bucket.</div>
                           )}
                           <div className="px-4 py-3 border-t border-gray-100 bg-white flex justify-end">
                             <button onClick={handleSaveBucketLifecyclePolicy} disabled={savingBucketSettings} className="bg-[#9c3029] text-white px-4 py-2 rounded-md text-xs font-bold hover:bg-[#7a2520] disabled:opacity-60 flex items-center gap-2">
                               {savingBucketSettings ? <Loader2 className="animate-spin" size={14} /> : <Save size={14} />}
                               Save Lifecycle
                             </button>
                           </div>
                         </div>

                       </div>
                     </div>
                   )}
                   </>
                )}
             </div>
          )}
        </div>

      </main>
    </div>
  );
}
