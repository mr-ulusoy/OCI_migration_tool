# Acceptance Test Results - 2026-08-31

## Scope

The acceptance plan in `docs/TEST_PLAN.md` was executed against a new Ubuntu lab server.

| Item | Value |
| --- | --- |
| Test date | 2026-08-31 |
| Final app commit | `02f6a0c` |
| Installer method | Fresh installation followed by in-app System Upgrade |
| OCI profile | `Lab` |
| Lab server | `82.70.49.244` / `10.0.1.2` |
| SMB/NFS client and provider | `79.72.19.83` / `10.0.1.121` |
| Tester | Codex with user-provided lab infrastructure |
| Overall result | Pass with TP-004 partial and TP-024 skipped |

No production VM was stopped or migrated. Existing buckets, jobs, and the original `LocalShare` were not used as destructive test targets.

## Results

| Test | Result | Evidence and notes |
| --- | --- | --- |
| TP-001 Fresh Installation | PASS | Ubuntu 24.04.4; `make doctor` passed; API, worker, scheduler, Redis, rclone, logrotate, env permissions, and TCP 8000 verified. |
| TP-002 Health, Time, NTP | PASS | `/health` OK; `Europe/Stockholm`; NTP enabled and synchronized. |
| TP-003 Admin Session and Logout | PASS | Protected API access, logout revocation, login, session survival across service restart, and post-restart access verified. Navigation was code/build verified rather than browser-automated. |
| TP-004 Runtime Config Export/Import | PARTIAL | Secure mode-600 ZIP exported and re-imported successfully. Env, OCI config/key, rclone config, jobs, and history were restored and a pre-restore backup was created. A second clean rebuild server was not available. |
| TP-005 OCI Profile and Bucket Listing | PASS | `Lab` profile listed buckets and objects successfully. |
| TP-006 Create Bucket | PASS | Two isolated Standard test buckets were created through the API and read back. Archive-tier creation was optional and not used. |
| TP-007 Bucket Settings and Lifecycle | PASS | Versioning enabled then suspended; Auto-Tiering enabled/disabled; three separate OCI rules saved with include-prefix, include-pattern, and exclude-pattern filters; IA conflict returned HTTP 400; WORM remained status-only. |
| TP-008 Local Folder Backup | PASS | Four files and 10 MiB copied. Metadata became `opc-meta-site=stockholm` and `opc-meta-ticket-id=TEST-001`. `100M` bandwidth and TPS `5` appeared in the log. |
| TP-009 Restore | PASS | Restore completed; `rclone check` reported zero differences and four matching files; SHA-256 values and the 2023 source mtime matched. |
| TP-010 Mounted SMB Source | PASS | Real CIFS mount over private IP, backup success, and zero differences against OCI. |
| TP-011 Mounted NFS Source | PASS | Real NFSv4 mount over private IP, backup success, and zero differences against OCI. |
| TP-012 Managed SMB User Share | PASS | Correct credentials wrote from a separate client; wrong password was rejected; uploaded file backed up successfully. |
| TP-013 Managed NFS Share | PASS | Allowed private client mounted and wrote successfully. Root squash denied a root write while the matching normal UID worked. Backup succeeded. |
| TP-014 Share Cleanup | PASS | Remote deletion removed Samba/NFS configuration and prevented reconnection while preserving underlying data until final cleanup. |
| TP-015 Local Cleanup | PASS | Both files were backed up first; one old file was deleted, one recent file was retained; history showed cleanup details; duplicate cleanup ownership returned HTTP 400. |
| TP-016 History, Logs, Rotation | PASS | Success and failure survived restart; log download returned an attachment; logrotate changed to `2M/7`, matched the system file, then returned to `10M/14`. |
| TP-017 Rclone Controls and JSON | PASS | Defaults accepted `100M/5`; per-job overrides persisted; logs showed bandwidth/TPS limiters and JSON summaries exposed bytes, files, speed, elapsed time, errors, and last object. Defaults were restored. |
| TP-018 Monitoring | PASS | Anonymous `/health` returned 200; anonymous `/monitoring/status` and `/metrics` returned 401; authenticated endpoints returned service, backup, disk, NTP, error, and Prometheus data. |
| TP-019 Disk Warnings | PASS | Thresholds produced warning and critical states in health and monitoring; metrics exposed used percent/free bytes; thresholds restored to `80/90`. Physical disk filling was intentionally avoided. |
| TP-020 System Upgrade | PASS | Upgrade check found a newer commit, in-app upgrade completed, services restarted, log was readable, and a second check reported `up_to_date: true`. |
| TP-021 Job Edit/Run/Delete | PASS | Source/destination settings, limits, metadata, and schedule fields persisted after edit; Run Now succeeded; log showed overrides; delete removed the job. |
| TP-022 Failure Scenarios | PASS | Missing bucket/profile, invalid source, wrong SMB password, blocked NFS client, Redis outage, disk threshold, lifecycle conflict, and worker restart were exercised. Cleanup did not run after failure. |
| TP-023 VM Scan | PASS | Seven VMs listed with full name, state, OS, shape, OCPU/RAM, IPs, and boot volume name/OCID/size/state. Search includes VM and volume OCIDs. No scanned VM currently had attached data volumes. |
| TP-024 Boot Disk Migration | SKIPPED | No explicitly disposable migration VM and destination profile were available. Self-migrating the lab server would stop the worker orchestrating the test. |
| TP-025 Data Volume Decision | NOT APPLICABLE | The scanned instances had no attached data volumes. The plan still requires classification and owner acceptance before a future data-volume migration. |

## Defects Found and Fixed

1. A misspelled destination bucket was silently created by rclone instead of failing. Fixed in `dbbe10b` with API validation and worker preflight.
2. OCI `ProfileNotFound` was returned as HTTP 502. Fixed in `3b75dae` with an actionable HTTP 400 response.
3. Restarting the worker left an interrupted backup permanently in `Running`. Fixed in `7450914`; worker startup now marks interrupted data-sync runs failed with a rerun message.
4. VM scan omitted boot and attached data volumes. Fixed in `02f6a0c`; API/UI now inventory and display volumes and warn that migration exports the boot volume only.

All new backend tests passed on the installed server (`6 tests`). The frontend production build and `make doctor` passed.

## Cleanup and Final State

- Deleted all acceptance jobs, remotes, SMB/NFS shares, mounts, local test folders, restore directories, and the runtime export.
- Removed all managed lifecycle test rules.
- Deleted the three temporary OCI buckets, including the bucket created during the discovered missing-bucket defect.
- Confirmed no `acceptance*` shares, exports, folders, jobs, remotes, or buckets remain.
- Confirmed the original `LocalShare` remains unchanged.
- Confirmed final lab health is OK and TCP 8000 remains accepted by the Ubuntu host firewall.
- Restored log rotation, rclone defaults, and local disk thresholds.

## Sign-Off

- [x] install tested
- [x] backup tested
- [x] restore tested
- [x] SMB/NFS tested
- [x] monitoring tested
- [x] upgrade tested
- [x] VM image migration warning and scan tested
- [x] failure messages reviewed
- [x] temporary test resources cleaned up
- [ ] runtime import tested on a second clean rebuild server
- [ ] boot image migration tested with an explicitly disposable VM and destination tenant/profile
