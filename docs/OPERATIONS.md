# Operations

## Service Commands

```bash
make status
make restart
make logs-api
make logs-worker
```

Equivalent direct commands:

```bash
sudo systemctl status migrator-api migrator-worker migrator-scheduler.timer
journalctl -u migrator-api -f
journalctl -u migrator-worker -f
```

## Health Check

```bash
make doctor
curl http://127.0.0.1:8000/health
```

The doctor checks:

- system dependencies
- runtime env file
- systemd services
- listening ports
- public `/health` endpoint
- authenticated backend response

## Job History

The UI shows recent runs in the Job Dashboard. The backend persists run history in:

```text
~/.oci/job_history.json
```

Authenticated API access:

```bash
curl -H "X-API-Token: <token>" http://127.0.0.1:8000/job-history
```

## Runtime Config Export

After logging in, use the download button in the top bar to export a zip backup. It includes the runtime env file, OCI config, job definitions/history, rclone config, and referenced key files when present.

The archive can contain secrets. Store it securely.

## Local Sources

Local remotes have two modes:

- Server local folders are created under `/var/lib/oci-migrator/local`.
- Mounted external shares must already exist, for example under `/mnt/customer-share`.

The installer creates `/var/lib/oci-migrator/local` for the service user. Use `--local-data-root PATH` to choose another managed root.

## Runtime Files

```text
~/.oci-migrator.env
~/.oci/config
~/.oci/jobs.json
~/.oci/job_history.json
~/.config/rclone/rclone.conf
/var/lib/oci-migrator/local
/tmp/rclone_<job>.log
```

## Admin Password

The admin password is stored as a hash in `~/.oci-migrator.env`.

Reset it from the server:

```bash
cd /opt/oci-migrator
./install.sh --admin-password '<new-strong-password>'
```

Or change it from the UI after logging in.

## Upgrade

If installed with bootstrap:

```bash
cd /opt/oci-migrator
git pull --ff-only
./install.sh --public-host <server-ip-or-dns>
```

If deploying from a workstation:

```bash
./scripts/deploy.sh
```

## Uninstall

Remove services but keep runtime data:

```bash
./scripts/uninstall.sh
```

Remove services and runtime data for the current user:

```bash
./scripts/uninstall.sh --purge-data
```

Remove services and the project directory:

```bash
./scripts/uninstall.sh --purge-project
```
