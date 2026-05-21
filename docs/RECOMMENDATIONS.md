# Recommendations

These are the main things worth improving before treating OCI Migrator as a broadly shared production tool.

## 1. Put It Behind HTTPS

The app currently runs as an admin tool on ports `5173` and `8000`. It has admin login, but for shared use it should still sit behind HTTPS, VPN, or a reverse proxy.

Recommended next step:

- Caddy or Nginx reverse proxy
- TLS certificate
- Basic auth, SSO, or IP allowlist
- expose only port `443`

## 2. Move Sessions To HttpOnly Cookies Later

The frontend stores the admin session token in browser localStorage. That is acceptable for a private admin tool, but HttpOnly secure cookies are better for broader access.

Better options:

- reverse proxy authentication in front of the app
- HttpOnly secure session cookies
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
cd frontend && npm ci && npm audit --omit=dev && npm run build
```
