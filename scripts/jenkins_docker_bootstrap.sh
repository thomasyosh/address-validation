#!/usr/bin/env bash
# Copy config + dataset from Jenkins workspace/uploads into host PERSIST_DIR.
# Docker mounts PERSIST_DIR as /data inside the container (NOT the Jenkins workspace).
set -eu

WORKSPACE_DIR="${WORKSPACE:-$(pwd)}"
PERSIST_DIR="${ADDRESS_VALIDATION_HOME:-/tmp/address-validation-data-${JOB_NAME:-jenkins}}"
DATASET_FILENAME="address.xlsx"

CONFIG_SOURCE="$PERSIST_DIR/config.yaml"
CONFIG_LOCAL_SOURCE="$PERSIST_DIR/config.local.yaml"
DATASET_PATH="$PERSIST_DIR/$DATASET_FILENAME"

mkdir -p "$PERSIST_DIR"

echo "=== Docker bootstrap ==="
echo "WORKSPACE=$WORKSPACE_DIR"
echo "PERSIST (mounted as /data in container)=$PERSIST_DIR"

_first_existing_file() {
    local path
    for path in "$@"; do
        if [ -n "$path" ] && [ -f "$path" ]; then
            echo "$path"
            return 0
        fi
    done
    return 1
}

# --- config.yaml (uploads and workspace beat stale persist copies) ---
if uploaded="$(_first_existing_file \
    "$WORKSPACE_DIR/config_yaml" \
    "$WORKSPACE_DIR/config.yaml")"; then
    cp "$uploaded" "$CONFIG_SOURCE"
    echo "config.yaml <= $uploaded"
elif uploaded="$(_first_existing_file \
    "$WORKSPACE_DIR/jenkins/config.yaml")"; then
    cp "$uploaded" "$CONFIG_SOURCE"
    echo "config.yaml <= $uploaded"
elif [ -f "$CONFIG_SOURCE" ]; then
    echo "config.yaml <= reusing $CONFIG_SOURCE"
elif uploaded="$(_first_existing_file "$WORKSPACE_DIR/config.example.yaml")"; then
    cp "$uploaded" "$CONFIG_SOURCE"
    echo "config.yaml <= $uploaded (example — replace with real config.yaml in workspace or upload)"
fi

# --- config.local.yaml ---
if uploaded="$(_first_existing_file \
    "$WORKSPACE_DIR/config_local_yaml" \
    "$WORKSPACE_DIR/config.local.yaml")"; then
    cp "$uploaded" "$CONFIG_LOCAL_SOURCE"
    echo "config.local.yaml <= $uploaded"
elif [ -f "$CONFIG_LOCAL_SOURCE" ]; then
    echo "config.local.yaml <= reusing $CONFIG_LOCAL_SOURCE"
fi

# --- address.xlsx (many possible names/locations in workspace) ---
if uploaded="$(_first_existing_file \
    "$WORKSPACE_DIR/address_xlsx" \
    "$WORKSPACE_DIR/addresses_xlsx" \
    "$WORKSPACE_DIR/dataset_xlsx" \
    "$WORKSPACE_DIR/data/address.xlsx" \
    "$WORKSPACE_DIR/data/addresses.xlsx" \
    "$WORKSPACE_DIR/address.xlsx" \
    "$WORKSPACE_DIR/addresses.xlsx" \
    "$WORKSPACE_DIR/jenkins/address.xlsx" \
    "${ADDRESSES_XLSX_SOURCE:-}")"; then
    cp "$uploaded" "$DATASET_PATH"
    echo "address.xlsx <= $uploaded"
elif [ -f "$DATASET_PATH" ]; then
    echo "address.xlsx <= reusing $DATASET_PATH"
elif [ -n "${ADDRESSES_XLSX_URL:-}" ]; then
    echo "Downloading address.xlsx from ADDRESSES_XLSX_URL..."
    curl -fsSL "$ADDRESSES_XLSX_URL" -o "$DATASET_PATH"
    echo "address.xlsx <= download"
fi

echo "--- Workspace (git checkout; container does NOT see this directly) ---"
ls -la "$WORKSPACE_DIR/config.yaml" "$WORKSPACE_DIR/config.example.yaml" 2>/dev/null || true
ls -la "$WORKSPACE_DIR/data/" 2>/dev/null || echo "(no workspace data/ folder)"

echo "--- PERSIST (docker -v ${PERSIST_DIR}:/data) ---"
ls -la "$PERSIST_DIR/" 2>/dev/null || true

if [ ! -f "$CONFIG_SOURCE" ]; then
    echo "ERROR: No config.yaml in $PERSIST_DIR"
    echo "  Put config.yaml in workspace root, commit jenkins/config.yaml, or upload config_yaml."
    exit 1
fi

if [ ! -f "$DATASET_PATH" ]; then
    echo "ERROR: No $DATASET_FILENAME in $PERSIST_DIR"
    echo "  Put data/address.xlsx in workspace, upload address_xlsx, or set ADDRESSES_XLSX_URL."
    echo "  Note: files in \$WORKSPACE/data/ are NOT visible inside Docker until copied here."
    exit 1
fi

echo "Persist directory ready."
