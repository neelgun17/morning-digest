#!/bin/bash

# Morning Digest — Setup Script
# Creates your private repo with everything you need.

set -e

echo ""
echo "========================================"
echo "  Morning Digest — Setup"
echo "========================================"
echo ""

# Check dependencies
if ! command -v git &> /dev/null; then
    echo "Error: git is not installed. Install it first: https://git-scm.com"
    exit 1
fi

if ! command -v gh &> /dev/null; then
    echo "Error: GitHub CLI (gh) is not installed. Install it first: https://cli.github.com"
    exit 1
fi

if ! gh auth status &> /dev/null 2>&1; then
    echo "Error: Not logged into GitHub CLI. Run: gh auth login"
    exit 1
fi

# Get repo name
echo "What do you want to name your private repo? (default: my-morning-digest)"
read -r REPO_NAME
REPO_NAME=${REPO_NAME:-my-morning-digest}

if [ -d "../$REPO_NAME" ]; then
    echo "Error: Directory ../$REPO_NAME already exists. Choose a different name or remove it."
    exit 1
fi

echo ""
echo "Creating '$REPO_NAME'..."
echo ""

# Create the private repo directory with all files
mkdir -p "../$REPO_NAME/daily"
mkdir -p "../$REPO_NAME/.github/workflows"
mkdir -p "../$REPO_NAME/.github/scripts"
mkdir -p "../$REPO_NAME/.github/prompts"
mkdir -p "../$REPO_NAME/worker"

# Copy templates (rename to active files)
cp interests.template.md "../$REPO_NAME/interests.md"
cp feedback-log.template.md "../$REPO_NAME/feedback-log.md"
cp sources.template.yml "../$REPO_NAME/sources.yml"

# Copy infrastructure files
cp .github/workflows/email-digest.yml "../$REPO_NAME/.github/workflows/email-digest.yml"
cp .github/scripts/send-digest.js "../$REPO_NAME/.github/scripts/send-digest.js"
cp .github/prompts/digest-agent.txt "../$REPO_NAME/.github/prompts/digest-agent.txt"
cp worker/index.js "../$REPO_NAME/worker/index.js"
cp worker/wrangler.toml "../$REPO_NAME/worker/wrangler.toml"

# Initialize git and create private repo
cd "../$REPO_NAME"
git init -b main
git add -A
git commit -m "Initial setup from morning-digest template"
gh repo create "$REPO_NAME" --private --source=. --push

REPO_URL=$(gh repo view --json url -q '.url')
GH_USERNAME=$(gh api user -q '.login')

# Update wrangler.toml with actual repo name
sed -i.bak "s|YOUR_GITHUB_USERNAME/YOUR_REPO_NAME|$GH_USERNAME/$REPO_NAME|" worker/wrangler.toml
rm -f worker/wrangler.toml.bak
git add worker/wrangler.toml
git commit -m "Set repo name in worker config" --allow-empty
git push

echo ""
echo "========================================"
echo "  Setup Complete!"
echo "========================================"
echo ""
echo "Private repo: $REPO_URL"
echo "Local path:   $(pwd)"
echo ""
echo "Next steps:"
echo ""
echo "  1. EDIT YOUR INTERESTS"
echo "     Open interests.md and replace the examples with yours."
echo "     Also customize sources.yml with RSS feeds you care about."
echo "     Commit and push when done:"
echo "       git add interests.md sources.yml && git commit -m 'Set up interests' && git push"
echo ""
echo "  2. CONNECT GITHUB TO CLAUDE CODE"
echo "     The agent needs access to your private repo."
echo "     Go to: https://claude.ai/code/onboarding?magic=github-app-setup"
echo "     Install the Claude GitHub App and grant access to '$REPO_NAME'."
echo ""
echo "  3. SCHEDULE THE AGENT"
echo "     Open Claude Code in this directory and run:"
echo ""
echo "       /schedule"
echo ""
echo "     Then tell it:"
echo ""
echo "       Create a scheduled trigger called \"morning-digest\" that runs daily"
echo "       at 7am my time. Use my repo $REPO_URL with tools: Bash, Read, Write,"
echo "       Edit, Glob, Grep, WebSearch. Use the prompt from .github/prompts/digest-agent.txt"
echo ""
echo "     NOTE: This uses Claude Code scheduled triggers (free, no API key needed)."
echo "     Do NOT create a GitHub Actions workflow — the agent runs on Anthropic's cloud."
echo ""
echo "  4. SET UP EMAIL (optional, recommended)"
echo "     a) Sign up at resend.com, get an API key"
echo "     b) Add GitHub repo secrets: RESEND_API_KEY, EMAIL_TO"
echo "     c) Deploy the feedback worker:"
echo "        cd worker && npx wrangler deploy && npx wrangler secret put GITHUB_TOKEN"
echo "     d) Add the worker URL as GitHub secret: FEEDBACK_URL"
echo ""
echo "  Full instructions: https://github.com/neelgun17/morning-digest"
echo ""
