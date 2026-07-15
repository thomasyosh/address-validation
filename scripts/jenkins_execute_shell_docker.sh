#!/bin/bash
# =============================================================================
# Jenkins Execute shell — DOCKER
# Paste ENTIRE file into Jenkins → Build → Execute shell.
# =============================================================================

set -eu

IMAGE_NAME="${DOCKER_IMAGE_NAME:-address-validation:ci}"

echo "=== Address Search Validation (Docker) ==="
echo "WORKSPACE=${WORKSPACE:-?} JOB_NAME=${JOB_NAME:-?} BUILD_NUMBER=${BUILD_NUMBER:-?}"

export http_proxy="${http_proxy:-http://smoproxy:8080/}"
export https_proxy="${https_proxy:-http://smoproxy:8080/}"
export HTTP_PROXY="${HTTP_PROXY:-$http_proxy}"
export HTTPS_PROXY="${HTTPS_PROXY:-$https_proxy}"
export NO_PROXY="${NO_PROXY:-ase.testingaddress.com,10.77.242.157,10.0.0.0/8,localhost,127.0.0.1}"

# Persist INSIDE workspace — Docker on Jenkins agents often cannot mount /tmp reliably.
PERSIST="${WORKSPACE}/.address-validation-data"
OLD_PERSIST="/tmp/address-validation-data-${JOB_NAME:-jenkins}"
export ADDRESS_VALIDATION_HOME="$PERSIST"
mkdir -p "$PERSIST" "${WORKSPACE}/results"

# One-time migration from older /tmp persist layout
if [ ! -f "${PERSIST}/config.yaml" ] && [ -d "$OLD_PERSIST" ]; then
  echo "Migrating data from $OLD_PERSIST to $PERSIST ..."
  cp -a "${OLD_PERSIST}/." "${PERSIST}/" 2>/dev/null || true
fi

source "${WORKSPACE}/scripts/jenkins_validate_args.sh"
echo "ASE fetch profile: batch_size=${ASE_BATCH_SIZE} concurrency=single-thread rps=${ASE_RPS}"
echo "Persist (host) = ${PERSIST}"
echo "Persist (container) = /data"

if [ ! -f "${WORKSPACE}/Dockerfile" ]; then
  echo "ERROR: ${WORKSPACE}/Dockerfile not found — Git SCM did not checkout the repo."
  exit 1
fi

bash "${WORKSPACE}/scripts/jenkins_docker_bootstrap.sh"

if [ ! -f "${PERSIST}/config.yaml" ] || [ ! -f "${PERSIST}/address.xlsx" ]; then
  echo "ERROR: Bootstrap did not populate ${PERSIST}"
  exit 1
fi

echo "Pre-docker persist check OK (host):"
ls -la "${PERSIST}/"

echo "Building Docker image: $IMAGE_NAME ..."
docker build \
  --build-arg "HTTP_PROXY=${HTTP_PROXY}" \
  --build-arg "HTTPS_PROXY=${HTTPS_PROXY}" \
  --build-arg "NO_PROXY=${NO_PROXY}" \
  -t "$IMAGE_NAME" \
  "${WORKSPACE}"

# No :z by default (commit 280acd82 worked without it). Set DOCKER_VOL_OPTS=:z if SELinux requires it.
VOL_OPTS="${DOCKER_VOL_OPTS:-}"

echo "Verifying Docker can read /data mount ..."
docker run --rm \
  --entrypoint /bin/sh \
  -v "${PERSIST}:/data${VOL_OPTS}" \
  "$IMAGE_NAME" \
  -c 'ls -la /data && test -f /data/config.yaml && test -f /data/address.xlsx'

echo "Running validation in container ..."
docker run --rm \
  -e "http_proxy=${http_proxy}" \
  -e "https_proxy=${https_proxy}" \
  -e "HTTP_PROXY=${HTTP_PROXY}" \
  -e "HTTPS_PROXY=${HTTPS_PROXY}" \
  -e "NO_PROXY=${NO_PROXY}" \
  -e "PYTHONUNBUFFERED=1" \
  -v "${PERSIST}:/data${VOL_OPTS}" \
  -v "${WORKSPACE}/results:/app/results${VOL_OPTS}" \
  "$IMAGE_NAME" \
  "${JENKINS_VALIDATE_ARGS[@]}"

echo "========================================================"
echo "Address validation PASSED (Docker)"
echo "Reports: ${WORKSPACE}/results/"
echo "Database: ${PERSIST}/address_validation.db"
echo "========================================================"
