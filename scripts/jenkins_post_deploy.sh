#!/usr/bin/env bash
# Append this block AFTER the OpenSearch upload step in Jenkins "Execute shell".
# See scripts/jenkins_integration_README.txt for GUI steps and prerequisites.

set -euo pipefail

echo "========================================================"
echo "Address Search Validation (after OpenSearch data deploy)"
echo "========================================================"

# Persistent folder OUTSIDE $WORKSPACE so compare-with-previous works across builds.
# Ask Jenkins admin to create this path once, or change to a shared mount you both use.
PERSIST_DIR="${ADDRESS_VALIDATION_HOME:-/var/jenkins/address-validation}"
VALIDATION_REPO="$PERSIST_DIR/repo"
VALIDATION_DATA="$PERSIST_DIR/data"
VALIDATION_DB="$PERSIST_DIR/address_validation.db"

mkdir -p "$VALIDATION_DATA"

GIT_REPO_URL="${ADDRESS_VALIDATION_GIT_URL:-}"
if [ ! -d "$VALIDATION_REPO/.git" ]; then
    if [ -z "$GIT_REPO_URL" ]; then
        echo "ERROR: Set ADDRESS_VALIDATION_GIT_URL to your internal Git clone URL,"
        echo "or clone this repository manually to: $VALIDATION_REPO"
        exit 1
    fi
    git clone "$GIT_REPO_URL" "$VALIDATION_REPO"
else
    git -C "$VALIDATION_REPO" pull --ff-only
fi

cd "$VALIDATION_REPO"
python3 -m pip install -r requirements.txt -q

# config.yaml and addresses.xlsx are gitignored — must exist on the Jenkins server.
if [ ! -f config.yaml ]; then
    echo "ERROR: Missing $VALIDATION_REPO/config.yaml"
    echo "Copy config.example.yaml to config.yaml on the Jenkins agent and edit ASE settings."
    exit 1
fi
if [ ! -f "$VALIDATION_DATA/addresses.xlsx" ] && [ ! -f data/addresses.xlsx ]; then
    echo "ERROR: Missing test dataset. Place addresses.xlsx at:"
    echo "  $VALIDATION_DATA/addresses.xlsx"
    echo "or symlink/copy into $VALIDATION_REPO/data/addresses.xlsx"
    exit 1
fi

# Use persistent DB + dataset (override via config.local.yaml if you prefer).
cat > config.local.yaml <<EOF
database:
  path: $VALIDATION_DB
dataset:
  path: $VALIDATION_DATA/addresses.xlsx
EOF

# Match proxy style used in the OpenSearch deploy step above.
export http_proxy="${http_proxy:-http://smoproxy:8080/}"
export https_proxy="${https_proxy:-http://smoproxy:8080/}"
export NO_PROXY="${NO_PROXY:-ase.testingaddress.com,10.77.242.157,10.0.0.0/8,localhost,127.0.0.1}"

python3 main.py validate \
    --compare-with-previous \
    --max-rate-delta 1 \
    --label "jenkins-build-${BUILD_NUMBER:-manual}"

echo "========================================================"
echo "Address validation PASSED"
echo "Reports: $VALIDATION_REPO/results/"
echo "========================================================"
