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

The `/health` response also reports server timezone and NTP synchronization. The default install sets `Europe/Stockholm` and writes the NTP pool to:

```text
/etc/systemd/timesyncd.conf.d/oci-migrator.conf
```

Change it by rerunning:

```bash
./install.sh --timezone Europe/Stockholm --ntp-servers "0.se.pool.ntp.org 1.se.pool.ntp.org"
```

It can also be changed from `Settings` -> `Time & NTP`; the UI uses the installed `/usr/local/sbin/oci-migrator-time-sync` helper to update `systemd-timesyncd`.

The doctor checks:

- system dependencies
- runtime env file
- systemd services
- listening ports
- public `/health` endpoint
- authenticated backend response
- server timezone and NTP synchronization

## Monitoring

Monitoring is pull-based when the monitoring system can reach the OCI Migrator server on the same network.

Open unauthenticated health check:

```bash
curl http://127.0.0.1:8000/health
```

Authenticated JSON status for generic monitoring tools:

```bash
curl -H "X-API-Token: <token>" http://127.0.0.1:8000/monitoring/status
```

The JSON status includes component status, Redis/rclone/NTP checks, active backup job counts, latest success/failure timestamps, failed jobs, running jobs, and jobs that have never run.

Prometheus metrics endpoint:

```bash
curl -H "X-API-Token: <token>" http://127.0.0.1:8000/metrics
```

Example Prometheus scrape config:

```yaml
scrape_configs:
  - job_name: oci-migrator
    metrics_path: /metrics
    scheme: http
    static_configs:
      - targets: ["oci-migrator.example.internal:8000"]
    authorization:
      credentials: "<token>"
```

The metrics endpoint exposes component health and backup gauges such as:

```text
oci_migrator_component_ok{component="redis"} 1
oci_migrator_backup_jobs 8
oci_migrator_backup_jobs_failed 1
oci_migrator_backup_job_last_run_timestamp{job="CustomerA",status="success"} 1779473400
```

## Job History

The UI shows recent runs in the Job Dashboard. The backend persists run history in:

```text
~/.oci/job_history.json
```

Authenticated API access:

```bash
curl -H "X-API-Token: <token>" http://127.0.0.1:8000/job-history
```

Each rclone run writes a persistent log file under:

```text
/var/log/oci-migrator/jobs/
```

In the Job Dashboard, use `Recent Runs` -> terminal button to view the log tail, or the download button to download the full log file for that run.
The same dashboard shows `Retention Days` and `Max Size`; saving those fields updates the managed logrotate config.

Log rotation is installed at:

```text
/etc/logrotate.d/migrator-job-logs
```

Default policy:

- daily rotation
- keep 14 daily rotated logs
- compress old logs
- `maxsize 10M`

Change these values in the UI, with `./install.sh --job-log-max-size 25M --job-log-retention-days 30`, or by editing the logrotate file directly.

## Runtime Config Export

After logging in, use the download button in the top bar to export a zip backup. It includes the runtime env file, OCI config, job definitions/history, rclone config, and referenced key files when present.

The archive can contain secrets. Store it securely.

## Local Sources

Local remotes have two modes:

- Server local folders are created under `/var/lib/oci-migrator/local`.
- Mounted external shares must already exist, for example under `/mnt/customer-share`.

The installer creates `/var/lib/oci-migrator/local` for the service user. Use `--local-data-root PATH` to choose another managed root.

When creating a server local folder in the UI, it can optionally be exposed as an SMB share:

- `Do Not Share` only creates the local folder.
- `Share to Everyone` creates a guest-access Samba share and opens TCP `445`.
- `Share to User` creates/updates the requested SMB user, sets the Samba password, creates the share, and opens TCP `445`.

The SMB password is not stored in the app config. Samba stores its own password hash. Deleting a remote that owns a managed share removes the Samba share block, but it does not delete the underlying local data folder.

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
/etc/oci-migrator/local-share.conf
/etc/systemd/timesyncd.conf.d/oci-migrator.conf
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
