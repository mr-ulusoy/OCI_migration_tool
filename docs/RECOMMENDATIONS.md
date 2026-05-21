# Recommendations

These are the main things worth improving before treating OCI Migrator as a broadly shared production tool.

## 1. Add HTTPS If Inbound Access Is Needed

The app now runs on a single app/API port, typically `8000`. It has admin login, but if it ever gets direct inbound access from user networks, add HTTPS or place it behind VPN/private access controls.

Options:

- Caddy or Nginx reverse proxy
- TLS certificate
- IP allowlist or VPN
- expose only `443` when internet-facing

## 2. Move Sessions To HttpOnly Cookies Later

The frontend stores the admin session token in browser localStorage. That is acceptable for a private admin tool, but HttpOnly secure cookies are better for broader access.

Better options:

- reverse proxy authentication in front of the app
- HttpOnly secure session cookies
- per-user audit logging

## 3. Frontend Serving

Done: production install no longer runs `npm run preview`. FastAPI serves the built `frontend/dist` files directly from the backend service.

Future options if needed:

- serve `frontend/dist` with Caddy/Nginx
- proxy to FastAPI
- keep FastAPI bound to `127.0.0.1`

## 4. Runtime Config Backups

Done: the UI can export a runtime backup zip containing the env file, OCI config, job definitions/history, rclone config, and referenced key files when present.

The application writes important runtime state to:

```text
~/.oci
~/.config/rclone
~/.oci-migrator.env
```

Keep exported backups encrypted or otherwise access-controlled, because they may contain cloud credentials.

## 5. Job History, Health, And Errors

Done:

- job run history is persisted in `~/.oci/job_history.json`
- `/health` reports API readiness and dependency checks
- OCI/rclone failures return structured messages and hints for the UI

## 6. Enable CI And Add Real Tests

A GitHub Actions template is included at `docs/ci/github-actions.yml`. Copy it to `.github/workflows/ci.yml` when the GitHub token/repo permissions allow workflow updates. After that, add real backend and frontend tests around critical migration behavior.

```bash
bash -n install.sh scripts/*.sh
python3 -m py_compile backend/main.py backend/worker.py backend/run_backups.py backend/job_store.py
cd frontend && npm ci && npm audit --omit=dev && npm run build
```
