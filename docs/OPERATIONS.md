# Operations

## Service Commands

```bash
make status
make restart
make logs-api
make logs-worker
make logs-frontend
```

Equivalent direct commands:

```bash
sudo systemctl status migrator-api migrator-worker migrator-frontend migrator-scheduler.timer
journalctl -u migrator-api -f
journalctl -u migrator-worker -f
journalctl -u migrator-frontend -f
```

## Health Check

```bash
make doctor
```

The doctor checks:

- system dependencies
- runtime env file
- systemd services
- listening ports
- authenticated backend response

## Runtime Files

```text
~/.oci-migrator.env
~/.oci/config
~/.oci/jobs.json
~/.config/rclone/rclone.conf
/tmp/rclone_<job>.log
```

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
