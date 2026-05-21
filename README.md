# OCI Migrator Pro

Full-stack admin tool for OCI migration and data sync:

- React/Vite frontend
- FastAPI backend
- Celery worker with Redis
- rclone-based sync/copy jobs
- OCI SDK-based VM and Object Storage operations

## Requirements

- Ubuntu 20.04, 22.04, or 24.04
- sudo access on the target server
- OCI API credentials with the required tenancy/compartment permissions
- Network access to install apt, Node.js, Python, npm, and rclone dependencies

## Local Project Copy

This copy was pulled from the server with `venv/` and `frontend/node_modules/` excluded. Those are recreated by the installer.

## Install On A Server

From the project directory on the target server:

```bash
chmod +x install.sh
PUBLIC_HOST=<server-ip-or-dns> ./install.sh
```

The installer is idempotent. It can be rerun after code changes and will keep the existing `~/.oci-migrator.env` file.
Backend dependencies are installed from `backend/requirements.lock` when present, using the versions captured from the current server.

Useful installer options:

```bash
PUBLIC_HOST=207.127.90.146 ./install.sh
API_PORT=8001 FRONTEND_PORT=5174 ./install.sh
OPEN_FIREWALL=1 ./install.sh
STOP_LEGACY_PROCESSES=1 ./install.sh
INSTALL_FRONTEND_SERVICE=0 ./install.sh
```

What it creates:

- `migrator-api.service`
- `migrator-worker.service`
- `migrator-scheduler.timer`
- `migrator-frontend.service`
- `~/.oci-migrator.env`
- `venv/`
- `frontend/dist/`

## Deploy From This Machine

The deploy script defaults to the current server and SSH key:

```bash
chmod +x scripts/deploy.sh
./scripts/deploy.sh
```

Defaults:

```bash
SSH_HOST=ubuntu@207.127.90.146
SSH_KEY=/Users/mr-ulusoy/Documents/ssh1/cloudssh
REMOTE_DIR=/home/ubuntu/oci-migrator
PUBLIC_HOST=207.127.90.146
```

Override when needed:

```bash
SSH_HOST=ubuntu@1.2.3.4 PUBLIC_HOST=migrator.example.com ./scripts/deploy.sh
```

If the old server has a manual `uvicorn` process occupying port `8000`, run:

```bash
STOP_LEGACY_PROCESSES=1 ./scripts/deploy.sh
```

## API Token

The backend requires `X-API-Token`. The installer creates or preserves:

```bash
~/.oci-migrator.env
```

To use the frontend without baking the token into the static build, open the browser console and set:

```js
localStorage.setItem('OCI_MIGRATOR_API_TOKEN', '<token-from-env-file>');
```

## Operations

```bash
sudo systemctl status migrator-api migrator-worker migrator-frontend migrator-scheduler.timer
journalctl -u migrator-api -f
journalctl -u migrator-worker -f
journalctl -u migrator-frontend -f
```

Frontend:

```text
http://<server-ip-or-dns>:5173
```

Backend:

```text
http://<server-ip-or-dns>:8000
```

## Security Note

This is an admin tool. Run it behind VPN, a private network, or a reverse proxy with authentication. Do not expose port `8000` publicly without additional protection.
