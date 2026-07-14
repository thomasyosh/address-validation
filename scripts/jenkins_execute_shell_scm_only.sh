#!/bin/bash
# =============================================================================
# Paste this ENTIRE file into Jenkins → Build → Execute shell.
# Do NOT paste jenkins_post_deploy.sh here — only this file.
# Do NOT add lines from other jobs (mkdir, clone to /tmp/.../repo, etc.).
# =============================================================================

set -eu

# ============ EDIT: your GitLab clone URL (used if Git SCM checkout is empty) ============
GIT_URL="${ADDRESS_VALIDATION_GIT_URL:-<your-gitlab-clone-url>}"
# ========================================================================================

echo "=== Address Search Validation (SCM-only 2026-07-14e) ==="
echo "WORKSPACE=${WORKSPACE:-?} JOB_NAME=${JOB_NAME:-?}"

unset ADDRESS_VALIDATION_HOME
export ADDRESS_VALIDATION_HOME="/tmp/address-validation-data-${JOB_NAME:-jenkins}"
export HOME="/tmp/jenkins-home-${JOB_NAME:-jenkins}"
export PIP_CACHE_DIR="/tmp/pip-cache-${JOB_NAME:-jenkins}"
export TMPDIR="/tmp"
export ADDRESS_VALIDATION_GIT_URL="$GIT_URL"

mkdir -p "${ADDRESS_VALIDATION_HOME}/data" "$HOME" "$PIP_CACHE_DIR"
echo "Persist OK: ${ADDRESS_VALIDATION_HOME}"

export http_proxy="${http_proxy:-http://smoproxy:8080/}"
export https_proxy="${https_proxy:-http://smoproxy:8080/}"
export NO_PROXY="${NO_PROXY:-ase.testingaddress.com,10.77.242.157,10.0.0.0/8,localhost,127.0.0.1}"

_ensure_repo_in_workspace() {
  if [ -f "${WORKSPACE}/scripts/jenkins_post_deploy.sh" ]; then
    if grep -q '2026-07-14e' "${WORKSPACE}/scripts/jenkins_post_deploy.sh" 2>/dev/null \
        || grep -q '2026-07-14d' "${WORKSPACE}/scripts/jenkins_post_deploy.sh" 2>/dev/null; then
      echo "Git SCM checkout OK (scripts/jenkins_post_deploy.sh found)."
      return 0
    fi
    echo "WARNING: Old jenkins_post_deploy.sh in workspace — will re-fetch from GitLab."
  fi

  if [ -z "$GIT_URL" ] || [ "$GIT_URL" = "<your-gitlab-clone-url>" ]; then
    echo "ERROR: Git SCM did not checkout the repo AND GIT_URL is not set."
    echo "  Fix ONE of:"
    echo "    1) Jenkins → Source Code Management → Git (URL + credentials + branch */main)"
    echo "    2) Edit GIT_URL at the top of this Execute shell"
    echo ""
    echo "  If you see 'clone manually to .../repo' — you are running an OLD script."
    echo "  Delete ALL Execute shell content and paste ONLY this file."
    exit 1
  fi

  echo "Cloning repo into workspace from $GIT_URL ..."
  rm -rf "${WORKSPACE}/_av_clone"
  git clone --depth 1 "$GIT_URL" "${WORKSPACE}/_av_clone"
  shopt -s dotglob nullglob
  cp -a "${WORKSPACE}/_av_clone"/. "${WORKSPACE}/"
  rm -rf "${WORKSPACE}/_av_clone"
  shopt -u dotglob nullglob

  if [ ! -f "${WORKSPACE}/scripts/jenkins_post_deploy.sh" ]; then
    echo "ERROR: Clone succeeded but scripts/jenkins_post_deploy.sh still missing."
    exit 1
  fi
  echo "Clone into workspace OK."
}

_ensure_repo_in_workspace
bash "${WORKSPACE}/scripts/jenkins_post_deploy.sh"
