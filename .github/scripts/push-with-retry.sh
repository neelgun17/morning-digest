#!/usr/bin/env bash
# Pull-then-push against a branch several workflows write to concurrently.
#
# Four workflows (pipeline, train, eval's refresh-baseline, judge) plus the
# Cloudflare Worker all write to main. A GitHub Actions `concurrency` group
# serializes the workflow pushes, but the Worker writes via the Contents API
# outside that group, so a run can still find the remote moved underneath it.
#
# The rebase is aborted explicitly on failure rather than swallowed with
# `|| true` — that older form left the repo mid-rebase and let the push fail
# anyway, reporting a confusing error instead of the real one.
#
# Usage: push-with-retry.sh <branch> [max_attempts]
set -uo pipefail

branch="${1:?usage: push-with-retry.sh <branch> [max_attempts]}"
max_attempts="${2:-3}"

# Guard against reporting success without pushing: a non-numeric max_attempts
# would make `seq` fail, skip the loop body entirely, and fall off the end.
case "$max_attempts" in
  ''|*[!0-9]*|0) echo "::error::max_attempts must be a positive integer, got '${max_attempts}'"; exit 1 ;;
esac

for attempt in $(seq 1 "$max_attempts"); do
  if git pull --rebase origin "$branch" && git push; then
    exit 0
  fi
  git rebase --abort >/dev/null 2>&1 || true
  if [ "$attempt" -eq "$max_attempts" ]; then
    echo "::error::push to ${branch} failed after ${max_attempts} attempts (rebase conflict or remote race) — aborting"
    exit 1
  fi
  echo "::warning::push attempt ${attempt}/${max_attempts} to ${branch} failed, retrying..."
  sleep $((attempt * 3))
done

echo "::error::push loop exited without pushing — this should be unreachable"
exit 1
