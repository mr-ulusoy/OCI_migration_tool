# Installation Guide

OCI Migrator is designed for repeatable installs on Ubuntu servers.

## Recommended: Bootstrap From GitHub

On a fresh server:

```bash
curl -fsSL https://raw.githubusercontent.com/mr-ulusoy/OCI_migration_tool/main/scripts/bootstrap.sh -o bootstrap.sh
chmod +x bootstrap.sh
./bootstrap.sh --public-host <server-ip-or-dns> --prompt-admin-password
```

This will:

- install `git` if needed
- clone the repository to `/opt/oci-migrator`
- run `install.sh`
- create systemd services
- create `~/.oci-migrator.env`
- store a hashed admin password
- install a root-owned helper for optional SMB sharing from the UI
- configure server timezone/NTP for reliable schedules and timestamps

## Manual Install

```bash
git clone https://github.com/mr-ulusoy/OCI_migration_tool.git
cd OCI_migration_tool
./install.sh --public-host <server-ip-or-dns> --prompt-admin-password
```

## Common Options

```bash
./install.sh --public-host <server-ip-or-dns>
./install.sh --public-host <server-ip-or-dns> --admin-password '<strong-password>'
./install.sh --public-host <server-ip-or-dns> --admin-password-file /path/to/password.txt
./install.sh --public-host migrator.example.com --open-firewall
./install.sh --api-port 8001
./install.sh --local-data-root /srv/oci-migrator/local
./install.sh --job-log-dir /var/log/oci-migrator/jobs
./install.sh --job-log-max-size 10M
./install.sh --job-log-retention-days 14
./install.sh --timezone Europe/Stockholm
./install.sh --ntp-servers "0.se.pool.ntp.org 1.se.pool.ntp.org"
./install.sh --local-share-helper /usr/local/sbin/oci-migrator-local-share
./install.sh --stop-legacy-processes
```

`--job-log-max-size` controls the `maxsize` value written to logrotate. The default is `10M`.
`--job-log-retention-days` controls how many daily rotated logs are kept. The default is `14`.
`--timezone` defaults to `Europe/Stockholm`, so Swedish daylight saving time is handled by the OS.
`--ntp-servers` defaults to the Swedish NTP pool and is written to `systemd-timesyncd`.
After installation, timezone and NTP servers can also be changed from `Settings` -> `Time & NTP`.

## Optional SMB Sharing

No SMB share is enabled during installation. The installer only places a root-owned helper and a sudoers rule so the web UI can enable a managed share later.

In the UI, choose `Add Remote` -> `Local / Mounted Share` -> `Server Local Folder`. If `SMB Share` is set to `Share to Everyone` or `Share to User`, the helper will:

- install/configure Samba if needed
- share the created folder under `/var/lib/oci-migrator/local`
- create/update the SMB user when user access is selected
- open inbound TCP `445` and save the firewall rule when supported

## Multiple Installations On The Same Host

Use a unique directory, service prefix, app/API port, and env file.
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

## Admin Password

The installer stores only a password hash in `~/.oci-migrator.env`.

Recommended interactive setup:

```bash
./install.sh --public-host <server-ip-or-dns> --prompt-admin-password
```

Non-interactive setup:

```bash
./install.sh --public-host <server-ip-or-dns> --admin-password '<strong-password>'
```

Reset from the server:

```bash
cd /opt/oci-migrator
./install.sh --admin-password '<new-strong-password>'
```

Then open and log in:

```text
http://<server-ip-or-dns>:8000
```

## Verify

```bash
make doctor
make status
```
