#!/usr/bin/env bash
# Prepare persistent /data directory on the Jenkins host before docker run.
set -eu

WORKSPACE_DIR="${WORKSPACE:-$(pwd)}"
PERSIST_DIR="${ADDRESS_VALIDATION_HOME:-/tmp/address-validation-data-${JOB_NAME:-jenkins}}"
DATASET_FILENAME="address.xlsx"

CONFIG_SOURCE="$PERSIST_DIR/config.yaml"
CONFIG_LOCAL_SOURCE="$PERSIST_DIR/config.local.yaml"
DATASET_PATH="$PERSIST_DIR/$DATASET_FILENAME"

mkdir -p "$PERSIST_DIR"

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

if uploaded="$(_first_existing_file \
    "$WORKSPACE_DIR/config_yaml" \
    "$WORKSPACE_DIR/config.yaml")"; then
    cp "$uploaded" "$CONFIG_SOURCE"
    echo "Installed config.yaml from upload: $uploaded"
elif [ -f "$CONFIG_SOURCE" ]; then
    echo "Reusing saved config.yaml: $CONFIG_SOURCE"
elif [ -f "$WORKSPACE_DIR/config.example.yaml" ]; then
    cp "$WORKSPACE_DIR/config.example.yaml" "$CONFIG_SOURCE"
    echo "Installed config from config.example.yaml"
elif [ -f "$WORKSPACE_DIR/jenkins/config.yaml" ]; then
    cp "$WORKSPACE_DIR/jenkins/config.yaml" "$CONFIG_SOURCE"
    echo "Installed config from jenkins/config.yaml"
fi

if uploaded="$(_first_existing_file \
    "$WORKSPACE_DIR/config_local_yaml" \
    "$WORKSPACE_DIR/config.local.yaml")"; then
    cp "$uploaded" "$CONFIG_LOCAL_SOURCE"
    echo "Installed config.local.yaml from upload: $uploaded"
elif [ -f "$CONFIG_LOCAL_SOURCE" ]; then
    echo "Reusing saved config.local.yaml: $CONFIG_LOCAL_SOURCE"
fi

if uploaded="$(_first_existing_file \
    "$WORKSPACE_DIR/address_xlsx" \
    "$WORKSPACE_DIR/addresses_xlsx" \
    "$WORKSPACE_DIR/dataset_xlsx" \
    "$WORKSPACE_DIR/address.xlsx" \
    "$WORKSPACE_DIR/data/address.xlsx" \
    "$WORKSPACE_DIR/jenkins/address.xlsx" \
    "${ADDRESSES_XLSX_SOURCE:-}")"; then
    cp "$uploaded" "$DATASET_PATH"
    echo "Installed dataset from: $uploaded"
elif [ -f "$DATASET_PATH" ]; then
    echo "Reusing saved dataset: $DATASET_PATH"
elif [ -n "${ADDRESSES_XLSX_URL:-}" ]; then
    echo "Downloading dataset from ADDRESSES_XLSX_URL..."
    curl -fsSL "$ADDRESSES_XLSX_URL" -o "$DATASET_PATH"
fi

if [ ! -f "$CONFIG_SOURCE" ]; then
    echo "ERROR: No config.yaml. Upload config_yaml or add jenkins/config.yaml to GitLab."
    exit 1
fi

if [ ! -f "$DATASET_PATH" ]; then
    echo "ERROR: No $DATASET_FILENAME. Upload address_xlsx or set ADDRESSES_XLSX_URL."
    exit 1
fi

echo "Persist directory ready: $PERSIST_DIR"
