#!/usr/bin/env bash
# Copy the VM's history, telemetry and price-history files into a local folder of their
# own, for `./run.sh --report --data-dir <dest>`. Safe to re-run: scp overwrites the same
# month's file with the latest rows, and never touches files in the project directory.
#
# Usage: scripts/pull-telemetry.sh [dest-dir] [host]
#   dest-dir  default: ~/vm-data
#   host      default: $ILP_VM_HOST (e.g. ubuntu@your-vm); required if not set
# The app's directory on the VM, relative to the login's home: $ILP_VM_DIR (default melcloud-spot-heating).
set -euo pipefail

DEST="${1:-$HOME/vm-data}"
HOST="${2:-${ILP_VM_HOST:?give the host as the second argument or set ILP_VM_HOST}}"
REMOTE_DIR="${ILP_VM_DIR:-melcloud-spot-heating}"

mkdir -p "$DEST"

for pattern in 'history-*.csv' 'telemetry-*.csv' 'price_history.csv'; do
  scp "${HOST}:${REMOTE_DIR}/${pattern}" "$DEST/" 2>&1 \
    | grep -v "no such file or directory" || true
done

echo "Copied into $DEST:"
ls -la "$DEST"
