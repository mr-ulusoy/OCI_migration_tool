# OCI Migrator Pro

Admin tool for OCI migration and cloud data sync.

- React/Vite frontend
- FastAPI backend
- Celery worker with Redis
- rclone sync/copy jobs
- OCI SDK VM and Object Storage operations
- persistent job run history and runtime config export

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

The installer stores a hashed admin password in `~/.oci-migrator.env`. If you do not pass or prompt for a password, the installer generates one and prints it once.

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
- `/etc/systemd/timesyncd.conf.d/oci-migrator.conf`

The backend service also serves the built frontend from `frontend/dist`. Backend dependencies use `backend/requirements.lock` when present.

Rclone job logs are persisted per run and visible/downloadable from Job Dashboard -> Recent Runs. The dashboard also exposes log rotation settings. The installer configures logrotate with `maxsize 10M` and `14` retention days by default.

Server local folders can optionally be shared from the UI with SMB. When a user chooses `Share to Everyone` or `Share to User`, the backend runs the installed root helper to install/configure Samba, expose that folder, and open inbound TCP `445`.

## Documentation

- [Installation](docs/INSTALL.md)
- [Operations](docs/OPERATIONS.md)
- [Development](docs/DEVELOPMENT.md)
- [Recommendations](docs/RECOMMENDATIONS.md)
- [GitHub Actions CI template](docs/ci/github-actions.yml)

## Security Note

This is an admin tool. Run it behind VPN or a private network when possible. If it ever becomes internet-facing, add HTTPS and restrict inbound access.
