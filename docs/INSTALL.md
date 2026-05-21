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
./install.sh --api-port 8001 --frontend-port 5174
./install.sh --stop-legacy-processes
./install.sh --no-frontend-service
```

## Multiple Installations On The Same Host

Use a unique directory, service prefix, ports, and env file:

```bash
./scripts/bootstrap.sh \
  --install-dir /opt/oci-migrator-dev \
  --public-host dev.example.com \
  -- \
  --service-prefix migrator-dev \
  --api-port 8100 \
  --frontend-port 5174 \
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
http://<server-ip-or-dns>:5173
```

## Verify

```bash
make doctor
make status
```
