<img width="1307" height="607" alt="image" src="https://github.com/user-attachments/assets/8e8432a2-2498-4571-9645-3e028483ccc2" />
<img width="1225" height="571" alt="image" src="https://github.com/user-attachments/assets/53b53340-29fc-49d6-99f6-6fe3706babb3" />
<img width="1442" height="746" alt="image" src="https://github.com/user-attachments/assets/9c451f5a-1558-4654-9ab7-cbd0a2e7d298" />
<img width="1429" height="735" alt="image" src="https://github.com/user-attachments/assets/8fa90421-f0c7-4e70-acf3-8f0b8ec51ffa" />
<img width="1434" height="739" alt="image" src="https://github.com/user-attachments/assets/52be513e-d9b7-40ce-9a60-53cd49469991" />
<img width="1424" height="738" alt="image" src="https://github.com/user-attachments/assets/a25766ce-31f6-4388-b1af-c02e6b8e0383" />
<img width="1438" height="737" alt="image" src="https://github.com/user-attachments/assets/26d2c985-725a-4633-9411-f908f04258c3" />
<img width="1427" height="743" alt="image" src="https://github.com/user-attachments/assets/03c7ab1f-fa90-44c5-9f4e-26c6bb7b7487" />
<img width="1430" height="738" alt="image" src="https://github.com/user-attachments/assets/398a2317-5ee1-4fa3-b74f-3e31f5f7bbd9" />

**VM Image & Data Migration**
<img width="1236" height="632" alt="image" src="https://github.com/user-attachments/assets/ad7f508b-1d1b-43a0-97ac-12d266f7ab6f" />
<img width="1290" height="655" alt="image" src="https://github.com/user-attachments/assets/27d85af2-5eac-41ca-aa02-5594adbe4d77" />

<img width="1440" height="748" alt="image" src="https://github.com/user-attachments/assets/5e861ceb-4c51-4424-b3bd-91865eee983a" />

<img width="1442" height="746" alt="image" src="https://github.com/user-attachments/assets/28521514-eedb-4ffe-bb63-a7d0f3ee3acf" />




# Cloud Migration Console

[![CI](https://github.com/mr-ulusoy/OCI_migration_tool/actions/workflows/ci.yml/badge.svg)](https://github.com/mr-ulusoy/OCI_migration_tool/actions/workflows/ci.yml)

Application updates are distributed as versioned GitHub Releases. Maintainers should use `scripts/prepare-release.sh patch|minor|major` and follow the [release checklist](docs/RELEASING.md); ordinary commits to `main`, including documentation-only changes, are not offered as dashboard updates.

Self-hosted admin console for moving file and object data into Oracle Cloud Infrastructure (OCI) Object Storage, and for migrating OCI VM images between OCI tenants.

Oracle and Oracle Cloud Infrastructure are trademarks of Oracle and/or its affiliates. Cloud Migration Console is independently developed and is not affiliated with, endorsed by, or sponsored by Oracle.

- React/Vite frontend
- FastAPI backend
- Celery worker with Redis
- rclone sync/copy jobs
- OCI SDK VM and Object Storage operations
- persistent job run history and runtime config export

## Recommended Server Size

For a production Cloud Migration Console installation handling large backup and migration jobs, use the following as a starting point:

| Resource | Recommendation |
| --- | --- |
| CPU | **8 vCPU** or **4 OCI OCPU** |
| RAM | **16 GB** |
| System disk | **80-100 GB SSD** |
| OS | Ubuntu Server 24.04 LTS |

## How Data Moves

Cloud Migration Console streams data from cloud providers and mounted external SMB/NFS file shares directly to OCI Object Storage:

```text
Cloud provider or external file share
                  |
                  v
Cloud Migration Console (memory buffers)
                  |
                  v
          OCI Object Storage
```

Complete files are not downloaded to a local staging area or copied into the managed local backup folder. The transfer traffic passes through the Cloud Migration Console server, while only configuration, job history, and logs are stored locally. The server must remain online for the duration of the transfer.

`Server Local Folder` is the intentional exception: those source files already reside on the Cloud Migration Console server before the backup job starts.

## Quick Install

On an Ubuntu server:

```bash
curl -fsSL https://raw.githubusercontent.com/mr-ulusoy/OCI_migration_tool/main/scripts/bootstrap.sh -o bootstrap.sh
chmod +x bootstrap.sh
./bootstrap.sh --prompt-admin-password
```

The bootstrap script installs the latest published GitHub Release. Use `--release vX.Y.Z` to install a specific published version. `--branch main` is a developer override and should not be used for production installations.

The installer automatically detects the server's first local IP address and prints the initial setup URL. Open the address shown when installation completes, normally:

```text
http://<detected-server-ip>:8000
```

`--public-host` is optional. Set it when the browser reaches the server through a DNS name, a public/NAT address that Ubuntu cannot detect, or a different interface on a multi-homed server. Use the exact host that the browser will open during initial setup:

```bash
./bootstrap.sh --public-host migrator.example.com --prompt-admin-password
```

Use the HTTP address only for initial setup. Sign in, open `Settings` -> `HTTPS & Certificates`, and configure Let's Encrypt, a corporate certificate, or external TLS termination before production use. The installer opens the local Ubuntu firewall for TCP `22` (SSH), `80`/`443` (HTTPS), `8000` (setup/app API), `445` (SMB), and `2049` (NFSv4) by default. Your OCI Security List, NSG, or on-premises firewall must allow only the traffic required by the selected deployment.

The installer stores a hashed admin password in `~/.oci-migrator.env`. If you do not pass or prompt for a password, the installer generates one and prints it once.

### Retrieve the admin login

The default admin username is `admin`. To retrieve the generated password from the server, run the following command from your workstation (replace `<server-ip-or-dns>`):

```bash
ssh ubuntu@<server-ip-or-dns> 'cat ~/oci-migrator-admin-password.txt'
```

You can also set it non-interactively:

```bash
./bootstrap.sh --admin-password '<strong-password>'
```

The installer configures server time sync with `systemd-timesyncd`, `Europe/Stockholm`, and the Swedish NTP pool by default. Override it when needed:

```bash
./bootstrap.sh --timezone Europe/Stockholm --ntp-servers "0.se.pool.ntp.org 1.se.pool.ntp.org"
```

## Web Console

- `Job Dashboard` shows service health, software update status, backup totals, running jobs, jobs requiring attention, and the latest successful run.
- `Backup Jobs` contains Active Backup Jobs and Recent Runs, including manual execution, editing, status summaries, and downloadable logs.
- `New Backup Job` creates scheduled or manual copy/sync pipelines.
- `Credentials` manages OCI, cloud, S3-compatible, local folder, SMB, and NFS sources.
- `OCI Object Storage` explores buckets and objects and manages supported bucket settings and lifecycle rules.
- `VM Image Migration` scans OCI compute instances and migrates the boot image plus selected attached data volumes between OCI tenancies.
- `Settings` contains managed HTTPS, a daily published-release check with controlled system upgrades, remote syslog notifications, runtime config backup/import, time and NTP, network, job defaults, local disk warnings, log rotation, admin password controls, and a protected uninstall workflow.

## Main Use Cases

- Move or back up data from AWS S3, Google Cloud Storage, Azure Blob Storage, S3-compatible storage, OCI Object Storage, and on-premises/server-local folders into OCI Object Storage.
- Use the server as a controlled ingest point for local data, with optional SMB and NFSv4 shares for users or systems that need to drop files into managed local folders.
- Create scheduled backup jobs with copy/sync mode, bandwidth and API TPS limits, OCI object metadata, local cleanup after successful jobs, persistent run history, and downloadable job logs.
- Manage OCI Object Storage buckets, folders, object versioning state, auto-tiering state, and bucket lifecycle policy rules.
- Export and import runtime configuration backups for easier rebuilds, upgrades, and repeated installations.
- Migrate OCI VM boot images and selected attached data volumes from one OCI tenant/profile to another. Data volumes support OCI cross-tenancy clone or backup/restore in the same region.

## Supported Data Paths

| Source | Target |
| --- | --- |
| AWS S3 | OCI Object Storage bucket |
| Google Cloud Storage | OCI Object Storage bucket |
| Azure Blob Storage | OCI Object Storage bucket |
| OCI Object Storage | OCI Object Storage bucket |
| S3-compatible object storage | OCI Object Storage bucket |
| Server local folder, SMB/NFS ingest folder, or mounted on-premises share | OCI Object Storage bucket |

For VM image migration, the source is an OCI tenant/profile with compute instances and the destination is another OCI tenant/profile with an Object Storage bucket for the exported boot image. The scan shows attached data volumes and lets the operator choose which disks to migrate using OCI cross-tenancy clone or backup/restore. Created target data volumes are left available and ready to attach; Cloud Migration Console does not yet provision or attach them to a target VM.

## HTTPS Modes

HTTPS is required for production use. Configure it from `Settings` -> `HTTPS & Certificates` after the initial HTTP login:

| Mode | Use case | Local Caddy service |
| --- | --- | --- |
| `Let's Encrypt` | Publicly resolvable DNS name with inbound TCP `80` and `443` | Enabled; certificate issuance and renewal are automatic |
| `Corporate Certificate` | Customer-issued PEM full chain and matching unencrypted private key uploaded in Settings | Enabled; files are validated and copied to protected storage |
| `External TLS` | Existing load balancer, ingress gateway, or reverse proxy | Disabled; the external service owns the certificate |
| `HTTP Setup` | Initial setup or recovery only; requires explicit acknowledgement of unencrypted traffic | Disabled |

Managed TLS uses an isolated service named `migrator-tls.service` with the default service prefix and does not replace an existing `/etc/caddy/Caddyfile`. A failed managed TLS activation restores the previous Caddy files and service state. See [HTTPS & Certificates](docs/OPERATIONS.md#https--certificates) and the [HTTPS installation requirements](docs/INSTALL.md#https-setup).

## VM Image And Data Volume Migration

`VM Image Migration` scans each OCI instance and displays its boot volume and attached Block Volumes. The operator can migrate the boot image alone or keep selected data volumes checked.

- The boot volume is captured as a custom image, exported through the destination Object Storage bucket, and imported as a custom image in the target tenancy.
- `Cross-tenancy Clone` creates each selected target Block Volume directly from its source volume.
- `Backup and Restore` creates a full OCI Block Volume backup and restores a new Block Volume in the selected target availability domain.
- Source and destination profiles must use the same OCI region when data volumes are selected.
- A running source VM is soft-stopped during capture and restarted afterward. A source VM that was already stopped remains stopped.
- Created data volumes are left in `AVAILABLE` state in the target tenancy. Create the target VM from the imported boot image, attach the volumes, and validate Linux mounts or Windows drive assignments separately.

The destination Object Storage bucket is used by the boot-image workflow. Selected data volumes remain OCI Block Volume resources and are not converted into ordinary objects. Cross-tenancy IAM policies must be configured before execution. See [VM Image Migration](docs/OPERATIONS.md#vm-image-migration) for preparation, method selection, and post-migration steps.

## Common Commands

```bash
make doctor
make status
make restart
make logs-api
make logs-worker
make package
```

Health endpoint:

```text
https://<dashboard-dns-name>/health
```

The direct setup/recovery endpoint remains available at `http://<server-ip-or-dns>:8000/health` when network policy permits it.

Monitoring endpoints for same-network pull monitoring:

```text
GET /monitoring/status
GET /metrics
```

Both require an admin session or `X-API-Token`. See the [Server Runbook](docs/RUNBOOK.md#monitoring).

Push monitoring is available through `Settings` -> `Notifications`. Cloud Migration Console can send backup failures, timeouts, recovery, and optionally every completed run to a remote syslog server over UDP or TCP.

## Manual Install

```bash
git clone https://github.com/mr-ulusoy/OCI_migration_tool.git
cd OCI_migration_tool
./install.sh --public-host <server-ip-or-dns> --prompt-admin-password
```

## Deploy From A Workstation

Deploy requires an SSH host and key:

```bash
SSH_HOST=ubuntu@1.2.3.4 \
SSH_KEY=/path/to/key \
PUBLIC_HOST=migrator.example.com \
./scripts/deploy.sh
```

## Multiple Installations

Use a unique install directory, service prefix, app/API port, and env file.
The built frontend is served by the backend, so each install only needs one app/API port.

```bash
./scripts/bootstrap.sh \
  --install-dir /opt/oci-migrator-dev \
  --public-host dev.example.com \
  -- \
  --service-prefix migrator-dev \
  --api-port 8100 \
  --env-file ~/.oci-migrator-dev.env
```

## What Gets Installed

- `migrator-api.service`
- `migrator-worker.service`
- `migrator-scheduler.timer`
- `migrator-tls.service` for managed HTTPS
- `~/.oci-migrator.env`
- Caddy for managed Let's Encrypt or corporate certificates
- `/usr/local/sbin/oci-migrator-tls`
- `/etc/oci-migrator/tls.conf`
- `/etc/oci-migrator/Caddyfile`
- `/var/lib/oci-migrator/tls/`
- `/usr/local/sbin/oci-migrator-local-share`
- `venv/`
- `frontend/dist/`
- `/var/lib/oci-migrator/local/`
- `/var/log/oci-migrator/jobs/`
- `/etc/logrotate.d/migrator-job-logs`
- `/usr/local/sbin/oci-migrator-job-log`
- `/usr/local/sbin/oci-migrator-time-sync`
- `/usr/local/sbin/oci-migrator-network`
- `/usr/local/sbin/oci-migrator-uninstall`
- `/etc/oci-migrator/uninstall.conf`
- `/etc/systemd/timesyncd.conf.d/oci-migrator.conf`

The backend service also serves the built frontend from `frontend/dist`. Backend dependencies use `backend/requirements.lock` when present.

Rclone job logs are persisted per run and visible/downloadable from `Backup Jobs` -> `Recent Runs`. Log rotation is configured under `Settings` -> `Job Log Rotation`. The installer configures logrotate with `maxsize 10M` and `14` retention days by default.

Timezone and NTP servers are visible and editable from Settings -> Time & NTP.

IPv4 networking is editable from Settings -> Network. DHCP is the default. Static mode requires an interface, IPv4 address/prefix, gateway, and DNS servers. Changes use a three-minute confirmation window; unconfirmed settings are rolled back automatically. On cloud platforms, assign or reserve the static address on the instance VNIC before applying it inside Ubuntu.

Per-job local cleanup can be enabled for managed server local folder sources. Cleanup runs only after a successful backup, deletes files older than the configured retention, and always skips files modified within the configured safety window.

Settings -> Local Disk Usage shows the managed local data disk usage and configurable warning/critical thresholds. The same status is exposed through `/health`, `/monitoring/status`, and `/metrics`.

Settings -> Uninstall Cloud Migration Console schedules `uninstall.sh --purge-project` after verifying the current admin password and the exact confirmation text `UNINSTALL`. Runtime configuration, OCI/rclone credentials, OCI Object Storage data, and external mounted shares are preserved. An additional checkbox can permanently remove only the configured server-local backup directory.

Server local folders can optionally be shared from the UI with SMB, NFSv4, or both. SMB can be opened as guest access or a named Samba user on TCP `445`. NFSv4 requires an allowed client IP/hostname/CIDR list and uses TCP `2049`. No share is enabled during installation; the installed root helper applies the share only when a user chooses it in `Add Remote`.

## Documentation

- [Installation](docs/INSTALL.md)
- [Dashboard Configuration](docs/OPERATIONS.md)
- [Server Runbook](docs/RUNBOOK.md)
- [Development](docs/DEVELOPMENT.md)
- [Production Readiness](docs/RECOMMENDATIONS.md)
- [Continuous Integration](.github/workflows/ci.yml)

## Feedback and Support

Use the structured [GitHub Issue forms](https://github.com/mr-ulusoy/OCI_migration_tool/issues/new/choose) to report reproducible bugs or propose features. Review [SUPPORT.md](SUPPORT.md) before submitting. Issues and attachments are public and must not contain credentials, customer data, runtime configuration archives, or sensitive infrastructure information.

Report suspected vulnerabilities privately according to [SECURITY.md](SECURITY.md). Do not disclose security problems in a public issue.

## License

Cloud Migration Console is source-available proprietary software owned by Cengiz Ulusoy (`mr-ulusoy`).
You may download, install, and run unmodified copies, but you may not modify, redistribute, publish, sublicense, sell, or provide modified versions without prior written permission.

Attribution is required: `Cloud Migration Console by Cengiz Ulusoy (mr-ulusoy)`.
See [LICENSE](LICENSE) for the full terms.

## Security Note

This is an admin tool. HTTPS is required for production browser and API access. Run it behind a VPN or private management network and restrict the setup/API port to approved management networks.
