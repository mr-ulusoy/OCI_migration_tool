# Production Readiness

OCI Migrator Pro is intended to run as a self-hosted administrative service. The installation, dashboard configuration, backup operations, monitoring, recovery, and runtime backup procedures are documented in the [Dashboard Configuration](OPERATIONS.md) and [Server Runbook](RUNBOOK.md). This page tracks only the remaining controls that matter before wider production use.

## 1. Protect Administrative Traffic With TLS

Use TLS for all browser and API access, including access from internal corporate networks. The application currently serves HTTP on its configured app/API port, typically `8000`.

Terminate TLS with infrastructure that fits the target environment, for example:

- a corporate load balancer or ingress gateway
- an existing reverse proxy
- a VPN or private access gateway that provides encrypted application access

Restrict the application port to approved management networks. A dedicated Caddy or Nginx installation is optional when existing infrastructure already provides TLS and access control.

## 2. Harden Browser Sessions

The dashboard currently stores a signed, expiring admin bearer token in browser `localStorage`. Before allowing broader browser access, migrate authentication to an `HttpOnly`, `Secure`, and appropriate `SameSite` session cookie and add CSRF protection for state-changing requests.

For a small deployment operated through one controlled administrator account, the current model can be used only behind tightly restricted private access. When several administrators use the service, add:

- individual identities through the organization's SSO/OIDC provider
- role-based access where responsibilities differ
- an audit trail that records who changed settings, credentials, jobs, or migration state

## 3. Keep Continuous Integration Required

The active workflow at [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs on pushes to `main` and on pull requests. It validates shell syntax, compiles the backend, runs the backend unit tests, audits production frontend dependencies, lints the frontend, and creates a production build.

For a shared repository, protect `main` and require both CI jobs before merge. Keep end-to-end installation, upgrade, backup, restore, SMB/NFS, and OCI integration tests in a disposable test environment because they require operating-system services and cloud resources that unit tests do not provide.

## 4. Make Backend Dependencies Reproducible

The frontend dependency tree is locked by `package-lock.json`, but `backend/requirements.txt` currently contains unpinned package names. Create and review a tested lock file before using unattended production upgrades so a new upstream release cannot silently change an installation or CI run.

Use an automated dependency update process that opens reviewed pull requests and lets CI validate each update before merge. Avoid upgrading production dependencies directly from an unreviewed moving dependency set.

## 5. Match Identity Controls To The Deployment

SSO, role-based access, and per-user auditing are recommended when the service is shared across teams or subject to compliance requirements. They are not required for a single-purpose appliance with one accountable administrator, private network access, and controlled host access.

Review this decision whenever the number of administrators, network exposure, or compliance scope changes.
