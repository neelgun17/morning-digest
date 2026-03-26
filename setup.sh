#!/bin/bash

# Morning Digest — Setup Script
# This script creates your private repo and prepares everything for scheduling.

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

# Check if directory already exists
if [ -d "../$REPO_NAME" ]; then
    echo "Error: Directory ../$REPO_NAME already exists. Choose a different name or remove it."
    exit 1
fi

echo ""
echo "Setting up '$REPO_NAME'..."
echo ""

# Create the private repo directory
mkdir -p "../$REPO_NAME/daily"
cp interests.template.md "../$REPO_NAME/interests.md"
cp feedback-log.template.md "../$REPO_NAME/feedback-log.md"

# Initialize git and create private repo
cd "../$REPO_NAME"
git init -b main
git add -A
git commit -m "Initial setup from morning-digest template"
gh repo create "$REPO_NAME" --private --source=. --push

REPO_URL=$(gh repo view --json url -q '.url')

echo ""
echo "========================================"
echo "  Setup Complete!"
echo "========================================"
echo ""
echo "Your private repo: $REPO_URL"
echo "Local directory:   $(pwd)"
echo ""
echo "Next steps:"
echo ""
echo "  1. Edit interests.md with your actual interests"
echo "     Open it now:  code interests.md"
echo ""
echo "  2. Schedule the daily agent (copy-paste into Claude Code):"
echo ""
echo "     /schedule create morning-digest daily at 7am"
echo ""
echo "     When prompted for the repo, use: $REPO_URL"
echo "     See README.md in the template repo for the full agent prompt."
echo ""
echo "  3. Tomorrow morning, pull your first digest:"
echo "     cd $(pwd) && git pull"
echo ""
