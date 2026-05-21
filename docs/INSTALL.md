# Installation Guide

OCI Migrator is designed for repeatable installs on Ubuntu servers.

## Recommended: Bootstrap From GitHub

On a fresh server:

```bash
curl -fsSL https://raw.githubusercontent.com/mr-ulusoy/OCI_migration_tool/main/scripts/bootstrap.sh -o bootstrap.sh
chmod +x bootstrap.sh
./bootstrap.sh --public-host <server-ip-or-dns>
```

This will:

- install `git` if needed
- clone the repository to `/opt/oci-migrator`
- run `install.sh`
- create systemd services
- create `~/.oci-migrator.env`

## Manual Install

```bash
git clone https://github.com/mr-ulusoy/OCI_migration_tool.git
cd OCI_migration_tool
./install.sh --public-host <server-ip-or-dns>
```

## Common Options

```bash
./install.sh --public-host <server-ip-or-dns>
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

## Token Setup

The installer creates `~/.oci-migrator.env` with an API token.

To retrieve it on the server:

```bash
grep '^OCI_MIGRATOR_API_TOKEN=' ~/.oci-migrator.env
```

In the browser, set the token once:

```js
localStorage.setItem('OCI_MIGRATOR_API_TOKEN', '<token>');
```

Then open:

```text
http://<server-ip-or-dns>:5173
```

## Verify

```bash
make doctor
make status
```
