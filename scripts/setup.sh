#!/usr/bin/env bash
# Interactive setup for Morning Digest. Idempotent — safe to re-run.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

bold()  { printf "\033[1m%s\033[0m\n" "$*"; }
ok()    { printf "  \033[32m✓\033[0m %s\n" "$*"; }
warn()  { printf "  \033[33m!\033[0m %s\n" "$*"; }
err()   { printf "  \033[31m✗\033[0m %s\n" "$*" >&2; }
ask()        { local p="$1" d="${2:-}" r; read -r -p "$p${d:+ [$d]}: " r; printf '%s' "${r:-$d}"; }
ask_secret() { local p="$1" r; read -rs -p "$p: " r; printf '\n' >&2; printf '%s' "$r"; }
yesno()      { local p="$1" d="${2:-n}" r; read -r -p "$p [$([[ $d == y ]] && echo Y/n || echo y/N)]: " r; r="${r:-$d}"; [[ "$r" =~ ^[Yy]$ ]]; }
set_gh_secret() { printf '%s' "$2" | gh secret set "$1" --body - --repo "$REPO_SLUG" >/dev/null; }

need() {
  local cmd="$1" hint="$2"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    err "$cmd not found. Install: $hint"
    return 1
  fi
  ok "$cmd"
}

bold "1/6  Checking prerequisites"
missing=0
need git "https://git-scm.com" || missing=1
need gh "https://cli.github.com" || missing=1
need node "https://nodejs.org" || missing=1
need npx "ships with node" || missing=1
[[ $missing -eq 1 ]] && { err "Install missing tools and re-run."; exit 1; }

if ! gh auth status >/dev/null 2>&1; then
  warn "GitHub CLI not authenticated. Launching 'gh auth login'..."
  gh auth login
fi
ok "gh authenticated"

REPO_SLUG="$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || true)"
if [[ -z "$REPO_SLUG" ]]; then
  err "Not in a GitHub repo, or 'gh repo view' failed. Push this repo to GitHub first."
  exit 1
fi
ok "repo: $REPO_SLUG"

bold "2/6  Seeding interests.md and feedback-log.md"
for f in interests feedback-log; do
  if [[ -f "$f.md" ]]; then
    ok "$f.md already exists"
  else
    cp "$f.example.md" "$f.md"
    ok "created $f.md from template"
  fi
done

if yesno "Open interests.md in \$EDITOR now to customize?" y; then
  "${EDITOR:-vi}" interests.md
fi

bold "3/6  Email delivery (Resend)"
if yesno "Set up email delivery?" y; then
  EMAIL_TO="$(ask "Your email address")"
  printf "Get a Resend API key at https://resend.com/api-keys\n"
  RESEND_KEY="$(ask_secret "Resend API key (starts with re_)")"
  EMAIL_FROM="$(ask "Email 'From' address" "Morning Digest <digest@resend.dev>")"
  set_gh_secret EMAIL_TO       "$EMAIL_TO"   && ok "set EMAIL_TO"
  set_gh_secret EMAIL_FROM     "$EMAIL_FROM" && ok "set EMAIL_FROM"
  set_gh_secret RESEND_API_KEY "$RESEND_KEY" && ok "set RESEND_API_KEY"
else
  warn "Skipping email. Digests will still commit to your repo."
fi

bold "4/6  One-click feedback (Cloudflare Worker)"
if yesno "Deploy the feedback Worker?" y; then
  if ! command -v wrangler >/dev/null 2>&1 && ! npx --no-install wrangler --version >/dev/null 2>&1; then
    warn "wrangler not found locally; npx will fetch it when needed."
  fi

  if ! (cd worker && npx --yes wrangler whoami >/dev/null 2>&1); then
    warn "Wrangler not logged in. Launching 'wrangler login'..."
    (cd worker && npx --yes wrangler login)
  fi
  ok "wrangler authenticated"

  FEEDBACK_SECRET="$(node -e 'console.log(require("crypto").randomBytes(32).toString("hex"))')"
  GH_TOKEN_FOR_WORKER="$(ask_secret "GitHub PAT with 'repo' write scope (https://github.com/settings/tokens)")"

  (
    cd worker
    npx --yes wrangler deploy | tee /tmp/morning-digest-deploy.log
    printf '%s' "$FEEDBACK_SECRET"     | npx --yes wrangler secret put FEEDBACK_SECRET
    printf '%s' "$GH_TOKEN_FOR_WORKER" | npx --yes wrangler secret put GITHUB_TOKEN
    printf '%s' "$REPO_SLUG"           | npx --yes wrangler secret put GITHUB_REPO
  )

  WORKER_URL="$(grep -Eo 'https://[a-zA-Z0-9.-]+\.workers\.dev' /tmp/morning-digest-deploy.log | tail -1 || true)"
  if [[ -z "$WORKER_URL" ]]; then
    WORKER_URL="$(ask "Could not auto-detect Worker URL. Paste it")"
  fi
  ok "worker URL: $WORKER_URL"

  set_gh_secret FEEDBACK_URL    "$WORKER_URL"      && ok "set FEEDBACK_URL"
  set_gh_secret FEEDBACK_SECRET "$FEEDBACK_SECRET" && ok "set FEEDBACK_SECRET"
else
  warn "Skipping Worker. Email will arrive without one-click feedback buttons."
fi

bold "5/6  Workflow permissions (needed for template auto-sync)"
if gh api -X PUT "repos/$REPO_SLUG/actions/permissions/workflow" \
     -f default_workflow_permissions=write \
     -F can_approve_pull_request_reviews=false >/dev/null 2>&1; then
  ok "workflow permissions set to read+write"
else
  warn "Could not set workflow permissions automatically. Toggle manually:"
  warn "  https://github.com/$REPO_SLUG/settings/actions"
fi

bold "6/6  Smoke-test"
warn "interests.md and feedback-log.md stay local (.gitignored by design)."

if yesno "Trigger the email-digest workflow now as a smoke test?" n; then
  if [[ -d daily ]] && ls daily/*.md >/dev/null 2>&1; then
    gh workflow run email-digest.yml --repo "$REPO_SLUG" || warn "workflow_dispatch may not be wired; that's fine."
    ok "smoke test dispatched. Watch: gh run watch --repo $REPO_SLUG"
  else
    warn "No digest in daily/ yet — smoke test deferred until first agent run."
  fi
fi

printf "\n"
bold "Setup complete."
cat <<EOF

Next: schedule the Claude Code agent.

  1. Install the Claude GitHub App:
       https://claude.ai/code/onboarding?magic=github-app-setup

  2. In Claude Code, run:  /schedule

  3. Paste this prompt:

       Create a scheduled trigger called "morning-digest" that runs daily at 7am my time.
       Repo: https://github.com/$REPO_SLUG
       Tools needed: Bash, Read, Write, Edit, Glob, Grep, WebSearch
       Use the prompt from .github/prompts/digest-agent.txt

Verify health anytime:  scripts/verify.sh
EOF
