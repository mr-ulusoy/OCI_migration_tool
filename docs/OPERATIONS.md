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

#### How to collect the OCI profile values

The quickest method is to create or inspect an API signing key in the OCI Console. OCI then displays a `Configuration File Preview` containing the user OCID, fingerprint, tenancy OCID, and region together.

1. Sign in to the [OCI Console](https://cloud.oracle.com/) and select the region that contains the resources you want to use. The current region appears in the Region menu at the top of the Console.
2. Choose a `Profile Name` yourself. It is only the friendly name shown in OCI Migrator, for example `OCI-PRODUCTION` or `SOURCE-TENANT`.
3. Find the compartment OCIDs:
   - Open the navigation menu and select `Identity & Security` -> `Compartments`.
   - Open or locate the compartment containing the compute instances and copy its OCID into `Compute Compartment`.
   - Open or locate the compartment containing the Object Storage buckets and copy its OCID into `Storage Compartment`.
   - If compute instances and buckets use the same compartment, enter the same compartment OCID in both fields.
4. Open the OCI IAM user that OCI Migrator will use:
   - For your own user, open the Profile menu and select `User settings`.
   - For another service user, open `Identity & Security` -> `Users`, then select that user. Administrator permissions may be required.
5. Under the user's resources, select `API Keys`, then select `Add API Key`.
6. Select `Generate API Key Pair`, download the **private key**, and then select `Add`. Store the downloaded PEM file securely; OCI does not provide the private key again later.
7. OCI displays the `Configuration File Preview`. Copy these values into OCI Migrator:

| Configuration File Preview | OCI Migrator field |
| --- | --- |
| `tenancy=ocid1.tenancy...` | `Tenancy OCID` |
| `user=ocid1.user...` | `User OCID` |
| `fingerprint=aa:bb:...` | `Fingerprint` |
| `region=eu-stockholm-1` | `Region` |
| Downloaded private PEM file | `API Key` using Upload API Key, or paste its PEM contents |

For an existing API key, open its Actions menu (three dots) and select `View configuration file` to display the same preview. This does not recover the private key. If the private key has been lost, create a new API key pair and remove the unusable old key after the new profile has been tested.

The API signing key is not the SSH key used to log in to a compute instance. Upload only the private PEM key belonging to the fingerprint registered on the selected IAM user. Never upload the public key, an SSH private key, or a Console password.

You can also find `Tenancy OCID` under Profile -> `Tenancy: <tenancy name>` and `User OCID` under `User settings`. See Oracle's documentation for [required keys and OCIDs](https://docs.oracle.com/en-us/iaas/Content/API/Concepts/apisigningkey.htm) and [locating compartment OCIDs](https://docs.oracle.com/en-us/iaas/Content/GSG/Tasks/contactingsupport_topic-Locating_Oracle_Cloud_Infrastructure_IDs.htm).

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

These fields control rclone concurrency, memory use, network utilization, and API request rate. Change one setting at a time and compare several completed runs before increasing it again.

#### Transfers

`Transfers` is the number of files rclone can transfer at the same time.

- Increase it when the job contains many small or medium files and the network is not fully utilized.
- Reduce it when the source disk is overloaded, memory use is high, connections are unstable, or the cloud provider starts throttling requests.
- A few very large files normally need less concurrency than millions of small files.

Recommended starting values:

| Workload | Transfers |
| --- | ---: |
| Mostly large files, 1 GB or larger | `4` to `8` |
| Mixed file sizes | `8` to `16` |
| Mostly small files | `16` to `32` |

For the planned 1 Gbit/s backup server, start at `16` for mixed data. Lower it to `8` for large sequential files or increase it carefully toward `32` only when CPU, RAM, source storage, and API limits all have available capacity.

#### Checkers

`Checkers` is the number of parallel checks used while listing and comparing source and destination objects. Checkers do not transfer file contents, but they can generate many metadata/list API requests.

- Increase it when a job spends a long time scanning before or between transfers.
- Reduce it when the source or destination reports rate limiting, timeouts, or excessive API traffic.
- A useful starting point is approximately two checkers per transfer.

Recommended starting values:

| Workload | Checkers |
| --- | ---: |
| Mostly large files | `8` to `16` |
| Mixed file sizes | `16` to `32` |
| Millions of small files | `32` to `64`, after testing |

For the planned server, start at `32` with `16` transfers.

#### Buffer Size

`Buffer Size` is the in-memory read buffer allocated for each active transfer. A larger buffer can help on high-latency paths, but it does not automatically make a fast local or OCI connection faster.

Approximate transfer buffer memory is:

```text
Transfers x Buffer Size
```

Examples:

| Transfers | Buffer Size | Approximate transfer buffers |
| ---: | ---: | ---: |
| `8` | `16M` | `128 MiB` |
| `16` | `16M` | `256 MiB` |
| `16` | `128M` | `2 GiB` |
| `16` | `512M` | `8 GiB` |

This is not the application's total memory use. rclone, multipart uploads, the API, worker, Redis, and the operating system require additional memory.

- `16M`: recommended default and safest starting point.
- `128M`: use only after testing shows that buffering improves throughput and the server has sufficient free RAM.
- `512M`: not recommended for normal use; reserve it for measured high-latency cases on a high-memory server with low transfer concurrency.

#### Bandwidth Limit

`Bandwidth Limit` caps the total rclone transfer rate for the job. Blank or `off` means unlimited.

The suffix is **bytes per second**, not bits per second. For example, `100M` is approximately 100 MiB/s or 839 Mbit/s. Do not enter `700M` to mean 700 Mbit/s; approximately `83M` represents 700 Mbit/s.

Recommended values for a 1 Gbit/s connection:

| Goal | Bandwidth Limit | Approximate network rate | Approximate maximum per day |
| --- | ---: | ---: | ---: |
| Leave capacity for other services | `80M` | 671 Mbit/s | 7.2 TB |
| Backup-focused shared link | `90M` | 755 Mbit/s | 8.2 TB |
| Mostly dedicated backup link | `100M` | 839 Mbit/s | 9.1 TB |
| Maximum available throughput | blank / `off` | Up to line speed | Up to 10.8 TB theoretical |

Daily figures are transfer-rate estimates before protocol overhead, retries, file checks, API latency, and source disk limits. Moving 10 TB every day over 1 Gbit/s requires the connection to remain close to full utilization for almost the entire day, so blank/`off` is normally required and the practical result may still be lower.

#### API TPS Limit

`API TPS Limit` caps rclone transactions per second. A transaction is approximately one backend API request. Blank or `0` means unlimited.

- Keep it blank or `0` under normal conditions.
- Start at `10` when a provider returns rate-limit responses, HTTP `429`, throttling, or repeated API timeouts.
- If the job becomes stable, increase in small steps such as `10`, `20`, and `30` while monitoring errors and completion time.
- Lower values reduce pressure on the provider but can significantly slow jobs containing many small files because each file requires multiple API operations.

Do not use TPS limiting as the primary network control. Use `Bandwidth Limit` for data throughput and `API TPS Limit` only for API request pressure.

#### Recommended baseline

For a dedicated OCI Migrator server with a 1 Gbit/s connection and mixed file sizes, use this initial profile:

| Setting | Initial value |
| --- | ---: |
| Transfers | `16` |
| Checkers | `32` |
| Buffer Size | `16M` |
| Bandwidth Limit | `90M`, or blank on a dedicated link |
| API TPS Limit | blank / `0` |

Run representative jobs before raising concurrency. If the network is below the target while CPU, RAM, source disk, and API error counts remain healthy, increase `Transfers` first. Increase `Checkers` only when listing/comparison is the bottleneck. Increase `Buffer Size` last.

See the official [rclone global options documentation](https://rclone.org/docs/) for the precise flag behavior and units.

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
