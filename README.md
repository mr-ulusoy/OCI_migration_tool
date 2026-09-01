<img width="953" height="486" alt="image" src="https://github.com/user-attachments/assets/e90732cf-9922-4d6a-bc62-3acb61c8abf6" />
<img width="1381" height="640" alt="image" src="https://github.com/user-attachments/assets/f4d5a841-d6fe-4e0e-8b8c-c032d3f2eb00" />
<img width="1411" height="750" alt="image" src="https://github.com/user-attachments/assets/74ee6182-cf89-47f1-ae44-909923f57ad3" />
<img width="1423" height="757" alt="image" src="https://github.com/user-attachments/assets/f62a48d0-4a57-4c41-981e-89f82b200bba" />
<img width="1442" height="733" alt="image" src="https://github.com/user-attachments/assets/46235a12-ccac-4e67-b6a3-2a0b64c6d725" />
<img width="1447" height="665" alt="image" src="https://github.com/user-attachments/assets/99e6977f-0519-4d7e-9909-0adb5856314e" />
<img width="1440" height="702" alt="image" src="https://github.com/user-attachments/assets/0dd762a6-fd7b-40db-8f38-8f3a4dd2a0d4" />





# Cloud Migration Console

[![CI](https://github.com/mr-ulusoy/OCI_migration_tool/actions/workflows/ci.yml/badge.svg)](https://github.com/mr-ulusoy/OCI_migration_tool/actions/workflows/ci.yml)

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
- `Settings` contains managed HTTPS, a daily GitHub update check with controlled system upgrades, remote syslog notifications, runtime config backup/import, time and NTP, network, job defaults, local disk warnings, log rotation, admin password controls, and a protected uninstall workflow.

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
