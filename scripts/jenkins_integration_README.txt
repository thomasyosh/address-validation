How to add Address Search Validation to Jenkins job
====================================================

Your colleague's job (deploy-uat-opensearch-data-bldg-street) currently runs a shell
script that uploads building/street data to OpenSearch UAT via Docker.

Add address validation as a NEW block at the END of that same "Execute shell" step,
OR as a second "Execute shell" build step.

You do NOT upload folders through the Jenkins Workspace / Changes tabs.
Jenkins runs commands; git clone downloads your app automatically.


STEP 1 — One-time setup on the Jenkins server (ask colleague / admin)
---------------------------------------------------------------------
1. Create a persistent folder (survives workspace wipe between builds):
     /var/jenkins/address-validation/data/

2. Copy the test Excel file there:
     /var/jenkins/address-validation/data/addresses.xlsx

3. Clone the repo once and create config.yaml (use your team's Git URL):
     export ADDRESS_VALIDATION_GIT_URL='<your-internal-git-url>'
     git clone "$ADDRESS_VALIDATION_GIT_URL" /var/jenkins/address-validation/repo
     cd /var/jenkins/address-validation/repo
     cp config.example.yaml config.yaml
     # Edit config.yaml: ASE URL, host_header, etc. (same as your PC)

4. Ensure Python 3.10+ and git are installed on the Jenkins agent.


STEP 2 — Edit the Jenkins job (GUI)
-----------------------------------
1. Open Jenkins → folder PROD → job deploy-uat-opensearch-data-bldg-street
2. Click "Configure" (left menu)
3. Scroll to "Build" or "Build Steps"
4. Find "Execute shell" (the script in jenkins.txt)
5. AFTER the existing docker/opensearch block, append the contents of:
     scripts/jenkins_post_deploy.sh
   Or add a second "Execute shell" step with that script.

6. Save.


STEP 3 — What happens on each build (#45, #46, ...)
--------------------------------------------------
1. Existing step: upload data to OpenSearch UAT (unchanged)
2. New step: git pull address-validation repo
3. Run: python main.py validate --compare-with-previous --max-rate-delta 1
4. If English or Chinese match-rate moved by >= 1 percentage point → build FAILS (red X)
5. If OK → build PASS (green tick)


STEP 4 — Where to look when a build fails
-----------------------------------------
- Build #N → "Console Output" → search for "Address Search Validation"
- Reports on agent: /var/jenkins/address-validation/repo/results/
- SQLite history: /var/jenkins/address-validation/address_validation.db


Git repo (for your colleague)
-----------------------------
Use your organisation's internal Git URL. Set it once on the Jenkins agent:

  export ADDRESS_VALIDATION_GIT_URL='<your-internal-git-url>'

The post-deploy script reads ADDRESS_VALIDATION_GIT_URL on first clone.
After that, git pull uses the remote already configured in $VALIDATION_REPO.
