#!/bin/bash
# =============================================================================
# COMPLETE Jenkins Execute shell — paste this entire block into your job.
# Works with: Source Code Management → Git (recommended, like your colleague's job)
#
# Prerequisites in Jenkins job:
#   - Git SCM → your GitLab URL + credentials
#   - Branch */main
#   - Do NOT set ADDRESS_VALIDATION_HOME=/var/jenkins (use default or $HOME/...)
#   - Do NOT use "Checkout to subdirectory: address-validation" (causes double nesting)
#
# Optional (pick one for addresses.xlsx if not in Git):
#   export ADDRESSES_XLSX_URL='https://your-internal-server/addresses.xlsx'
# =============================================================================

set -eu

export http_proxy="${http_proxy:-http://smoproxy:8080/}"
export https_proxy="${https_proxy:-http://smoproxy:8080/}"
export NO_PROXY="${NO_PROXY:-ase.testingaddress.com,10.77.242.157,10.0.0.0/8,localhost,127.0.0.1}"

# GitLab URL used only if Git SCM is NOT configured (fallback clone)
export ADDRESS_VALIDATION_GIT_URL="${ADDRESS_VALIDATION_GIT_URL:-}"

echo "=== Address Search Validation ==="
echo "WORKSPACE=${WORKSPACE:-?} HOME=${HOME:-?} JENKINS_HOME=${JENKINS_HOME:-?} BUILD_NUMBER=${BUILD_NUMBER:-?}"

_find_post_deploy_script() {
  for candidate in \
    "${WORKSPACE}/scripts/jenkins_post_deploy.sh" \
    "${WORKSPACE}/address-validation/scripts/jenkins_post_deploy.sh" \
    "${WORKSPACE}/address-validation/address-validation/scripts/jenkins_post_deploy.sh"; do
    if [ -f "$candidate" ]; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

if POST_DEPLOY="$(_find_post_deploy_script)"; then
  echo "Git SCM checkout detected at $(dirname "$(dirname "$POST_DEPLOY")")"
  bash "$POST_DEPLOY"
  exit $?
fi

# Fallback: no Git SCM — clone into workspace root (not a nested subfolder)
if [ -z "$ADDRESS_VALIDATION_GIT_URL" ] || [ "$ADDRESS_VALIDATION_GIT_URL" = "<your-gitlab-clone-url>" ]; then
  echo "ERROR: Configure Git SCM in the job, or set ADDRESS_VALIDATION_GIT_URL"
  exit 1
fi

if [ -f "${WORKSPACE}/main.py" ]; then
  bash "${WORKSPACE}/scripts/jenkins_post_deploy.sh"
  exit $?
fi

if [ -d "${WORKSPACE}/.git" ]; then
  git -C "${WORKSPACE}" pull --ff-only || true
  bash "${WORKSPACE}/scripts/jenkins_post_deploy.sh"
  exit $?
fi

echo "Cloning into workspace root ${WORKSPACE} ..."
git clone "$ADDRESS_VALIDATION_GIT_URL" "${WORKSPACE}/_clone_tmp"
shopt -s dotglob nullglob
mv "${WORKSPACE}/_clone_tmp"/* "${WORKSPACE}/"
rmdir "${WORKSPACE}/_clone_tmp"
shopt -u dotglob nullglob
bash "${WORKSPACE}/scripts/jenkins_post_deploy.sh"
