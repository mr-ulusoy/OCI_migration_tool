# OCI Migrator Pro Test Plan

This test plan is used to validate OCI Migrator Pro before production use, after upgrades, and before major releases.

The goal is to prove that the system can:

- install cleanly on a new server
- authenticate and keep stable admin sessions
- back up data to OCI Object Storage
- restore backed up data
- use local folders, mounted external shares, SMB, and NFSv4
- manage backup jobs, logs, cleanup, lifecycle settings, and monitoring
- scan and migrate OCI VM boot images with clear data-volume warnings
- report understandable errors when OCI, rclone, storage, or services fail

Do not run destructive tests against production VMs, production buckets, or production shares.

## Test Environments

Use at least two environments.

| Environment | Purpose |
| --- | --- |
| Lab server | Full install, SMB/NFS, local backup, restore, upgrade, service restart tests |
| OCI test tenancy/profile | Object Storage bucket tests, lifecycle rules, VM image migration tests |
| Optional second OCI tenancy/profile | Tenant-to-tenant VM image migration validation |
| Optional client machine | SMB/NFS client access validation from the same private network |

Recommended lab server:

- Ubuntu 22.04 or 24.04
- outbound HTTPS access to OCI APIs and GitHub
- private network access from at least one test client
- enough temporary disk space for restore tests
- no production workloads

## Test Data

Create small but realistic data first.

```bash
sudo mkdir -p /var/lib/oci-migrator/local/test-basic/subfolder
sudo bash -c 'printf "hello\n" > /var/lib/oci-migrator/local/test-basic/file-1.txt'
sudo bash -c 'printf "file with spaces\n" > "/var/lib/oci-migrator/local/test-basic/file with spaces.txt"'
sudo bash -c 'dd if=/dev/urandom of=/var/lib/oci-migrator/local/test-basic/random-10m.bin bs=1M count=10 status=none'
sudo bash -c 'printf "nested\n" > /var/lib/oci-migrator/local/test-basic/subfolder/nested.txt'
sudo touch -d "2023-01-01 10:00:00" /var/lib/oci-migrator/local/test-basic/file-1.txt
sudo chown -R ubuntu:ubuntu /var/lib/oci-migrator/local/test-basic
```

For larger throughput tests, create a separate dataset and do not mix it with functional tests.

## Pass Criteria

A release is acceptable only when:

- install and health checks pass
- at least one backup and restore path passes with verification
- job history survives service restart
- monitoring endpoints return usable data
- SMB/NFS only open when enabled and are reachable from the intended private clients
- failed jobs show useful UI messages and log files
- VM image migration clearly shows boot-only behavior and attached data-volume warnings
- no test leaves public SMB/NFS exposure, stale shares, or unwanted lifecycle rules behind

## Quick Regression Checklist

Run this after small changes.

- [ ] `git status` is clean before deployment
- [ ] fresh install or upgrade completes
- [ ] login works
- [ ] `/health` is OK
- [ ] Settings -> Time & NTP is OK
- [ ] create or select OCI profile
- [ ] list buckets
- [ ] create one manual backup job
- [ ] run job manually
- [ ] Recent Runs shows success and rclone summary
- [ ] download job log
- [ ] restore to temporary folder and compare files
- [ ] restart API and worker
- [ ] job history still shows previous run
- [ ] `/monitoring/status` and `/metrics` return data with token

## Full Acceptance Tests

### TP-001 Fresh Install

Purpose: prove repeatable installation on a clean Ubuntu server.

Steps:

1. Provision a new Ubuntu lab server.
2. Run:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/mr-ulusoy/OCI_migration_tool/main/scripts/bootstrap.sh -o bootstrap.sh
   chmod +x bootstrap.sh
   ./bootstrap.sh --public-host <server-ip-or-dns> --prompt-admin-password
   ```

3. Open `http://<server-ip-or-dns>:8000`.
4. Log in with the admin password.
5. Run:

   ```bash
   make doctor
   make status
   ```

Expected result:

- installer completes without manual code edits
- local Ubuntu firewall allows TCP 22, 8000 or the chosen app/API port, 445, and 2049
- API, worker, Redis, and scheduler are running
- frontend loads from the backend
- admin login works
- `make doctor` passes or only reports expected lab warnings

### TP-002 Health, Time, and NTP

Purpose: prove timestamps and schedules are based on a synchronized server clock.

Steps:

1. Open Settings -> Time & NTP.
2. Confirm timezone and NTP state.
3. Run:

   ```bash
   curl http://127.0.0.1:8000/health
   timedatectl
   ```

Expected result:

- `/health` reports timezone, time sync, and NTP service
- `timedatectl` shows synchronized time
- UI shows clear status for NTP and timezone

### TP-003 Admin Session and Logout

Purpose: prove auth state is predictable after login, logout, and restart.

Steps:

1. Log in as admin.
2. Navigate between Job Dashboard, New Backup Job, OCI Object Storage, VM Image Migration, and Settings.
3. Log out from the sidebar.
4. Try to open a protected page.
5. Restart services:

   ```bash
   make restart
   ```

6. Log in again.

Expected result:

- protected pages require login
- stale "Invalid or missing admin session" banners do not remain after a valid login
- logout clears access
- restart does not leave misleading UI state

### TP-004 Runtime Config Export and Import

Purpose: prove a server can be rebuilt from exported runtime configuration.

Steps:

1. Configure at least one OCI profile, one rclone remote, and one backup job.
2. Export runtime config from Settings.
3. Store the zip securely.
4. Install OCI Migrator Pro on a clean lab server.
5. Import the zip from Settings.
6. Restart services if requested by the UI.
7. Check profiles, remotes, jobs, job history, and key files.

Expected result:

- import restores runtime env, OCI config, rclone config, jobs, history, and bundled key files
- existing config is backed up before restore
- UI clearly warns that the zip contains secrets

### TP-005 OCI Profile and Bucket Listing

Purpose: prove OCI credentials can read Object Storage.

Steps:

1. Add an OCI profile in Credentials.
2. Open OCI Object Storage.
3. Select the profile.
4. List buckets.
5. Select a bucket and list objects.

Expected result:

- profile is saved
- buckets load
- object listing works
- OCI errors show actionable messages

### TP-006 Create Bucket

Purpose: prove the app can create buckets with supported settings.

Steps:

1. Open OCI Object Storage.
2. Select profile.
3. Create a test bucket with Default Tier `Standard`.
4. Create a second test bucket with Default Tier `Archive`, if desired for archive-specific testing.
5. Test object versioning choice during bucket creation.
6. Refresh bucket list.

Expected result:

- bucket is created in OCI
- default tier and versioning status match OCI
- Infrequent Access is not offered as a bucket default tier
- Auto-Tiering is handled as a bucket feature, not a default tier

### TP-007 Bucket Settings, Versioning, Auto-Tiering, and Lifecycle Rules

Purpose: prove bucket-level settings are visible and lifecycle rules can be managed.

Steps:

1. Select a test bucket.
2. Refresh Bucket Settings.
3. Enable Object Versioning.
4. Suspend Object Versioning.
5. Enable Auto-Tiering on a bucket without Infrequent Access lifecycle rules.
6. Create lifecycle policy rules:
   - Move to Infrequent Access after 30 days
   - Move to Archive after 90 days
   - Delete after 365 days
7. Add object name filters:
   - Include by prefix
   - Include by pattern
   - Exclude by pattern
8. Save lifecycle rules.
9. Confirm rules in OCI Console.
10. Confirm WORM retention rules are shown as status only and link to OCI docs.

Expected result:

- versioning can be enabled and suspended
- Auto-Tiering status refreshes correctly from OCI
- lifecycle actions are saved as separate OCI lifecycle rules
- object name filters match OCI behavior
- lifecycle success/error notice appears close to the lifecycle section
- app does not manage WORM retention rules

### TP-008 Local Folder Backup to OCI Object Storage

Purpose: prove backup from managed local folder to OCI Object Storage.

Steps:

1. Create or select a Server Local Folder remote.
2. Put test data under the local folder.
3. Create New Backup Job:
   - source: local folder
   - destination: OCI profile and test bucket
   - mode: COPY
   - metadata tags, for example `site=stockholm`, `ticket-id=TEST-001`
   - bandwidth limit, for example `100M`
   - API TPS limit, for example `5`
4. Save job.
5. Click Run Now.
6. Open Recent Runs and the job log.

Expected result:

- job saves successfully
- manual run starts
- job reaches Success
- rclone summary shows transferred files/bytes
- OCI objects exist under expected bucket/prefix
- object metadata appears in OCI as `opc-meta-site` and `opc-meta-ticket-id`

### TP-009 Restore From OCI Object Storage

Purpose: prove backed up data can be restored and verified.

Steps:

1. Create a clean restore directory:

   ```bash
   mkdir -p /tmp/oci-migrator-restore-test
   ```

2. Restore data using rclone or the configured restore workflow:

   ```bash
   rclone copy <remote>:<bucket>/<prefix> /tmp/oci-migrator-restore-test --progress
   ```

3. Compare source and restored data:

   ```bash
   rclone check /var/lib/oci-migrator/local/test-basic /tmp/oci-migrator-restore-test --one-way
   ```

Expected result:

- restored files match source files
- missing or changed files are reported clearly
- restore command does not overwrite unrelated local data

### TP-010 Existing Mounted SMB Share as Source

Purpose: prove an external SMB share mounted on the server can be used as a backup source.

Steps:

1. Mount an existing SMB share on the server, for example:

   ```bash
   sudo mkdir -p /mnt/test-smb
   sudo mount -t cifs //<share-server>/<share-name> /mnt/test-smb -o username=<user>,vers=3.0
   ```

2. Add Remote -> Local / Mounted Share -> Mounted External Share.
3. Point it to `/mnt/test-smb`.
4. Create a backup job from that remote to OCI Object Storage.
5. Run job.
6. Restore and verify.

Expected result:

- app accepts the mounted share path
- backup succeeds
- restore verifies
- mount errors show clear messages

### TP-011 Existing Mounted NFSv4 Share as Source

Purpose: prove an external NFSv4 share mounted on the server can be used as a backup source.

Steps:

1. Mount an existing NFSv4 share:

   ```bash
   sudo mkdir -p /mnt/test-nfs
   sudo mount -t nfs4 <share-server>:<export-path> /mnt/test-nfs
   ```

2. Add Remote -> Local / Mounted Share -> Mounted External Share.
3. Point it to `/mnt/test-nfs`.
4. Create a backup job to OCI Object Storage.
5. Run job.
6. Restore and verify.

Expected result:

- app accepts the mounted path
- backup succeeds
- restore verifies
- permission or stale mount issues appear in the job log and UI

### TP-012 Managed SMB Share

Purpose: prove the app can expose a managed local folder as SMB only when selected.

Steps:

1. Add Remote -> Local / Mounted Share -> Server Local Folder.
2. Choose SMB `Share to User`.
3. Enter share name, SMB user, and SMB password.
4. Save remote.
5. From a client in the same private network, test:

   ```bash
   smbclient -L //<server-private-ip> -U <smb-user>
   smbclient //<server-private-ip>/<share-name> -U <smb-user>
   ```

6. Upload a file through SMB.
7. Run a backup job from that local folder.

Expected result:

- Samba is configured only after SMB is enabled
- TCP 445 is open on the server firewall when supported
- share is reachable from allowed private network path
- uploaded file appears in the managed local folder
- backup includes uploaded file

### TP-013 Managed NFSv4 Share

Purpose: prove the app can expose a managed local folder as NFSv4 only when selected.

Steps:

1. Add Remote -> Local / Mounted Share -> Server Local Folder.
2. Enable NFSv4 Share.
3. Add allowed client IP, hostname, or CIDR.
4. Save remote.
5. From an allowed client, mount:

   ```bash
   sudo mkdir -p /mnt/oci-migrator-nfs-test
   sudo mount -t nfs4 <server-private-ip>:<export-path> /mnt/oci-migrator-nfs-test
   ```

6. Write a test file.
7. Run a backup job from that local folder.

Expected result:

- NFS server/export is configured only after NFS is enabled
- TCP 2049 is open on the server firewall when supported
- only allowed clients can mount
- test file backs up successfully

### TP-014 Share Cleanup

Purpose: prove deleting a remote removes share configuration without deleting data.

Steps:

1. Create a managed SMB/NFS remote.
2. Confirm client access works.
3. Delete the remote in the UI.
4. Try to reconnect from client.
5. Check the local data folder on the server.

Expected result:

- SMB/NFS share block is removed
- client can no longer access the share through the removed export/share
- underlying local folder and data remain unless manually deleted

### TP-015 Local Cleanup Policy

Purpose: prove per-job local cleanup deletes only safe files after successful backup.

Steps:

1. Create a managed local folder source.
2. Add files older than the retention threshold.
3. Add files modified within the safety window.
4. Enable Local Cleanup on the backup job:
   - delete files older than 30 days
   - ignore files modified in last 24 hours
5. Run job successfully.
6. Inspect source folder and job history.
7. Create a second job with cleanup enabled for the same source path.

Expected result:

- old files are deleted only after a successful backup
- recently modified files are skipped
- empty child directories are removed when safe
- cleanup result is visible in job history
- duplicate cleanup ownership for the same source is blocked

### TP-016 Job History, Logs, and Rotation

Purpose: prove job history and logs survive service restarts and rotate correctly.

Steps:

1. Run a successful backup job.
2. Run a failing backup job, for example with an invalid bucket.
3. Restart services:

   ```bash
   make restart
   ```

4. Open Job Dashboard -> Recent Runs.
5. Download logs.
6. Open Settings -> Job Log Rotation.
7. Change retention days and max size.
8. Save.
9. Check:

   ```bash
   sudo cat /etc/logrotate.d/migrator-job-logs
   ```

Expected result:

- success and failure runs remain visible after restart
- logs are downloadable
- failure has actionable message
- logrotate settings match UI values

### TP-017 Rclone Transfer Controls and JSON Log Summary

Purpose: prove bandwidth/API limits and parsed progress information work.

Steps:

1. Set Settings -> Backup Job Defaults:
   - Bandwidth Limit: `100M`
   - API TPS Limit: `5`
2. Create a new backup job and confirm defaults are applied.
3. Override limits on the job.
4. Run job.
5. Check job history and raw log.

Expected result:

- new jobs inherit settings defaults
- per-job override is saved
- rclone command uses `--bwlimit`, `--tpslimit`, and JSON logging
- dashboard shows useful transfer summary without requiring raw log reading

### TP-018 Monitoring Endpoints

Purpose: prove monitoring systems can pull service and backup state.

Steps:

1. Generate or configure an API token.
2. Run:

   ```bash
   curl http://127.0.0.1:8000/health
   curl -H "X-API-Token: <token>" http://127.0.0.1:8000/monitoring/status
   curl -H "X-API-Token: <token>" http://127.0.0.1:8000/metrics
   ```

3. Validate Prometheus scrape configuration if used.

Expected result:

- `/health` is available without login for basic service checks
- `/monitoring/status` returns JSON for services, backup jobs, latest runs, disk, NTP, and errors
- `/metrics` returns Prometheus text metrics
- requests without token are rejected for authenticated endpoints

### TP-019 Local Disk Usage Warnings

Purpose: prove local data disk thresholds are visible and useful.

Steps:

1. Open Settings -> Local Disk Usage.
2. Set low warning and critical thresholds in a lab environment.
3. Refresh `/health`, `/monitoring/status`, and `/metrics`.

Expected result:

- UI shows warning/critical when threshold is exceeded
- monitoring status reflects disk state
- Prometheus metrics expose used percent and free bytes

### TP-020 System Upgrade

Purpose: prove the app can check and apply GitHub upgrades.

Steps:

1. Open Settings -> System Upgrade.
2. Click Check.
3. If already current, confirm status says the system is on the latest version.
4. If a newer version exists, click Upgrade in a lab environment.
5. Confirm services restart and app loads.

Expected result:

- check reports installed and latest commit
- no-update case is clear
- upgrade pulls latest code and reruns install safely
- upgrade log is visible

### TP-021 Backup Job Edit, Run Now, and Delete

Purpose: prove existing jobs remain manageable.

Steps:

1. Create a backup job.
2. Edit source/destination/schedule/limits/metadata.
3. Save.
4. Run Now.
5. View Log.
6. Delete job.

Expected result:

- edit preserves unchanged fields
- Run Now works after edit
- delete removes scheduled job
- lifecycle rules owned by the job are removed or updated when destination/lifecycle changes

### TP-022 Failure Scenarios

Purpose: prove failures are visible, understandable, and do not corrupt state.

Run these in a lab only.

| Scenario | Expected result |
| --- | --- |
| Invalid OCI key or profile | UI explains OCI auth/permission failure |
| Missing bucket | job fails with clear bucket/object storage message |
| Invalid local path | job fails before unsafe cleanup |
| SMB wrong password | client cannot connect and UI/helper reports useful error |
| NFS client not allowed | mount fails from blocked client |
| Worker restart during job | history keeps queued/running/failure state understandable |
| Redis unavailable | health and monitoring report Redis problem |
| Full local disk | health/monitoring report disk warning or critical |
| Lifecycle conflict with Auto-Tiering | UI explains OCI conflict and still allows valid lifecycle saves |

### TP-023 VM Image Migration Scan

Purpose: prove VM scan gives enough information before migration.

Steps:

1. Configure source OCI profile.
2. Open VM Image Migration.
3. Select source profile.
4. Scan VMs.
5. Filter by name, OS, shape, IP, and OCID.
6. Inspect rows.

Expected result:

- VMs list as rows
- full VM name is visible
- running/stopped state is visible under VM name
- OS and shape are visible
- OCPU/RAM are visible
- public/private IPs are visible
- attached data volumes are shown when supported by the scanner
- UI warning says boot volume image only and data volumes must be migrated separately

### TP-024 VM Image Migration Boot Disk

Purpose: prove boot image migration works and does not imply data volume migration.

Steps:

1. Use a small non-production source VM.
2. Attach a small data volume to prove it is not included in the boot image.
3. Select destination profile and bucket.
4. Start migration.
5. Confirm source VM stop behavior is clearly stated.
6. Track migration status.
7. Launch/import in destination workflow as supported.

Expected result:

- app queues migration
- user sees that selected VM may be stopped
- boot image export/import workflow completes
- data volume is not claimed as migrated
- logs/history identify the VM and task result

### TP-025 Data Volume Migration Decision Test

Purpose: force the team to choose the correct method for attached data volumes.

Steps:

1. For each VM with attached data volumes, classify every volume:
   - file data only
   - application data
   - database data
   - Windows application/data disk
   - special layout such as LVM, RAID, or dynamic disk
2. Choose migration method:
   - file-level backup job for normal files
   - app/database-native backup when consistency is required
   - advanced block-level disk image migration only when exact disk layout is required
3. Record downtime/freeze requirements.

Expected result:

- no data disk is treated as automatically included in VM image migration
- owner accepts the chosen migration method
- required downtime or application stop is documented

### TP-026 DHCP and Static IPv4

Purpose: prove dashboard network changes are validated and cannot silently lock out the server.

Steps:

1. Record the current interface, IPv4 address, prefix, gateway, and DNS servers.
2. Apply DHCP to the current interface and confirm it from Settings.
3. Verify the address remains reachable and the rollback timer becomes inactive.
4. Reserve a second test address on the cloud VNIC or lab network before testing static mode.
5. Apply the reserved static IPv4 address, open the dashboard on that address, sign in, and confirm it.
6. Stage another safe test change but do not confirm it; verify the previous configuration returns after three minutes.
7. Return the server to its intended production mode and remove the temporary test address.

Expected result:

- invalid IPv4, prefix, gateway, DNS, and interface values are rejected
- DHCP and reserved static IPv4 settings persist after confirmation
- an unconfirmed change rolls back automatically
- the rollback timer is disabled after confirmation or rollback
- no static address is applied unless it is assigned or reserved outside the guest OS first

## Production Readiness Sign-Off

Before production use, record:

| Item | Value |
| --- | --- |
| Test date | |
| App commit | |
| Installer method | |
| Source OCI profile/tenant | |
| Destination OCI profile/tenant | |
| Test bucket | |
| Test server | |
| Tester | |
| Result | Pass / Fail |
| Notes | |

Required sign-off:

- [ ] install tested
- [ ] backup tested
- [ ] restore tested
- [ ] SMB/NFS tested if used
- [ ] monitoring tested
- [ ] upgrade tested
- [ ] VM image migration warning and scan tested if used
- [ ] DHCP/static IPv4 confirmation and rollback tested
- [ ] failure messages reviewed
- [ ] test shares, test buckets, test lifecycle rules, and temporary data cleaned up

## Cleanup After Testing

Clean up lab resources only after results are recorded.

- delete test backup jobs
- remove test SMB/NFS remotes
- unmount external shares
- delete test buckets or test prefixes
- remove test lifecycle rules
- remove test helper VMs and block volumes
- remove temporary restore directories
- store runtime config export securely or delete it
