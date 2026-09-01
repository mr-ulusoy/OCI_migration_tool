# Server Runbook

This runbook covers server operation, health checks, monitoring integration, runtime files, upgrades, and recovery commands. Dashboard fields are documented separately in [Dashboard Configuration](OPERATIONS.md).

## Service Commands

```bash
make status
make restart
make logs-api
make logs-worker
make doctor
```

Equivalent direct commands:

```bash
sudo systemctl status migrator-api migrator-worker migrator-scheduler.timer
sudo systemctl restart migrator-api migrator-worker migrator-scheduler.timer
journalctl -u migrator-api -f
journalctl -u migrator-worker -f
```

## Health Check

```bash
curl http://127.0.0.1:8000/health
```

The public health response reports overall status and checks for the admin hash, runtime files, OCI/rclone configuration, frontend build, local disk usage, HTTPS, upgrade/uninstall helpers, timezone, NTP, and Redis.

`make doctor` additionally checks system dependencies, systemd services, listening ports, the public health endpoint, and an authenticated backend response.

## Monitoring

Monitoring systems on the same network can pull JSON status or Prometheus metrics.

Open health check:

```bash
curl https://oci-migrator.example.internal/health
```

Authenticated JSON status:

```bash
curl -H "X-API-Token: <token>" https://oci-migrator.example.internal/monitoring/status
```

The JSON response includes service state, Redis/rclone/NTP checks, disk utilization, active backup job counts, latest success/failure timestamps, failed and running jobs, and jobs that have never run.

Prometheus metrics:

```bash
curl -H "X-API-Token: <token>" https://oci-migrator.example.internal/metrics
```

Example scrape configuration:

```yaml
scrape_configs:
  - job_name: oci-migrator
    metrics_path: /metrics
    scheme: https
    static_configs:
      - targets: ["oci-migrator.example.internal:443"]
    authorization:
      credentials: "<token>"
```

Representative metrics:

```text
oci_migrator_component_ok{component="redis"} 1
oci_migrator_backup_jobs 8
oci_migrator_backup_jobs_failed 1
oci_migrator_local_disk_used_percent 62.4
oci_migrator_local_disk_free_bytes 41234567890
oci_migrator_backup_job_last_run_timestamp{job="CustomerA",status="success"} 1779473400
```

Push notifications are configured under `Settings` -> `Notifications`. They send backup events to remote syslog over UDP or TCP. Syslog delivery is outbound and best effort; a delivery failure does not change the backup result.

## Job History and Logs

Run history is persisted in:

```text
~/.oci/job_history.json
```

Authenticated API access:

```bash
curl -H "X-API-Token: <token>" http://127.0.0.1:8000/job-history
```

Each run writes a JSON rclone log under:

```text
/var/log/oci-migrator/jobs/
```

The backend stores a compact transfer summary in job history, including transferred bytes, files, deletes, errors, elapsed time, and average speed. Full logs can also be viewed or downloaded from `Backup Jobs` -> `Recent Runs`.

The managed logrotate configuration is:

```text
/etc/logrotate.d/migrator-job-logs
```

The default policy rotates daily, keeps 14 rotated logs, compresses older logs, and uses `maxsize 10M`. Configure retention and maximum size from the dashboard.

## Runtime Files

```text
~/.oci-migrator.env
~/.oci/config
~/.oci/jobs.json
~/.oci/job_history.json
~/.config/rclone/rclone.conf
/var/lib/oci-migrator/local
/var/log/oci-migrator/jobs
/etc/logrotate.d/migrator-job-logs
/usr/local/sbin/oci-migrator-job-log
/usr/local/sbin/oci-migrator-local-share
/usr/local/sbin/oci-migrator-time-sync
/usr/local/sbin/oci-migrator-network
/usr/local/sbin/oci-migrator-upgrade
/usr/local/sbin/oci-migrator-uninstall
/etc/oci-migrator/local-share.conf
/etc/oci-migrator/uninstall.conf
/etc/exports.d/oci-migrator.exports
/etc/systemd/timesyncd.conf.d/oci-migrator.conf
```

Use the dashboard Runtime Config Backup export before server replacement or invasive maintenance. The exported archive contains secrets and must be protected.

## Time and NTP Recovery

The dashboard is the normal configuration path. If it is unavailable, rerun the installer with the required values:

```bash
cd /opt/oci-migrator
./install.sh --timezone Europe/Stockholm --ntp-servers "0.se.pool.ntp.org 1.se.pool.ntp.org"
```

## Admin Password Recovery

The admin password is stored as a hash. Reset it from the server when dashboard login is unavailable:

```bash
cd /opt/oci-migrator
./install.sh --admin-password '<new-strong-password>'
```

## Upgrade

The managed dashboard upgrade is the normal path. Update availability is cached in `/var/lib/oci-migrator/upgrade/check.json` for 24 hours; the dashboard `Check` command forces an immediate refresh. Structured progress is stored in `/var/lib/oci-migrator/upgrade/status.json`, while the complete troubleshooting output remains in `/var/log/oci-migrator/upgrade.log`.

For manual recovery:

```bash
cd /opt/oci-migrator
git pull --ff-only
./install.sh --public-host <server-ip-or-dns>
```

For deployment from a workstation:

```bash
./scripts/deploy.sh
```

## Uninstall

Remove services while keeping runtime data:

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

Add `--purge-local-data` only when the configured managed local backup directory must also be permanently deleted. The script rejects unsafe paths and nested mounted filesystems. Mounted external shares and OCI Object Storage data are outside that deletion target.
