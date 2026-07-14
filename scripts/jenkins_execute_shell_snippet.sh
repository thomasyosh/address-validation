#!/bin/bash
# Paste this ENTIRE file into Jenkins → Build → Execute shell.
# Line 1 MUST be #!/bin/bash (Jenkins uses sh by default; sh does not support pipefail).
#
# Recommended: use Git SCM + scripts/jenkins_execute_shell_full.sh instead of this file.
# If you use this snippet WITH Git SCM, do not clone again — the script detects checkout.

set -eu

export ADDRESS_VALIDATION_GIT_URL='<your-gitlab-clone-url>'
export http_proxy="${http_proxy:-http://smoproxy:8080/}"
export https_proxy="${https_proxy:-http://smoproxy:8080/}"
export NO_PROXY="${NO_PROXY:-ase.testingaddress.com,10.77.242.157,10.0.0.0/8,localhost,127.0.0.1}"

echo "=== Address validation bootstrap ==="
echo "WORKSPACE=${WORKSPACE:-<not set>} HOME=${HOME:-<not set>} JENKINS_HOME=${JENKINS_HOME:-<not set>}"
echo "BUILD_NUMBER=${BUILD_NUMBER:-<not set>}"

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
  echo "Git SCM checkout detected — running validation script..."
  bash "$POST_DEPLOY"
  exit $?
fi

if [ "${ADDRESS_VALIDATION_GIT_URL}" = "<your-gitlab-clone-url>" ]; then
  echo "ERROR: Configure Git SCM, or edit this script and set ADDRESS_VALIDATION_GIT_URL."
  exit 1
fi

echo "No Git SCM checkout found — cloning into workspace root..."
git clone "$ADDRESS_VALIDATION_GIT_URL" "${WORKSPACE}/_clone_tmp"
shopt -s dotglob nullglob
mv "${WORKSPACE}/_clone_tmp"/* "${WORKSPACE}/"
rmdir "${WORKSPACE}/_clone_tmp"
shopt -u dotglob nullglob

bash "${WORKSPACE}/scripts/jenkins_post_deploy.sh"
