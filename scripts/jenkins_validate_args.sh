#!/bin/bash
# Shared Jenkins validate flags — gentle on ASE query_debug (~8GB RAM).
# batch + single-thread: one HTTP request at a time, many addresses per array body.
export ASE_BATCH_SIZE="${ASE_BATCH_SIZE:-25}"
export ASE_RPS="${ASE_RPS:-2}"

JENKINS_VALIDATE_ARGS=(
  validate
  --fetch-mode batch
  --concurrency single-thread
  --batch-size "${ASE_BATCH_SIZE}"
  --no-auto-parallel-batches
  --sequential
  --rps "${ASE_RPS}"
  --compare-with-previous
  --max-rate-delta 1
  --label "jenkins-build-${BUILD_NUMBER:-manual}"
)
