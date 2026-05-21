SHELL := /usr/bin/env bash

PUBLIC_HOST ?=
API_PORT ?= 8000
FRONTEND_PORT ?= 5173
SERVICE_PREFIX ?= migrator
ADMIN_PASSWORD ?=
PROMPT_ADMIN_PASSWORD ?= 0
ADMIN_ARGS := $(if $(ADMIN_PASSWORD),--admin-password "$(ADMIN_PASSWORD)") $(if $(filter 1,$(PROMPT_ADMIN_PASSWORD)),--prompt-admin-password)

.PHONY: help install install-print-token deploy doctor status start stop restart logs logs-api logs-worker logs-frontend package uninstall

help:
	@printf 'OCI Migrator commands\n\n'
	@printf '  make install              Install on this server\n'
	@printf '  make deploy               Deploy to SSH_HOST with scripts/deploy.sh\n'
	@printf '  make doctor               Check dependencies, services, ports, and API health\n'
	@printf '  make status               Show systemd service status\n'
	@printf '  make restart              Restart all OCI Migrator services\n'
	@printf '  make logs-api             Follow backend logs\n'
	@printf '  make logs-worker          Follow worker logs\n'
	@printf '  make logs-frontend        Follow frontend logs\n'
	@printf '  make package              Create a source tar.gz in dist-packages/\n'
	@printf '  make uninstall            Remove systemd services, preserve data\n\n'
	@printf 'Common variables:\n'
	@printf '  PUBLIC_HOST=<server-ip-or-dns> API_PORT=8000 FRONTEND_PORT=5173 SERVICE_PREFIX=migrator PROMPT_ADMIN_PASSWORD=1\n'

install:
	./install.sh --api-port "$(API_PORT)" --frontend-port "$(FRONTEND_PORT)" --service-prefix "$(SERVICE_PREFIX)" $(if $(PUBLIC_HOST),--public-host "$(PUBLIC_HOST)") $(ADMIN_ARGS)

install-print-token:
	./install.sh --api-port "$(API_PORT)" --frontend-port "$(FRONTEND_PORT)" --service-prefix "$(SERVICE_PREFIX)" --print-token $(if $(PUBLIC_HOST),--public-host "$(PUBLIC_HOST)") $(ADMIN_ARGS)

deploy:
	./scripts/deploy.sh

doctor:
	SERVICE_PREFIX="$(SERVICE_PREFIX)" API_PORT="$(API_PORT)" FRONTEND_PORT="$(FRONTEND_PORT)" ./scripts/doctor.sh

status:
	SERVICE_PREFIX="$(SERVICE_PREFIX)" ./scripts/service.sh status

start:
	SERVICE_PREFIX="$(SERVICE_PREFIX)" ./scripts/service.sh start

stop:
	SERVICE_PREFIX="$(SERVICE_PREFIX)" ./scripts/service.sh stop

restart:
	SERVICE_PREFIX="$(SERVICE_PREFIX)" ./scripts/service.sh restart

logs: logs-api

logs-api:
	SERVICE_PREFIX="$(SERVICE_PREFIX)" ./scripts/service.sh logs api

logs-worker:
	SERVICE_PREFIX="$(SERVICE_PREFIX)" ./scripts/service.sh logs worker

logs-frontend:
	SERVICE_PREFIX="$(SERVICE_PREFIX)" ./scripts/service.sh logs frontend

package:
	./scripts/package.sh

uninstall:
	SERVICE_PREFIX="$(SERVICE_PREFIX)" ./scripts/uninstall.sh
