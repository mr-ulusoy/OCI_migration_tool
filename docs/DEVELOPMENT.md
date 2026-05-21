# Development

## Backend

```bash
cd backend
python3 -m venv ../venv
../venv/bin/python -m pip install -r requirements.lock
OCI_MIGRATOR_API_TOKEN=dev-token ../venv/bin/python -m uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

For local UI login, create a local env file with an admin password hash or use the legacy dev token through `VITE_API_TOKEN=dev-token`.

Redis is required for Celery tasks:

```bash
redis-server
```

Worker:

```bash
cd backend
OCI_MIGRATOR_REDIS_URL=redis://localhost:6379/0 ../venv/bin/python -m celery -A worker.celery_app worker --loglevel=info
```

## Frontend

```bash
cd frontend
npm ci
cp .env.example .env
npm run dev -- --host 0.0.0.0
```

## Checks

```bash
bash -n install.sh scripts/*.sh
python3 -m py_compile backend/main.py backend/worker.py backend/run_backups.py
cd frontend && npm run build
```

## Packaging

```bash
make package
```

This creates a source tarball in `dist-packages/`.
