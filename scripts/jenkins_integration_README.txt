Jenkins deployment
==================

RECOMMENDED — Docker (scripts/jenkins_execute_shell_docker.sh)
--------------------------------------------------------------
  Avoids python3-pip / venv issues on the Jenkins agent. Same pattern as other
  Docker jobs on your server.

  1. Git SCM → GitLab, branch */main
  2. Build → Execute shell → paste scripts/jenkins_execute_shell_docker.sh
  3. Optional File Parameters (one-time): address_xlsx, config_yaml, config_local_yaml
  4. Build with Parameters once to upload files; later builds reuse /tmp/.../data
  5. Post-build: Archive artifacts → results/**

  Requires: docker command on Jenkins agent (ask colleague).


LEGACY — Shell on agent (scripts/jenkins_execute_shell_scm_only.sh)
-------------------------------------------------------------------
  Use only if Docker is not available on the agent.


Jenkins — SCM + File Upload setup (shell / Docker bootstrap)
============================================================





STEP 1 — Push repo to GitLab

----------------------------

  git push your repo to company GitLab (branch main)





STEP 2 — Jenkins job: File Parameters (upload button)

-----------------------------------------------------

  Job → Configure → General → check "This project is parameterized"



  Add Parameter → File Parameter (three times):



  | Parameter Name     | File Location | You upload from PC   |
  |--------------------|---------------|----------------------|
  | address_xlsx       | (leave blank) | data/address.xlsx    |
  | config_yaml        | (leave blank) | config.yaml          |
  | config_local_yaml  | (leave blank) | config.local.yaml    |

  File Location = path under $WORKSPACE where Jenkins saves the upload.
  Leave it BLANK for all three — Jenkins saves as address_xlsx, config_yaml,
  config_local_yaml in the workspace root (our script finds these).

  Optional — use real paths instead of blank File Location:
    address_xlsx      → data/address.xlsx
    config_yaml       → config.yaml
    config_local_yaml → config.local.yaml



  Save the job.



  IMPORTANT: Use "Build with Parameters" (not plain "Build Now") so the

  upload buttons appear. Jenkins saves uploaded files in Workspace as:

    $WORKSPACE/address_xlsx

    $WORKSPACE/config_yaml

    $WORKSPACE/config_local_yaml

  (Jenkins uses the parameter name, not your original filename.)


ONE-TIME UPLOAD — colleagues do NOT re-upload every build
---------------------------------------------------------
  The script COPIES uploads to a persistent folder on the Jenkins agent:
    /tmp/address-validation-data-<jobname>/
      config.yaml
      config.local.yaml
      data/address.xlsx
      address_validation.db

  Build #1: upload all 3 files (Build with Parameters).
  Build #2+: script reuses saved copies — no new upload required.

  Annoying Jenkins UI? After the first successful build you can:
    A) Remove the 3 File Parameters from the job (recommended for colleagues)
       → plain "Build Now" works; files already on the agent.
    B) Keep parameters but colleagues click Build with Parameters and leave
       uploads empty / skip — saved files are still used if present.
    C) Avoid uploads entirely:
       - config: commit jenkins/config.yaml to private GitLab
       - proxy: commit jenkins/config.local.yaml OR set proxy in Execute shell
       - Excel: export ADDRESSES_XLSX_URL='https://internal/address.xlsx'

  Re-upload only when config or dataset changes.

STEP 3 — Jenkins job: Git SCM

-----------------------------

  Source Code Management → Git

    Repository URL: <your-gitlab-clone-url>

    Credentials: GitLab token or SSH key

    Branch Specifier: */main



  Do NOT enable "Checkout to subdirectory: address-validation".





STEP 4 — Jenkins job: Execute shell

-----------------------------------

  Build → Execute shell

  Paste the ENTIRE file: scripts/jenkins_execute_shell_scm_only.sh

  (First line must be #!/bin/bash)



  Do NOT add extra mkdir or clone commands from other jobs.

  Do NOT set ADDRESS_VALIDATION_HOME=/var/jenkins anywhere.





STEP 5 — Build with Parameters

------------------------------

  1. Click "Build with Parameters"

  2. Upload address_xlsx  → choose your data/address.xlsx

  3. Upload config_yaml   → choose your config.yaml

  4. Upload config_local_yaml → choose your config.local.yaml (proxy)

  5. Click Build



  First build: creates baseline in /tmp/address-validation-data-<jobname>/

  Later builds: re-upload only when files change; otherwise prior copies are reused.





Console output — success looks like

-----------------------------------

  Address Search Validation (jenkins_post_deploy.sh 2026-07-14c)

  Persist directory OK: /tmp/address-validation-data-your-job

  Installed config.yaml from upload: .../config_yaml

  Installed config.local.yaml from upload: .../config_local_yaml

  Installed dataset from: .../address_xlsx

  Starting validate ...





Where files live

----------------

  Workspace (git checkout):  $WORKSPACE/main.py, scripts/, results/

  Uploaded copies (persist):   /tmp/address-validation-data-<jobname>/

    config.yaml

    config.local.yaml

    data/address.xlsx

    address_validation.db





Troubleshooting

---------------



  mkdir: cannot create directory 'var/jenkins' or '/var/lib/jenkins'

    → Often pip install using HOME=/var/lib/jenkins (fixed in 2026-07-14d via /tmp + venv).

    → Or extra shell lines copied from another job (mkdir ...).

    → Remove global/job env ADDRESS_VALIDATION_HOME if set.

    → Execute shell = ONLY jenkins_execute_shell_scm_only.sh (no other lines).

    → Console must show "2026-07-14d" and "HOME redirected to: /tmp/jenkins-home-...".

    → Push latest scripts to GitLab and rebuild.



  ERROR: Set ADDRESS_VALIDATION_GIT_URL ... clone manually to .../repo

    → OLD Execute shell still in the job (outdated jenkins_post_deploy pasted inline).

    → Delete ALL Execute shell text. Paste ONLY jenkins_execute_shell_scm_only.sh.

    → Edit GIT_URL at the top of that file if Git SCM is not configured.



  Couldn't find any revision to build

    → GitLab empty or wrong branch. Push main, set Branch */main, fix credentials.

    → Or edit GIT_URL in jenkins_execute_shell_scm_only.sh (fallback git clone).



  Missing address.xlsx

    → Use "Build with Parameters" and upload address_xlsx.

    → Parameter name must be exactly: address_xlsx



  Missing config

    → Upload config_yaml parameter, or commit jenkins/config.yaml to GitLab.



  /usr/bin/python3: No module named pip

    → Agent has python3 but not python3-pip (fixed in 2026-07-14f via get-pip.py download).

    → Push to GitLab; console should show "Downloading get-pip.py" then "venv + get-pip.py".

    → Jenkins agent needs HTTPS to bootstrap.pypa.io and pypi.org (via smoproxy).

    → If download blocked, ask admin: yum install python3-pip python3-venv



  Double address-validation folder

    → Remove Git "Checkout to subdirectory". Use SCM-only execute shell.

