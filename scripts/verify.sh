#!/usr/bin/env bash
# Health check for a Morning Digest install. Read-only.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

ok()    { printf "  \033[32m✓\033[0m %s\n" "$*"; }
warn()  { printf "  \033[33m!\033[0m %s\n" "$*"; }
err()   { printf "  \033[31m✗\033[0m %s\n" "$*"; }
bold()  { printf "\033[1m%s\033[0m\n" "$*"; }

fail=0

bold "Local files"
[[ -f interests.md ]]    && ok "interests.md present"    || { err "interests.md missing — run scripts/setup.sh"; fail=1; }
[[ -f feedback-log.md ]] && ok "feedback-log.md present" || { err "feedback-log.md missing — run scripts/setup.sh"; fail=1; }
[[ -f sources.yml ]]     && ok "sources.yml present"     || { err "sources.yml missing"; fail=1; }

if ! command -v gh >/dev/null 2>&1; then
  err "gh CLI not installed — skipping remote checks"
  exit 1
fi
if ! gh auth status >/dev/null 2>&1; then
  err "gh not authenticated — run 'gh auth login'"
  exit 1
fi

REPO_SLUG="$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || true)"
[[ -z "$REPO_SLUG" ]] && { err "Not a GitHub repo"; exit 1; }

bold "GitHub secrets on $REPO_SLUG"
existing="$(gh secret list --repo "$REPO_SLUG" --json name -q '.[].name' 2>/dev/null || true)"
check_secret() {
  if grep -qx "$1" <<< "$existing"; then ok "$1 set"
  else warn "$1 not set"; [[ "${2:-}" == required ]] && fail=1
  fi
}
check_secret RESEND_API_KEY required
check_secret EMAIL_TO       required
check_secret EMAIL_FROM
check_secret FEEDBACK_URL
check_secret FEEDBACK_SECRET

bold "Workflow permissions"
perm="$(gh api "repos/$REPO_SLUG/actions/permissions/workflow" -q .default_workflow_permissions 2>/dev/null || true)"
if [[ "$perm" == "write" ]]; then ok "read+write (template sync OK)"
else warn "permissions=$perm — template auto-sync will fail to push"
fi

bold "Worker liveness"
# FEEDBACK_URL is a secret and GitHub never returns secret values, so we can't
# auto-discover it. Export MORNING_DIGEST_WORKER_URL=https://...workers.dev to probe.
probe_url="${MORNING_DIGEST_WORKER_URL:-}"
if [[ -n "$probe_url" ]]; then
  code="$(curl -sS -o /dev/null -w '%{http_code}' "$probe_url/click" --max-time 10 || echo 000)"
  case "$code" in
    401) ok "worker live ($probe_url → 401 without token, as expected)" ;;
    000) err "worker unreachable at $probe_url"; fail=1 ;;
    *)   warn "worker returned HTTP $code (expected 401)" ;;
  esac
else
  warn "set MORNING_DIGEST_WORKER_URL=https://...workers.dev to probe the worker"
fi

bold "Recent workflow runs"
gh run list --repo "$REPO_SLUG" --limit 5 --json workflowName,status,conclusion,createdAt \
  -q '.[] | "  \(.conclusion // .status)\t\(.workflowName)\t\(.createdAt)"' 2>/dev/null \
  | sed 's/^/  /' || warn "no runs yet"

bold "Eval baseline"
if [[ -f eval/baseline.json ]]; then ok "eval/baseline.json present"
else warn "no eval baseline — runs once the pipeline has data"
fi

printf "\n"
if [[ $fail -eq 0 ]]; then bold "Install looks healthy."
else err "Issues found — see above."; exit 1
fi
