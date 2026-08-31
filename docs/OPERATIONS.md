# Dashboard Configuration

This guide documents the configuration available in the OCI Migrator Pro web dashboard. For service commands, health endpoints, monitoring integrations, runtime files, and command-line recovery procedures, see the [Server Runbook](RUNBOOK.md).

## Credentials

Open `Credentials` to add source and destination profiles. Profile and remote names must be unique.

### Oracle Object Storage (OCI)

An OCI profile requires:

- `Profile Name`: the name shown throughout the dashboard.
- `Compute Compartment`: compartment OCID used when scanning instances for VM image migration.
- `Storage Compartment`: compartment OCID used for Object Storage buckets.
- `Tenancy OCID`: OCID of the OCI tenancy.
- `User OCID`: OCID of the OCI IAM user.
- `Fingerprint`: fingerprint of the uploaded API signing key.
- `Region`: OCI region identifier, for example `eu-stockholm-1`.
- `API Key`: upload or paste the private API signing key associated with the IAM user.

Use separate profiles when the source and destination belong to different OCI tenants. Grant the IAM user only the permissions required for the intended Object Storage or Compute operations.

### AWS S3 or S3-compatible storage

Configure a remote name, access key ID, secret access key, and region. The credentials must be allowed to list and read the selected source buckets and objects.

### Azure Blob Storage

Configure a remote name, storage account name, and account key.

### Google Cloud Storage

Configure a remote name and upload a service account JSON key. `Object ACL`, `Bucket ACL`, and `Location` are optional advanced values.

### Local or mounted share

Choose one of these local types:

- `Server Local Folder`: creates a managed folder under the server's configured local data root.
- `Mounted External Share`: registers an existing absolute mount path. Mount the external storage on the server before saving the remote.

A server local folder can optionally be exposed through SMB, NFSv4, or both:

- `Do Not Share`: only creates the local folder.
- `Share to Everyone`: enables guest SMB access.
- `Share to User`: requires an SMB username and a password of at least eight characters.
- `Enable NFSv4 Share`: requires one or more allowed client IP addresses, hostnames, or CIDR ranges. Wildcards are rejected.
- `Share Name`: controls the exported SMB/NFS share name.

SMB uses TCP `445` and NFSv4 uses TCP `2049`. Restrict network access outside the application to the client networks that require the share.

## New Backup Job

Open `New Backup Job` to create a scheduled or manual file/object transfer.

### Job and pipeline

- `Job Name`: unique display name for the backup job.
- `Sync Mode`: `COPY (Safe)` uploads new and changed data without deleting destination objects; `SYNC (Mirror)` also removes destination objects that no longer exist at the source.
- `Source`: source remote and source folder, bucket, container, or prefix.
- `Destination`: destination profile and OCI bucket/prefix.

Use `COPY (Safe)` unless destination deletion is an intentional part of the retention design.

### Local Cleanup

Local Cleanup is available only when the source is a managed server local folder.

- `Enabled`: runs cleanup only after the backup transfer succeeds.
- `Delete Files Older Than`: retention age in days.
- `Ignore Modified in Last`: safety window in hours; recently modified files are never removed.

Only one enabled cleanup policy can own a local source path. Cleanup removes eligible source files and empty child directories; it does not delete OCI objects.

### Object Metadata

Add optional metadata as name/value pairs. Enter names such as `site` or `ticket-id`; OCI stores them as `opc-meta-site` and `opc-meta-ticket-id`. Metadata is applied to objects uploaded by that job.

### Transfer settings

- `Transfers`: number of parallel file transfers.
- `Checkers`: number of parallel object checks.
- `Buffer Size`: per-transfer memory buffer.
- `Bandwidth Limit`: optional rclone bandwidth limit such as `700M` or `1G`; blank means unlimited.
- `API TPS Limit`: optional API transaction limit; blank or `0` means unlimited.

Higher concurrency can improve throughput but also increases memory use and API request load. Start conservatively and tune from measured backup runs.

### Schedule

- `Manual Only`: the job runs only when `Run Now` is selected.
- `Daily`: runs every day at the configured server-local time.
- `Weekly`: also requires a weekday.
- `Monthly`: also requires a day from 1 to 31.
- `Time`: interpreted using the timezone configured under `Settings` -> `Time & NTP`.

## OCI Object Storage

Select an OCI profile before creating or managing buckets.

### Create Bucket

- `New Bucket Name`: valid bucket name within the OCI namespace.
- `Default Tier`: `Standard` or `Archive`.
- `Versioning`: enabled or disabled when the bucket is created.
- `Auto-Tiering to Infrequent Access`: available for Standard-tier buckets.

Infrequent Access is reached through Auto-Tiering or a lifecycle rule; it is not a bucket default tier.

### Bucket Settings

For the selected bucket, the dashboard shows the default tier, versioning state, Auto-Tiering state, lifecycle rule count, and OCI retention/WORM rule count.

- `Enable/Suspend Object Versioning`: controls whether OCI keeps previous versions after overwrite or deletion. Existing versions remain when versioning is suspended.
- `Enable/Disable Auto-Tiering`: controls automatic movement to Infrequent Access.
- `OCI Retention Rules (WORM)`: status only. Create and manage immutable retention rules in the OCI Console. Active WORM rules can prevent object updates, metadata changes, deletion, and bucket deletion. See [OCI retention rule documentation](https://docs.oracle.com/en-us/iaas/Content/Object/Tasks/usingretentionrules.htm).

OCI does not allow Object Versioning to be enabled while active retention rules exist on the bucket. Auto-Tiering cannot be enabled while a lifecycle rule moves objects to Infrequent Access.

### OCI Lifecycle Policy Rules

Each row is saved as a separate OCI lifecycle rule:

- `Name`: unique lifecycle rule name.
- `Target`: objects, previous object versions, or uncommitted multipart uploads.
- `Lifecycle Action`: available actions depend on the selected target and include moving data to Infrequent Access, moving data to Archive, or deleting data.
- `Number of Days`: positive age threshold for the action.
- `Enabled`: activates or disables the individual rule.

Object targets support optional name filters:

- `Include by prefix`
- `Include by pattern`
- `Exclude by pattern`

No filter means the rule applies to the entire bucket. Keep each action in its own rule so its target, age, state, and filters can be managed independently.

## VM Image Migration

Open `VM Image Migration` and configure:

- `Source`: OCI profile used to scan compute instances.
- `VM`: selected instance from the scan results.
- `Destination Profile`: OCI profile that receives the exported image workflow.
- `Storage Bucket`: destination Object Storage bucket.

Executing a migration can stop the selected source VM and creates a boot-volume image backup in the selected destination workflow. The current workflow migrates the boot volume image only. Attached data volumes are displayed during scanning but must be migrated separately.

## Settings

### System Upgrade

`Check` compares the installed commit with the latest GitHub commit. `Upgrade` installs the latest version, and `Log` shows progress while the API and frontend restart. Runtime configuration is preserved by the managed upgrade process.

### Notifications

Configure outbound syslog notifications for backup results:

- `Enabled`: turns delivery on or off.
- `Syslog Server`: hostname or IP address of the receiving system.
- `Port`: UDP or TCP destination port.
- `Protocol`: UDP or TCP.
- `Facility`: syslog facility from `local0` through `local7`, `daemon`, or `user`.
- `Notify On`: failures and recovery, failures only, or all completed runs.

Use `Send Test` before enabling notifications. `Last Sent` and `Last Error` show the most recent delivery state. Delivery is best effort and a notification failure does not change the backup result.

Messages are sent outbound from OCI Migrator; no inbound syslog port is required on this server.

### Change Password

Enter the current admin password, the new password, and confirmation. Use a unique strong password and update any operational password records after the change.

### Runtime Config Backup

- `Export`: downloads a ZIP containing runtime configuration, OCI/rclone credentials, job definitions/history, and referenced key files when available.
- `Import`: uploads a previously exported ZIP and restores the contained configuration after validation.

The ZIP contains secrets. Store it encrypted or in another access-controlled location. Import can replace active credentials and job configuration, so take a fresh export before restoring an older archive.

### Network

`DHCP` is the default IPv4 mode. Static mode requires:

- `Network Interface`
- `IPv4 Address`
- `Prefix`
- `Gateway`
- `DNS Servers`

Network changes use a confirmation window. Confirm the new configuration after connectivity is verified; otherwise the server rolls it back automatically. On OCI, reserve or assign the address on the instance VNIC before setting it inside Ubuntu.

### Time & NTP

- `Timezone`: IANA timezone such as `Europe/Stockholm` or `Asia/Singapore`.
- `NTP Servers`: space-separated NTP server list.

Schedules and displayed timestamps use the configured timezone. NTP synchronizes the clock but does not select the timezone.

### Backup Job Defaults

- `Bandwidth Limit`: default rclone bandwidth limit for new jobs.
- `API TPS Limit`: default transaction limit for new jobs.

Changing defaults does not modify existing jobs. Edit an existing job to change its saved limits.

### Local Disk Usage

- `Warning %`: disk utilization that changes local disk status to warning.
- `Critical %`: higher utilization that changes status to critical.

The panel reports used, free, and total capacity for the managed local data disk. Set thresholds with enough free-space margin for the largest expected ingest batch.

### Job Log Rotation

- `Retention Days`: number of rotated daily job logs to retain.
- `Max Size`: size threshold such as `10M` or `50M` that can trigger rotation.

The log directory is displayed for reference and is not editable from the dashboard.

### Uninstall OCI Migrator

Dashboard uninstall requires the current admin password and the exact confirmation text `UNINSTALL`.

`Delete local backups stored on this server` additionally removes only the configured managed server-local backup directory. OCI Object Storage data, mounted external shares, runtime credentials, and imported source storage are not selected by that checkbox.

Export the runtime configuration before uninstalling when the installation may need to be rebuilt.
