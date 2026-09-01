SHELL := /usr/bin/env bash

PUBLIC_HOST ?=
API_PORT ?= 8000
LOCAL_DATA_ROOT ?=
SERVICE_PREFIX ?= migrator
ADMIN_PASSWORD ?=
PROMPT_ADMIN_PASSWORD ?= 0
ADMIN_ARGS := $(if $(ADMIN_PASSWORD),--admin-password "$(ADMIN_PASSWORD)") $(if $(filter 1,$(PROMPT_ADMIN_PASSWORD)),--prompt-admin-password)

.PHONY: help install install-print-token deploy doctor status start stop restart logs logs-api logs-worker package uninstall

help:
	@printf 'Cloud Migration Console commands\n\n'
	@printf '  make install              Install on this server\n'
	@printf '  make deploy               Deploy to SSH_HOST with scripts/deploy.sh\n'
	@printf '  make doctor               Check dependencies, services, ports, and API health\n'
	@printf '  make status               Show systemd service status\n'
	@printf '  make restart              Restart all Cloud Migration Console services\n'
	@printf '  make logs-api             Follow backend logs\n'
	@printf '  make logs-worker          Follow worker logs\n'
	@printf '  make package              Create a source tar.gz in dist-packages/\n'
	@printf '  make uninstall            Remove systemd services, preserve data\n\n'
	@printf 'Common variables:\n'
	@printf '  PUBLIC_HOST=<server-ip-or-dns> API_PORT=8000 LOCAL_DATA_ROOT=/srv/oci-migrator/local SERVICE_PREFIX=migrator PROMPT_ADMIN_PASSWORD=1\n'

install:
	./install.sh --api-port "$(API_PORT)" --service-prefix "$(SERVICE_PREFIX)" $(if $(LOCAL_DATA_ROOT),--local-data-root "$(LOCAL_DATA_ROOT)") $(if $(PUBLIC_HOST),--public-host "$(PUBLIC_HOST)") $(ADMIN_ARGS)

install-print-token:
	./install.sh --api-port "$(API_PORT)" --service-prefix "$(SERVICE_PREFIX)" --print-token $(if $(LOCAL_DATA_ROOT),--local-data-root "$(LOCAL_DATA_ROOT)") $(if $(PUBLIC_HOST),--public-host "$(PUBLIC_HOST)") $(ADMIN_ARGS)

deploy:
	./scripts/deploy.sh

doctor:
	SERVICE_PREFIX="$(SERVICE_PREFIX)" API_PORT="$(API_PORT)" ./scripts/doctor.sh

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

package:
	./scripts/package.sh

uninstall:
	SERVICE_PREFIX="$(SERVICE_PREFIX)" ./scripts/uninstall.sh
