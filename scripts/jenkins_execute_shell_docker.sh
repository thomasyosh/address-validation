#!/bin/bash
# =============================================================================
# Jenkins Execute shell — DOCKER (recommended when agent has docker, like other jobs)
#
# Prerequisites:
#   - Source Code Management → Git (GitLab URL, branch */main)
#   - Jenkins agent: docker command available to build user
#   - Optional File Parameters: address_xlsx, config_yaml, config_local_yaml
#     (one-time upload; files saved under /tmp/address-validation-data-<job>/)
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

PERSIST="/tmp/address-validation-data-${JOB_NAME:-jenkins}"
export ADDRESS_VALIDATION_HOME="$PERSIST"
mkdir -p "$PERSIST" "${WORKSPACE}/results"

# Gentle ASE profile: batch requests, single client thread (see scripts/jenkins_validate_args.sh)
source "${WORKSPACE}/scripts/jenkins_validate_args.sh"
echo "ASE fetch profile: batch_size=${ASE_BATCH_SIZE} concurrency=single-thread rps=${ASE_RPS}"
echo "SQLite DB (grows during fetch): ${PERSIST}/address_validation.db"

if [ ! -f "${WORKSPACE}/Dockerfile" ]; then
  echo "ERROR: ${WORKSPACE}/Dockerfile not found — Git SCM did not checkout the repo."
  exit 1
fi

bash "${WORKSPACE}/scripts/jenkins_docker_bootstrap.sh"

if [ ! -f "${PERSIST}/config.yaml" ] || [ ! -f "${PERSIST}/address.xlsx" ]; then
  echo "ERROR: Bootstrap did not populate ${PERSIST} — see messages above."
  exit 1
fi

echo "Pre-docker persist check OK:"
ls -la "${PERSIST}/"

echo "Building Docker image: $IMAGE_NAME ..."
docker build \
  --build-arg "HTTP_PROXY=${HTTP_PROXY}" \
  --build-arg "HTTPS_PROXY=${HTTPS_PROXY}" \
  --build-arg "NO_PROXY=${NO_PROXY}" \
  -t "$IMAGE_NAME" \
  "${WORKSPACE}"

# :z helps SELinux agents (RHEL) mount host dirs into Docker
DOCKER_VOL_OPTS="${DOCKER_VOL_OPTS:-:z}"

echo "Running validation in container ..."
docker run --rm \
  -e "http_proxy=${http_proxy}" \
  -e "https_proxy=${https_proxy}" \
  -e "HTTP_PROXY=${HTTP_PROXY}" \
  -e "HTTPS_PROXY=${HTTPS_PROXY}" \
  -e "NO_PROXY=${NO_PROXY}" \
  -e "PYTHONUNBUFFERED=1" \
  -v "${PERSIST}:/data${DOCKER_VOL_OPTS}" \
  -v "${WORKSPACE}/results:/app/results${DOCKER_VOL_OPTS}" \
  "$IMAGE_NAME" \
  "${JENKINS_VALIDATE_ARGS[@]}"

echo "========================================================"
echo "Address validation PASSED (Docker)"
echo "Reports: ${WORKSPACE}/results/"
echo "Database: ${PERSIST}/address_validation.db"
echo "========================================================"
