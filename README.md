# OCI Migrator Pro

Admin tool for OCI migration and cloud data sync.

- React/Vite frontend
- FastAPI backend
- Celery worker with Redis
- rclone sync/copy jobs
- OCI SDK VM and Object Storage operations

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

## Common Commands

```bash
make doctor
make status
make restart
make logs-api
make logs-worker
make package
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
- `venv/`
- `frontend/dist/`

The backend service also serves the built frontend from `frontend/dist`. Backend dependencies use `backend/requirements.lock` when present.

## Documentation

- [Installation](docs/INSTALL.md)
- [Operations](docs/OPERATIONS.md)
- [Development](docs/DEVELOPMENT.md)
- [Recommendations](docs/RECOMMENDATIONS.md)
- [GitHub Actions CI template](docs/ci/github-actions.yml)

## Security Note

This is an admin tool. Run it behind VPN or a private network when possible. If it ever becomes internet-facing, add HTTPS and restrict inbound access.
