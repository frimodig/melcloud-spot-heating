#!/bin/sh
# Cron wrapper: loads credentials from .env (chmod 600) and runs the controller.
cd "$(dirname "$0")" || exit 1
set -a
. ./.env
set +a
exec .venv/bin/python ilp_optimizer.py "$@"
