#!/usr/bin/env bash
# Container entrypoint — expects /data mounted with config + dataset (+ optional config.local.yaml).
set -eu

DATA_DIR="${DATA_DIR:-/data}"
DB_PATH="${DATA_DIR}/address_validation.db"
DATASET_PATH="${DATA_DIR}/address.xlsx"
CONFIG_LOCAL="${DATA_DIR}/config.local.yaml"

cd /app

echo "=== Address Search Validation (Docker) ==="
echo "DATA_DIR=$DATA_DIR"

if [ -f "${DATA_DIR}/config.yaml" ]; then
    cp "${DATA_DIR}/config.yaml" config.yaml
    echo "Using config: ${DATA_DIR}/config.yaml"
elif [ -f config.example.yaml ]; then
    cp config.example.yaml config.yaml
    echo "WARNING: ${DATA_DIR}/config.yaml missing — using config.example.yaml from image"
    echo "  Fix: ensure jenkins_docker_bootstrap.sh copied workspace config to ${DATA_DIR}/"
else
    echo "ERROR: Missing ${DATA_DIR}/config.yaml"
    ls -la "${DATA_DIR}/" 2>/dev/null || echo "(cannot list ${DATA_DIR})"
    exit 1
fi

if [ ! -f "$DATASET_PATH" ]; then
    echo "ERROR: Missing dataset at $DATASET_PATH"
    ls -la "${DATA_DIR}/" 2>/dev/null || echo "(cannot list ${DATA_DIR})"
    exit 1
fi
echo "Dataset: $DATASET_PATH"

python3 - "$CONFIG_LOCAL" "$DB_PATH" "$DATASET_PATH" <<'PY'
import sys
from pathlib import Path

import yaml

local_path = Path(sys.argv[1])
db_path = sys.argv[2]
dataset_path = sys.argv[3]

data = {}
if local_path.exists() and local_path.read_text(encoding="utf-8").strip():
    loaded = yaml.safe_load(local_path.read_text(encoding="utf-8"))
    if isinstance(loaded, dict):
        data = loaded

data.setdefault("database", {})["path"] = db_path
data.setdefault("dataset", {})["path"] = dataset_path
local_path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8")
Path("/app/config.local.yaml").write_text(
    local_path.read_text(encoding="utf-8"), encoding="utf-8"
)
print("Wrote /app/config.local.yaml (db + dataset paths)")
PY

echo "Database: $DB_PATH"
echo "Note: SQLite is created at start; rows are saved every ~50 addresses (batch_save_size)."
echo "      Progress logs appear every progress_every rows (default 50). DB file grows mid-run."
echo "Running: python main.py $*"
exec python3 -u main.py "$@"
