# Optional Jenkins CI files (commit to private GitLab only)
#
# jenkins/config.yaml      — ASE settings for CI (copied to JENKINS_HOME on first run)
# jenkins/addresses.xlsx   — test dataset (large; optional if you use ADDRESSES_XLSX_URL instead)
#
# If these files are not in Git, the Execute shell script will:
#   - use config.example.yaml for config on first run
#   - require ADDRESSES_XLSX_URL or ADDRESSES_XLSX_SOURCE for the Excel file
