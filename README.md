# OCI Migrator Pro
<img width="1573" height="822" alt="image" src="https://github.com/user-attachments/assets/b8897377-92b8-4a9e-8858-e1f7995cb0c3" />


Self-hosted admin console for moving file and object data into Oracle Cloud Infrastructure (OCI) Object Storage, and for migrating OCI VM images between OCI tenants.

- React/Vite frontend
- FastAPI backend
- Celery worker with Redis
- rclone sync/copy jobs
- OCI SDK VM and Object Storage operations
- persistent job run history and runtime config export

## Main Use Cases

- Move or back up data from AWS S3, Google Cloud Storage, Azure Blob Storage, S3-compatible storage, OCI Object Storage, and on-premises/server-local folders into OCI Object Storage.
- Use the server as a controlled ingest point for local data, with optional SMB and NFSv4 shares for users or systems that need to drop files into managed local folders.
- Create scheduled backup jobs with copy/sync mode, bandwidth and API TPS limits, OCI object metadata, local cleanup after successful jobs, persistent run history, and downloadable job logs.
- Manage OCI Object Storage buckets, folders, object versioning state, auto-tiering state, and bucket lifecycle policy rules.
- Export and import runtime configuration backups for easier rebuilds, upgrades, and repeated installations.
- Migrate OCI VM images from one OCI tenant/profile to another tenant/profile. VM image migration is separate from file backup jobs and can stop selected source VMs before creating and moving images.

## Supported Data Paths

| Source | Target |
| --- | --- |
| AWS S3 | OCI Object Storage bucket |
| Google Cloud Storage | OCI Object Storage bucket |
| Azure Blob Storage | OCI Object Storage bucket |
| OCI Object Storage | OCI Object Storage bucket |
| S3-compatible object storage | OCI Object Storage bucket |
| Server local folder, SMB/NFS ingest folder, or mounted on-premises share | OCI Object Storage bucket |

For VM image migration, the source is an OCI tenant/profile with compute instances and the destination is another OCI tenant/profile with an Object Storage bucket for the exported image workflow.

## Quick Install

On an Ubuntu server:

```bash
curl -fsSL https://raw.githubusercontent.com/mr-ulusoy/OCI_migration_tool/main/scripts/bootstrap.sh -o bootstrap.sh
chmod +x bootstrap.sh
./bootstrap.sh --public-host <server-ip-or-dns> --prompt-admin-password
```

Open:

```text
http://<server-ip-or-dns>:8000
```

The installer opens the local Ubuntu firewall for TCP `22` (SSH), `8000` (app/API), `445` (SMB), and `2049` (NFSv4) by default. Your OCI Security List or NSG must allow the same traffic from the networks that should reach the server.

The installer stores a hashed admin password in `~/.oci-migrator.env`. If you do not pass or prompt for a password, the installer generates one and prints it once.

### Retrieve the admin login

The default admin username is `admin`. To retrieve the generated password from the server, run the following command from your workstation (replace `<server-ip-or-dns>`):

```bash
ssh ubuntu@<server-ip-or-dns> 'cat ~/oci-migrator-admin-password.txt'
```

You can also set it non-interactively:

```bash
./bootstrap.sh --public-host <server-ip-or-dns> --admin-password '<strong-password>'
```

The installer configures server time sync with `systemd-timesyncd`, `Europe/Stockholm`, and the Swedish NTP pool by default. Override it when needed:

```bash
./bootstrap.sh --public-host <server-ip-or-dns> --timezone Europe/Stockholm --ntp-servers "0.se.pool.ntp.org 1.se.pool.ntp.org"
```

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
http://<server-ip-or-dns>:8000/health
```

Monitoring endpoints for same-network pull monitoring:

```text
GET /monitoring/status
GET /metrics
```

Both require an admin session or `X-API-Token`. See [Operations](docs/OPERATIONS.md#monitoring).

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
- `~/.oci-migrator.env`
- `/usr/local/sbin/oci-migrator-local-share`
- `venv/`
- `frontend/dist/`
- `/var/lib/oci-migrator/local/`
- `/var/log/oci-migrator/jobs/`
- `/etc/logrotate.d/migrator-job-logs`
- `/usr/local/sbin/oci-migrator-job-log`
- `/usr/local/sbin/oci-migrator-time-sync`
- `/etc/systemd/timesyncd.conf.d/oci-migrator.conf`

The backend service also serves the built frontend from `frontend/dist`. Backend dependencies use `backend/requirements.lock` when present.

Rclone job logs are persisted per run and visible/downloadable from Job Dashboard -> Recent Runs. The dashboard also exposes log rotation settings. The installer configures logrotate with `maxsize 10M` and `14` retention days by default.

Timezone and NTP servers are visible and editable from Settings -> Time & NTP.

Per-job local cleanup can be enabled for managed server local folder sources. Cleanup runs only after a successful backup, deletes files older than the configured retention, and always skips files modified within the configured safety window.

Settings -> Local Disk Usage shows the managed local data disk usage and configurable warning/critical thresholds. The same status is exposed through `/health`, `/monitoring/status`, and `/metrics`.

Server local folders can optionally be shared from the UI with SMB, NFSv4, or both. SMB can be opened as guest access or a named Samba user on TCP `445`. NFSv4 requires an allowed client IP/hostname/CIDR list and uses TCP `2049`. No share is enabled during installation; the installed root helper applies the share only when a user chooses it in `Add Remote`.

## Documentation

- [Installation](docs/INSTALL.md)
- [Operations](docs/OPERATIONS.md)
- [Test Plan](docs/TEST_PLAN.md)
- [Development](docs/DEVELOPMENT.md)
- [Recommendations](docs/RECOMMENDATIONS.md)
- [GitHub Actions CI template](docs/ci/github-actions.yml)

## License

OCI Migrator Pro is source-available proprietary software owned by Mr. Ulusoy (`mr-ulusoy`).
You may download, install, and run unmodified copies, but you may not modify, redistribute, publish, sublicense, sell, or provide modified versions without prior written permission.

Attribution is required: `OCI Migrator Pro by Mr. Ulusoy (mr-ulusoy)`.
See [LICENSE](LICENSE) for the full terms.

## Security Note

This is an admin tool. Run it behind VPN or a private network when possible. If it ever becomes internet-facing, add HTTPS and restrict inbound access.
