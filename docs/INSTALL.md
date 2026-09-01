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
- install Caddy support for managed HTTPS
- open local Ubuntu firewall TCP `22`, `80`, `443`, `8000` or the chosen setup/app API port, `445`, and `2049`
- install a root-owned helper for optional SMB/NFS sharing from the UI
- configure server timezone/NTP for reliable schedules and timestamps

The local firewall is opened by default. Your OCI Security List or NSG still needs matching inbound rules for the networks that should reach the server.

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
./install.sh --public-host migrator.example.com --no-open-firewall
./install.sh --api-port 8001
./install.sh --local-data-root /srv/oci-migrator/local
./install.sh --job-log-dir /var/log/oci-migrator/jobs
./install.sh --job-log-max-size 10M
./install.sh --job-log-retention-days 14
./install.sh --timezone Europe/Stockholm
./install.sh --ntp-servers "0.se.pool.ntp.org 1.se.pool.ntp.org"
./install.sh --local-share-helper /usr/local/sbin/oci-migrator-local-share
./install.sh --uninstall-helper /usr/local/sbin/oci-migrator-uninstall
./install.sh --stop-legacy-processes
```

`--job-log-max-size` controls the `maxsize` value written to logrotate. The default is `10M`.
`--job-log-retention-days` controls how many daily rotated logs are kept. The default is `14`.
`--timezone` defaults to `Europe/Stockholm`, so Swedish daylight saving time is handled by the OS.
`--ntp-servers` defaults to the Swedish NTP pool and is written to `systemd-timesyncd`.
`--no-open-firewall` skips opening local Ubuntu firewall ports during install. By default the installer opens TCP `22`, `80`, `443`, the app/API port, `445`, and `2049`.
After installation, timezone and NTP servers can also be changed from `Settings` -> `Time & NTP`.

## HTTPS Setup

The initial HTTP endpoint exists so a new server can be configured before its DNS record or certificate is ready. It is not a production endpoint. After the first login, open `Settings` -> `HTTPS & Certificates` and select one of these modes:

### Let's Encrypt

Use this for a publicly resolvable DNS hostname when the server can receive inbound traffic on TCP `80` and `443`.

1. Create an A and/or AAAA record such as `migrator.example.com` that resolves to the server.
2. Allow inbound TCP `80` and `443` through the on-premises firewall or OCI Security List/NSG.
3. Select `Let's Encrypt`, enter the hostname and an optional ACME contact email, then apply.

Caddy obtains the certificate, redirects HTTP to HTTPS, and renews it automatically. DNS must resolve correctly before applying the setting. Port `80` is required for normal HTTP validation and redirect traffic; port `443` serves the dashboard and API.

### Corporate Certificate

Use this when the customer PKI team issues the certificate.

1. Place a PEM full-chain certificate and its matching unencrypted PEM private key on the server in a root-controlled directory.
2. Select `Corporate Certificate` and enter the dashboard DNS hostname plus both absolute server paths.
3. Apply the setting. OCI Migrator verifies the hostname, expiration, certificate/key match, and Caddy configuration before activating it.

The validated files are copied into protected OCI Migrator TLS storage. Reapply the setting after the PKI team renews or replaces the certificate. Client devices must trust the corporate issuing CA.

### External TLS

Use this when an enterprise load balancer, ingress gateway, or existing reverse proxy owns the certificate. Enter the public DNS hostname and configure that external component to forward requests to the OCI Migrator app/API port. It must send `X-Forwarded-Proto: https`. Caddy is not started in this mode.

### HTTP Setup

Use this only for initial configuration or controlled recovery. Select the acknowledgement checkbox before applying the mode. The acknowledgement records that the administrator accepts unencrypted traffic and suppresses the repeated health warning; it does not add encryption or make HTTP suitable for production.

The managed local Caddy service is isolated from `/etc/caddy/Caddyfile`; OCI Migrator uses `/etc/oci-migrator/Caddyfile` and its own `migrator-tls.service`. Failed Caddy activation restores the previous managed TLS files.

## DHCP and Static IPv4

The installer leaves the operating system on its existing network configuration, which is normally DHCP on a new Ubuntu cloud instance. It installs `/usr/local/sbin/oci-migrator-network` and a restricted sudoers rule so an administrator can later choose `DHCP` or `Static IPv4` from `Settings` -> `Network`.

Static mode writes `/etc/netplan/99-oci-migrator-network.yaml`. Before applying a static address on OCI or another cloud, assign or reserve that private IP on the instance VNIC. Every dashboard change starts a three-minute confirmation window. Confirm the working configuration from the dashboard; otherwise the previous managed Netplan file is restored automatically.

If the address changes, open the dashboard on the new address, sign in again, and confirm the pending network configuration before the timer expires.

## Optional SMB/NFS Sharing

No SMB or NFS share is enabled during installation. The installer opens the local firewall ports and places a root-owned helper plus a sudoers rule so the web UI can enable a managed share later.

In the UI, choose `Add Remote` -> `Local / Mounted Share` -> `Server Local Folder`.

If `SMB Share` is set to `Share to Everyone` or `Share to User`, the helper will:

- install/configure Samba if needed
- share the created folder under `/var/lib/oci-migrator/local`
- create/update the SMB user when user access is selected
- ensure inbound TCP `445` is open and save the firewall rule when supported

If `Enable NFSv4 Share` is selected, the helper will:

- install/configure `nfs-kernel-server` if needed
- export the created folder to the allowed client IPs, hostnames, or CIDR ranges
- map NFS client users to the OCI Migrator service user so the managed ingest folder remains writable without `no_root_squash` or world-writable permissions
- ensure inbound TCP `2049` is open and save the firewall rule when supported
- return a Linux mount command, for example `sudo mount -t nfs4 SERVER:/var/lib/oci-migrator/local/customer-a /mnt/customer-a`

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

For initial setup, open and log in:

```text
http://<server-ip-or-dns>:8000
```

Configure HTTPS immediately afterward and use `https://<dashboard-dns-name>` for normal operation.

## Verify

```bash
make doctor
make status
```
