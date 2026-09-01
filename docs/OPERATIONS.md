# Dashboard Configuration

This guide documents the configuration available in the OCI Migrator Pro web dashboard. For service commands, health endpoints, monitoring integrations, runtime files, and command-line recovery procedures, see the [Server Runbook](RUNBOOK.md).

## Quick Install

On an Ubuntu server:

```bash
curl -fsSL https://raw.githubusercontent.com/mr-ulusoy/OCI_migration_tool/main/scripts/bootstrap.sh -o bootstrap.sh
chmod +x bootstrap.sh
./bootstrap.sh --prompt-admin-password
```

The installer automatically detects the server's first local IP address and prints the initial setup URL. Open the address shown when installation completes, normally:

```text
http://<detected-server-ip>:8000
```

`--public-host` is optional. Set it when the browser uses a DNS name, a public/NAT address that Ubuntu cannot detect, or a different interface on a multi-homed server:

```bash
./bootstrap.sh --public-host migrator.example.com --prompt-admin-password
```

Use HTTP only for initial setup. Configure a production HTTPS mode under `Settings` -> `HTTPS & Certificates`. See the [Installation Guide](INSTALL.md) for firewall, DNS, certificate, and advanced installation options.

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
- `Region`: the OCI **Region Identifier**, for example `eu-stockholm-1`. Find the required value in Oracle's [Regions and Availability Domains](https://docs.oracle.com/en-us/iaas/Content/General/Concepts/regions.htm) table and use the value shown under **Region Identifier**.
- `API Key`: upload or paste the private API signing key associated with the IAM user.

#### How to collect the OCI profile values

The quickest method is to create or inspect an API signing key in the OCI Console. OCI then displays a `Configuration File Preview` containing the user OCID, fingerprint, tenancy OCID, and region together.

1. Sign in to the [OCI Console](https://cloud.oracle.com/) and select the region that contains the resources you want to use. The current region appears in the Region menu at the top of the Console. Open Oracle's [Regions and Availability Domains](https://docs.oracle.com/en-us/iaas/Content/General/Concepts/regions.htm) list and copy the matching **Region Identifier** into OCI Migrator, not the region name or region key.
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
- `OCI Retention Rules (WORM)`: status only. Create and manage immutable retention rules in the OCI Console. While a time-bound rule protects an object, its data and metadata cannot be updated, overwritten, or deleted until that object's retention period expires; OCI calculates the period individually from the object's `Last Modified` timestamp. An indefinite rule protects objects until the rule is removed. Locking a retention rule is irreversible: the duration can only be increased, and neither a tenancy administrator nor Oracle Support can unlock or delete the rule separately. Because an accidental rule or lock can make customer data undeletable for the configured period, OCI Migrator deliberately does not create, change, or lock WORM rules. See [OCI retention rule documentation](https://docs.oracle.com/en-us/iaas/Content/Object/Tasks/usingretentionrules.htm).

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

VM image migration supports the boot volume plus individually selected attached OCI Block Volumes. It is an OCI-to-OCI workflow; it does not migrate VMs from AWS, Azure, or Google Cloud.

### Before starting

1. Create separate OCI profiles for the source and target tenancies under `Credentials`.
2. Confirm that both profiles use the same OCI region when data volumes will be migrated.
3. Create or select an Object Storage bucket in the target profile for the boot-image export.
4. Configure the required cross-tenancy IAM policies for Block Volume clone or backup/restore.
5. Confirm that the source VM can be stopped safely and that application services can tolerate a controlled shutdown.

Open `VM Image Migration` and configure:

- `Source`: OCI profile used to scan compute instances.
- `VM`: selected instance from the scan results. Selecting a VM selects all attached data volumes by default.
- `Data Volumes`: individually select or clear attached disks. The scan shows the volume name, size, device, attachment type, and availability domain.
- `Destination Profile`: OCI profile that receives the exported image workflow.
- `Storage Bucket`: destination Object Storage bucket.
- `Data Volume Method`: `Cross-tenancy Clone` creates a target volume directly from the source volume; `Backup and Restore` first creates a full source volume backup and restores it in the target tenancy.
- `Target Availability Domain`: required for backup/restore. OCI infers the matching physical availability domain for cross-tenancy clone.

Selecting a VM selects all discovered attached data volumes by default. Clear any volume that should not be copied. A maximum of 32 data volumes can be selected for one VM migration request.

### Choosing a data-volume method

| Method | What OCI Migrator requests | When to use it |
| --- | --- | --- |
| `Cross-tenancy Clone` | Creates a target Block Volume directly from the source volume OCID | Preferred when the source and target tenancy policies allow cross-tenancy volume cloning and the matching availability domain is available |
| `Backup and Restore` | Creates a full source Block Volume backup, waits for it to become available, and restores a target Block Volume from that backup | Use when the backup-based workflow is required or when the target volume must be restored into an explicitly selected availability domain |

The destination Object Storage bucket is used only for the boot-image export/import bridge. Data-volume clone and backup/restore remain OCI Block Volume operations; data disks are not downloaded to the OCI Migrator server and are not uploaded as ordinary Object Storage files.

The source and destination profiles must use the same OCI region for data-volume migration. OCI cross-tenancy policies must be configured before execution. The target IAM group needs local permission to manage volumes in the destination compartment, an `Endorse` policy in the target tenancy, and matching `Admit` policies in the source tenancy. The source profile also needs local permission to create a volume backup when `Backup and Restore` is selected. See [OCI cross-tenancy volume migration](https://docs.oracle.com/en/solutions/migrate-data-across-tenancies/volume-data-migration-process1.html).

Example policy structure, with names and OCIDs replaced for the environment:

```text
# Source tenancy: clone access
Define tenancy TargetTenancy as <target-tenancy-ocid>
Define group TargetVolumeAdmins as <target-group-ocid>
Admit group TargetVolumeAdmins of tenancy TargetTenancy to use volumes in compartment <source-compartment-name> where ANY { request.operation='CreateVolume', request.operation='GetVolume' }

# Source tenancy: restore access
Admit group TargetVolumeAdmins of tenancy TargetTenancy to read volume-backups in compartment <source-compartment-name>
Admit group TargetVolumeAdmins of tenancy TargetTenancy to inspect volumes in compartment <source-compartment-name>

# Target tenancy: clone access
Define tenancy SourceTenancy as <source-tenancy-ocid>
Endorse group <target-group-name> to use volumes in tenancy SourceTenancy where ANY { request.operation='CreateVolume', request.operation='GetVolume' }

# Target tenancy: restore access and local target access
Endorse group <target-group-name> to read volume-backups in tenancy SourceTenancy
Endorse group <target-group-name> to inspect volumes in tenancy SourceTenancy
Allow group <target-group-name> to manage volume-family in compartment <target-compartment-name>
```

### Execution and results

During execution, a running source VM is soft-stopped while the boot image and selected data-volume copies are requested. It is restarted after capture. A VM that was already stopped remains stopped.

The worker then:

1. Creates a source custom image from the boot volume.
2. Starts a cross-tenancy clone or full backup for every selected data volume.
3. Restarts the source VM when its original state was `RUNNING`.
4. Exports the boot image through a 48-hour pre-authenticated request into the selected target Object Storage bucket.
5. Imports the exported object as a custom image in the target tenancy.
6. Waits for every target data volume to reach `AVAILABLE`.

The migration run history records the target image OCID, target volume OCIDs, and intermediate backup OCIDs when backup/restore is used. A failed migration attempts to restart a source VM that OCI Migrator stopped.

### After migration

OCI Migrator does not currently provision the target VM or attach the created data volumes. After creating the target VM from the imported image, attach each available target volume in the OCI Console or through OCI automation and preserve the intended device mapping. Validate application mounts and Windows drive assignments before placing the migrated VM in service.

Also verify:

- filesystem consistency and application data on every attached volume
- Linux `/etc/fstab`, filesystem UUIDs, LVM, and mount points
- Windows disk state, drive letters, mount points, and application service dependencies
- target VM networking, security rules, boot behavior, and application startup
- whether temporary source images, exported `.oci` objects, pre-authenticated requests, or intermediate volume backups should be retained or removed

## Settings

### System Upgrade

`Check` compares the installed commit with the latest GitHub commit. `Upgrade` installs the latest version, and `Log` shows progress while the API and frontend restart. Runtime configuration is preserved by the managed upgrade process.

### HTTPS & Certificates

HTTPS is required for production use. The direct HTTP endpoint is retained for initial setup and recovery. Select one mode:

- `Let's Encrypt`: Caddy obtains and automatically renews a public certificate. The DNS hostname must resolve to the server and inbound TCP `80` and `443` must be allowed.
- `Corporate Certificate`: upload a customer-issued PEM full chain and matching unencrypted PEM private key. The files are validated and copied into protected storage.
- `External TLS`: an existing load balancer or reverse proxy owns the certificate and forwards requests to OCI Migrator with `X-Forwarded-Proto: https`.
- `HTTP Setup`: recovery and initial configuration only. Credentials and session tokens are not encrypted in transit.

#### Mode requirements

| Mode | Required preparation | Certificate lifecycle |
| --- | --- | --- |
| `Let's Encrypt` | DNS A/AAAA record points to the server; inbound TCP `80` and `443` reach Caddy | Caddy obtains and renews the certificate automatically |
| `Corporate Certificate` | PEM full chain and matching unencrypted PEM private key are ready to upload; clients trust the issuing CA | Upload the replacement files and reapply the setting when the certificate is renewed |
| `External TLS` | External proxy forwards to the app/API port and sends `X-Forwarded-Proto: https` | Managed entirely by the external platform |
| `HTTP Setup` | Access is restricted to a trusted setup or recovery network; the administrator acknowledges the unencrypted-traffic warning | No certificate |

For managed modes, OCI Migrator validates the generated Caddy configuration and checks the HTTPS health endpoint before accepting the change. If startup or the health check fails, the previous managed Caddy files and service state are restored. With the default service prefix, Caddy runs as the dedicated `migrator-tls.service`, proxies to `127.0.0.1:8000`, and keeps its state below `/var/lib/oci-migrator/tls`.

When `HTTP Setup` is deliberately retained, select the acknowledgement checkbox and apply the setting. The acknowledgement is stored in the server runtime configuration so the health overview does not repeatedly warn about an accepted deployment decision. The dashboard still labels the connection as HTTP and does not treat it as encrypted.

Corporate certificate and private-key uploads are limited to 2 MiB each. They are written to private temporary files, validated by the managed TLS helper, copied into protected Caddy storage, and removed from temporary storage immediately afterward.

`Open HTTPS` opens the configured dashboard hostname. The status line shows whether the managed Caddy service is active. Upload renewed files and reapply `Corporate Certificate` after certificate replacement. Runtime Config Backup does not export the managed private-key copy, so configure TLS separately on a replacement server. See the [Installation Guide](INSTALL.md#https-setup) for DNS, firewall, and certificate preparation.

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

TLS mode, dashboard hostname, certificate source, and allowed browser origins are server-specific and remain unchanged during import. Managed Caddy certificate/private-key copies are not included in the portable ZIP; configure HTTPS separately on a replacement server.

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
