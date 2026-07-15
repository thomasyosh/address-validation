#!/usr/bin/env bash
# Invoked as: bash scripts/jenkins_post_deploy.sh
# Uses bash (not POSIX sh). Do not run with sh.
# Script version: 2026-07-14f (get-pip bootstrap when ensurepip missing)

set -eu

echo "========================================================"
echo "Address Search Validation (jenkins_post_deploy.sh 2026-07-14f)"
echo "========================================================"

DATASET_FILENAME="address.xlsx"

# Ignore Jenkins env vars that point at system dirs the build user cannot write.
case "${ADDRESS_VALIDATION_HOME:-}" in
    /var/jenkins|/var/jenkins/*|/var/lib/jenkins|/var/lib/jenkins/*|*var/jenkins*|*var/lib/jenkins*)
        echo "WARNING: Ignoring ADDRESS_VALIDATION_HOME=${ADDRESS_VALIDATION_HOME}"
        unset ADDRESS_VALIDATION_HOME
        ;;
esac

# pip and other tools use HOME — redirect away from /var/lib/jenkins.
export HOME="/tmp/jenkins-home-${JOB_NAME:-jenkins}-${BUILD_NUMBER:-0}"
export PIP_CACHE_DIR="/tmp/pip-cache-${JOB_NAME:-jenkins}"
export TMPDIR="/tmp"
mkdir -p "$HOME" "$PIP_CACHE_DIR"

GIT_REPO_URL="${ADDRESS_VALIDATION_GIT_URL:-<your-gitlab-clone-url>}"
WORKSPACE_DIR="${WORKSPACE:-$(pwd)}"

# --- Resolve repo root ---
_is_repo_root() {
    [ -f "$1/main.py" ] && [ -f "$1/requirements.txt" ] && [ -f "$1/scripts/jenkins_post_deploy.sh" ]
}

_resolve_repo_dir() {
    local candidate
    for candidate in \
        "$WORKSPACE_DIR" \
        "$WORKSPACE_DIR/address-validation" \
        "$WORKSPACE_DIR/address-validation/address-validation"; do
        if _is_repo_root "$candidate"; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}

_flatten_double_nested_repo() {
    local outer="$WORKSPACE_DIR/address-validation"
    local inner="$outer/address-validation"

    if ! _is_repo_root "$inner"; then
        return 1
    fi
    if _is_repo_root "$WORKSPACE_DIR" || _is_repo_root "$outer"; then
        return 1
    fi

    echo "Detected double-nested repo at $inner — flattening to $WORKSPACE_DIR ..."
    shopt -s dotglob nullglob
    mv "$inner"/* "$WORKSPACE_DIR/"
    rmdir "$inner" 2>/dev/null || true
    rmdir "$outer" 2>/dev/null || true
    shopt -u dotglob nullglob
    echo "$WORKSPACE_DIR"
}

REPO_DIR=""
if REPO_DIR="$(_resolve_repo_dir)"; then
    echo "Using existing checkout at $REPO_DIR (no git clone needed)"
elif REPO_DIR="$(_flatten_double_nested_repo)"; then
    echo "Flattened repo is now at $REPO_DIR"
else
    REPO_DIR="${ADDRESS_VALIDATION_REPO_DIR:-$WORKSPACE_DIR}"
    if [ -d "$REPO_DIR/.git" ]; then
        echo "Updating existing clone at $REPO_DIR ..."
        git -C "$REPO_DIR" fetch --all
        git -C "$REPO_DIR" pull --ff-only
    else
        if [ "$GIT_REPO_URL" = "<your-gitlab-clone-url>" ] || [ -z "$GIT_REPO_URL" ]; then
            echo "ERROR: No repo in workspace and ADDRESS_VALIDATION_GIT_URL is not set."
            echo "  Configure Jenkins → Source Code Management → Git (recommended)."
            exit 1
        fi
        echo "Cloning from GitLab into $REPO_DIR ..."
        git clone "$GIT_REPO_URL" "$REPO_DIR"
    fi
fi

# --- Writable persist directory (/tmp is always safe on Jenkins agents) ---
_abs_path() {
    local path="$1"
    if [ "${path#/}" = "$path" ]; then
        path="$WORKSPACE_DIR/$path"
    fi
    echo "$path"
}

_is_unsafe_persist_path() {
    case "$1" in
        /var/jenkins|/var/jenkins/*|/var/lib/jenkins|/var/lib/jenkins/*) return 0 ;;
        "") return 0 ;;
        *) return 1 ;;
    esac
}

_can_write_dir() {
    local base="$1"
    [ -n "$base" ] || return 1
    _is_unsafe_persist_path "$base" && return 1
    mkdir -p "$base" 2>/dev/null && [ -w "$base" ]
}

_pick_persist_dir() {
    local candidate

    if [ -n "${ADDRESS_VALIDATION_HOME:-}" ]; then
        candidate="$(_abs_path "$ADDRESS_VALIDATION_HOME")"
        if ! _is_unsafe_persist_path "$candidate" && _can_write_dir "$candidate"; then
            echo "$candidate"
            return 0
        fi
        echo "WARNING: ADDRESS_VALIDATION_HOME=${ADDRESS_VALIDATION_HOME} is unusable; trying fallbacks." >&2
    fi

    for candidate in \
        "/tmp/address-validation-data-${JOB_NAME:-jenkins}" \
        "$WORKSPACE_DIR/.address-validation-persist"; do
        if _can_write_dir "$candidate"; then
            echo "$candidate"
            return 0
        fi
    done

    return 1
}

if ! PERSIST_DIR="$(_pick_persist_dir)"; then
    echo "ERROR: Could not find a writable directory for persistent data."
    exit 1
fi

VALIDATION_DATA="$PERSIST_DIR/data"
VALIDATION_DB="$PERSIST_DIR/address_validation.db"
CONFIG_SOURCE="$PERSIST_DIR/config.yaml"
CONFIG_LOCAL_SOURCE="$PERSIST_DIR/config.local.yaml"
DATASET_PATH="$VALIDATION_DATA/$DATASET_FILENAME"

if ! mkdir -p "$VALIDATION_DATA" 2>/dev/null; then
    echo "ERROR: Cannot create $VALIDATION_DATA"
    echo "  HOME=${HOME:-?} JENKINS_HOME=${JENKINS_HOME:-?} USER=${USER:-?}"
    exit 1
fi

echo "Workspace: $WORKSPACE_DIR"
echo "Repo: $REPO_DIR"
echo "Persistent data: $PERSIST_DIR"
echo "Env: HOME=${HOME:-?} JENKINS_HOME=${JENKINS_HOME:-?} ADDRESS_VALIDATION_HOME=${ADDRESS_VALIDATION_HOME:-<unset>}"

cd "$REPO_DIR"
echo "Using repo at: $(pwd)"

if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 is not installed on this Jenkins agent."
    exit 1
fi

_setup_python() {
    local venv_dir="$WORKSPACE_DIR/.venv"
    local pydeps="$WORKSPACE_DIR/.pydeps"
    local py pip_cmd get_pip

    _python_has_deps() {
        "$1" -c "import httpx, openpyxl, yaml" 2>/dev/null
    }

    _pip_install_reqs() {
        local py="$1"
        "$py" -m pip install -r requirements.txt -q \
            --trusted-host pypi.org \
            --trusted-host files.pythonhosted.org
    }

    _pip_install_reqs_user() {
        local py="$1"
        "$py" -m pip install -r requirements.txt -q --user \
            --trusted-host pypi.org \
            --trusted-host files.pythonhosted.org
    }

    _bootstrap_get_pip() {
        py="$1"
        get_pip="$TMPDIR/get-pip.py"
        if [ -f "$REPO_DIR/scripts/get-pip.py" ]; then
            get_pip="$REPO_DIR/scripts/get-pip.py"
        elif [ ! -f "$get_pip" ]; then
            echo "Downloading get-pip.py (bootstrap pip without python3-pip package) ..."
            curl -fsSL https://bootstrap.pypa.io/get-pip.py -o "$get_pip"
        fi
        "$py" "$get_pip" --no-warn-script-location
        "$py" -m pip --version >/dev/null 2>&1
    }

    # System Python already has dependencies (e.g. admin installed rpms).
    if _python_has_deps python3; then
        PYTHON="python3"
        echo "Python: $(python3 --version) (system packages already installed)"
        return 0
    fi

    if [ ! -x "$venv_dir/bin/python" ]; then
        echo "Creating Python venv in $venv_dir ..."
        if ! python3 -m venv "$venv_dir" 2>/dev/null; then
            echo "WARNING: python3 -m venv failed; trying --without-pip ..."
            python3 -m venv --without-pip "$venv_dir" 2>/dev/null \
                || python3 -m venv --without-pip --system-site-packages "$venv_dir"
        fi
    fi

    py="$venv_dir/bin/python"

    if [ -x "$venv_dir/bin/pip" ]; then
        "$venv_dir/bin/pip" install -r requirements.txt -q \
            --trusted-host pypi.org --trusted-host files.pythonhosted.org
        PYTHON="$py"
        echo "Python: $($PYTHON --version) (venv + pip)"
        return 0
    fi

    if [ -x "$py" ] && "$py" -m pip --version >/dev/null 2>&1; then
        _pip_install_reqs "$py"
        PYTHON="$py"
        echo "Python: $($PYTHON --version) (venv + python -m pip)"
        return 0
    fi

    if [ -x "$py" ]; then
        echo "Bootstrapping pip in venv via ensurepip ..."
        if "$py" -m ensurepip --upgrade 2>/dev/null && "$py" -m pip --version >/dev/null 2>&1; then
            _pip_install_reqs "$py"
            PYTHON="$py"
            echo "Python: $($PYTHON --version) (venv + ensurepip)"
            return 0
        fi
        echo "ensurepip unavailable; trying get-pip.py ..."
        if _bootstrap_get_pip "$py"; then
            _pip_install_reqs "$py"
            PYTHON="$py"
            echo "Python: $($PYTHON --version) (venv + get-pip.py)"
            return 0
        fi
    fi

    if command -v pip3 >/dev/null 2>&1; then
        echo "Using system pip3 with --target $pydeps ..."
        mkdir -p "$pydeps"
        pip3 install -r requirements.txt -q --target "$pydeps" \
            --trusted-host pypi.org --trusted-host files.pythonhosted.org
        PYTHON="python3"
        export PYTHONPATH="$pydeps${PYTHONPATH:+:$PYTHONPATH}"
        echo "Python: $(python3 --version) (pip3 --target)"
        return 0
    fi

    echo "Bootstrapping pip for system python3 via get-pip.py ..."
    if _bootstrap_get_pip python3; then
        _pip_install_reqs_user python3
        PYTHON="python3"
        echo "Python: $(python3 --version) (get-pip.py --user)"
        return 0
    fi

    echo "ERROR: Could not install Python dependencies on this Jenkins agent."
    echo "  python3 exists but pip/ensurepip/get-pip all failed."
    echo "  Ask admin: yum install python3-pip python3-venv   (or apt equivalent)"
    echo "  Or allow outbound HTTPS to bootstrap.pypa.io and pypi.org through the proxy."
    exit 1
}

_setup_python

# --- Jenkins File Parameter uploads (Build with Parameters) ---
# Parameter names → files in $WORKSPACE:
#   address_xlsx      → dataset (data/address.xlsx)
#   config_yaml       → config.yaml
#   config_local_yaml → config.local.yaml (proxy etc.)
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
    echo "Reusing saved config.yaml (no upload this build): $CONFIG_SOURCE"
elif [ ! -f "$CONFIG_SOURCE" ]; then
    for candidate in "$REPO_DIR/jenkins/config.yaml" "$REPO_DIR/config.example.yaml"; do
        if [ -f "$candidate" ]; then
            cp "$candidate" "$CONFIG_SOURCE"
            echo "Installed config from repo: $candidate"
            break
        fi
    done
fi

if uploaded="$(_first_existing_file \
    "$WORKSPACE_DIR/config_local_yaml" \
    "$WORKSPACE_DIR/config.local.yaml")"; then
    cp "$uploaded" "$CONFIG_LOCAL_SOURCE"
    echo "Installed config.local.yaml from upload: $uploaded"
elif [ -f "$CONFIG_LOCAL_SOURCE" ]; then
    echo "Reusing saved config.local.yaml (no upload this build): $CONFIG_LOCAL_SOURCE"
fi

if uploaded="$(_first_existing_file \
    "$WORKSPACE_DIR/address_xlsx" \
    "$WORKSPACE_DIR/addresses_xlsx" \
    "$WORKSPACE_DIR/dataset_xlsx" \
    "$WORKSPACE_DIR/address.xlsx" \
    "$WORKSPACE_DIR/data/address.xlsx" \
    "$WORKSPACE_DIR/data/addresses.xlsx" \
    "$REPO_DIR/data/address.xlsx" \
    "$REPO_DIR/jenkins/address.xlsx" \
    "${ADDRESSES_XLSX_SOURCE:-}")"; then
    cp "$uploaded" "$DATASET_PATH"
    echo "Installed dataset from: $uploaded"
elif [ -f "$DATASET_PATH" ]; then
    echo "Reusing saved dataset (no upload this build): $DATASET_PATH"
elif [ ! -f "$DATASET_PATH" ] && [ -n "${ADDRESSES_XLSX_URL:-}" ]; then
    echo "Downloading dataset from ADDRESSES_XLSX_URL..."
    curl -fsSL "$ADDRESSES_XLSX_URL" -o "$DATASET_PATH"
    echo "Installed dataset from URL"
fi

if [ -f "$CONFIG_SOURCE" ]; then
    cp "$CONFIG_SOURCE" config.yaml
    echo "Using config from $CONFIG_SOURCE"
elif [ -f config.yaml ]; then
    echo "Using config.yaml from repo checkout"
elif [ -f config.example.yaml ]; then
    cp config.example.yaml config.yaml
    echo "WARNING: using config.example.yaml from repo only"
else
    echo "ERROR: No config.yaml. Upload config_yaml (File Parameter) or add jenkins/config.yaml to GitLab."
    exit 1
fi

if [ ! -f "$DATASET_PATH" ]; then
    echo "ERROR: Missing $DATASET_FILENAME. Use Build with Parameters and upload address_xlsx."
    echo "  Or commit data/address.xlsx to GitLab, or set ADDRESSES_XLSX_URL."
    echo "  Expected persist path: $DATASET_PATH"
    exit 1
fi

# Merge runtime paths into config.local.yaml (preserve proxy settings from upload)
"$PYTHON" - "$CONFIG_LOCAL_SOURCE" "$VALIDATION_DB" "$DATASET_PATH" <<'PY'
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
print(f"Wrote config.local.yaml (db + dataset paths applied)")
PY
cp "$CONFIG_LOCAL_SOURCE" config.local.yaml

export http_proxy="${http_proxy:-http://smoproxy:8080/}"
export https_proxy="${https_proxy:-http://smoproxy:8080/}"
export NO_PROXY="${NO_PROXY:-ase.testingaddress.com,10.77.242.157,10.0.0.0/8,localhost,127.0.0.1}"

# shellcheck source=jenkins_validate_args.sh
source "$REPO_DIR/scripts/jenkins_validate_args.sh"
echo "ASE fetch profile: batch_size=${ASE_BATCH_SIZE} concurrency=single-thread rps=${ASE_RPS}"
echo "SQLite DB (grows during fetch): $VALIDATION_DB"

echo "Starting validate ..."
"${PYTHON}" main.py "${JENKINS_VALIDATE_ARGS[@]}"

echo "========================================================"
echo "Address validation PASSED"
echo "Reports: $REPO_DIR/results/"
echo "Database: $VALIDATION_DB"
echo "Dataset: $DATASET_PATH"
echo "========================================================"
