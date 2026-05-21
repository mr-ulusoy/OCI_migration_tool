# Recommendations

These are the main things worth improving before treating OCI Migrator as a broadly shared production tool.

## 1. Put It Behind HTTPS And Auth

The app currently runs as an admin tool on ports `5173` and `8000`. For shared use, put it behind a VPN or reverse proxy with HTTPS and authentication.

Recommended next step:

- Caddy or Nginx reverse proxy
- TLS certificate
- Basic auth, SSO, or IP allowlist
- expose only port `443`

## 2. Avoid Long-Term Tokens In Browser Storage

The frontend currently reads `OCI_MIGRATOR_API_TOKEN` from browser localStorage or a Vite env var. That is acceptable for a private admin tool, but not ideal for broader access.

Better options:

- reverse proxy authentication in front of the app
- short-lived sessions
- per-user audit logging

## 3. Replace Vite Preview For Production

`npm run preview` is convenient and works for internal deployments, but a static server or reverse proxy is cleaner for production.

Better options:

- serve `frontend/dist` with Caddy/Nginx
- proxy `/api` to FastAPI
- keep FastAPI bound to `127.0.0.1`

## 4. Add Backups For Runtime Config

The application writes important runtime state to:

```text
~/.oci
~/.config/rclone
~/.oci-migrator.env
```

Back these up before upgrades, especially when colleagues will manage real migration jobs.

## 5. Enable CI And Add Real Tests

A GitHub Actions template is included at `docs/ci/github-actions.yml`. Copy it to `.github/workflows/ci.yml` when the GitHub token/repo permissions allow workflow updates. After that, add real backend and frontend tests around critical migration behavior.

```bash
bash -n install.sh scripts/*.sh
python3 -m py_compile backend/main.py backend/worker.py backend/run_backups.py
cd frontend && npm ci && npm run build
```
