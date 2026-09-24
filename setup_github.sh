#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# One-time GitHub setup for the job agent.
#
#   1. Checks git and the GitHub CLI, and logs you in if needed
#   2. Creates a PRIVATE repository and uploads this folder
#   3. Stores the keys from your .env file as GitHub Secrets
#   4. Starts a first test run (mode "light") and shows the result
#
# Your keys go straight from your computer to GitHub. The .env file itself
# is never uploaded (it's listed in .gitignore).
#
# Run from this folder:   bash setup_github.sh              (creates or uses a repo named job-agent)
#                   or:   bash setup_github.sh <repo-name>  (uses your existing repo with that name)
# Windows: run it in "Git Bash", which comes with Git for Windows.
# ---------------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")"
REPO_NAME="${1:-job-agent}"

step() { printf "\n\033[1m==> %s\033[0m\n" "$1"; }
fail() { printf "\n\033[31m%s\033[0m\n" "$1"; exit 1; }

# ── 1. Tools and login ────────────────────────────────────────────────────────
step "Checking tools"
command -v git >/dev/null || fail "Install git first: https://git-scm.com/downloads"
command -v gh  >/dev/null || fail "Install the GitHub CLI first: https://cli.github.com  (Mac: brew install gh)"

if ! gh auth status >/dev/null 2>&1; then
  echo "Log in to GitHub. A browser window will open; follow the steps there."
  gh auth login --web --git-protocol https --scopes workflow
fi
# Uploading the schedule file (.github/workflows) needs the 'workflow' permission.
if ! gh auth status 2>&1 | grep -q "workflow"; then
  echo "GitHub needs one extra permission to upload the schedule file. A browser window will open."
  gh auth refresh -h github.com -s workflow
fi
gh auth setup-git >/dev/null 2>&1 || true
OWNER="$(gh api user -q .login)"
FULL="$OWNER/$REPO_NAME"
echo "Logged in as $OWNER"

# ── 2. Keys present? ──────────────────────────────────────────────────────────
step "Checking your .env file"
if [ ! -f .env ]; then
  cp .env.example .env
  fail "Created a .env file here. Open it, paste in your keys, save, and run this script again."
fi
missing=()
for key in ANTHROPIC_API_KEY NOTION_API_KEY NOTION_DATABASE_ID ADZUNA_APP_ID ADZUNA_APP_KEY; do
  value="$(grep -E "^${key}=" .env | head -1 | cut -d= -f2- | tr -d '[:space:]"' || true)"
  [ -n "$value" ] || missing+=("$key")
done
[ ${#missing[@]} -eq 0 ] || fail "Still empty in .env: ${missing[*]}. Fill them in and run the script again."
echo "Required keys found (optional ones may stay empty)."

# ── 3. Repository ─────────────────────────────────────────────────────────────
step "Creating the private repository $FULL"
[ -d .git ] || git init -q -b main
if [ -z "$(git config user.email || true)" ]; then
  git config user.name "$OWNER"
  git config user.email "$(gh api user -q .id)+$OWNER@users.noreply.github.com"
fi
git add -A
git commit -qm "Job agent: first version" 2>/dev/null || echo "(nothing new to commit)"

if gh repo view "$FULL" >/dev/null 2>&1; then
  echo "Repository $FULL already exists; adding the files to it."
  if [ "$(gh repo view "$FULL" --json visibility -q .visibility)" = "PUBLIC" ]; then
    echo "WARNING: this repository is PUBLIC. profile.md contains your CV details."
    echo "Make it private: GitHub -> the repo -> Settings -> General -> Danger Zone -> Change visibility."
    read -r -p "Continue anyway? (y/N) " answer
    [ "$answer" = "y" ] || fail "Stopped. Make the repository private, then run the script again."
  fi
  BRANCH="$(gh repo view "$FULL" --json defaultBranchRef -q '.defaultBranchRef.name' 2>/dev/null || true)"
  [ -n "$BRANCH" ] || BRANCH=main
  git branch -M "$BRANCH"
  git remote get-url origin >/dev/null 2>&1 || git remote add origin "https://github.com/$FULL.git"
  # If GitHub created starter files (README, .gitignore, licence), keep them
  # but let this folder's version win wherever both have the same file.
  if git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
    git fetch -q origin "$BRANCH"
    git merge -q "origin/$BRANCH" --allow-unrelated-histories -X ours -m "Merge existing repository files" || true
  fi
  git push -u origin "$BRANCH"
else
  git branch -M main
  gh repo create "$FULL" --private --source=. --remote=origin --push
fi

# ── 4. Secrets ────────────────────────────────────────────────────────────────
step "Storing your keys as GitHub Secrets"
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
grep -E '^[A-Z_]+=.+' .env > "$tmp" || true
gh secret set -f "$tmp" -R "$FULL"
rm -f "$tmp"
echo "Secrets stored:"
gh secret list -R "$FULL"

# ── 5. First test run ─────────────────────────────────────────────────────────
step "Starting a first test run (mode: light)"
started=false
for _ in 1 2 3 4 5 6; do
  if gh workflow run job-agent.yml -R "$FULL" -f mode=light >/dev/null 2>&1; then
    started=true
    break
  fi
  sleep 5   # GitHub needs a few seconds to register a new workflow
done
$started || fail "Couldn't start the run yet. Open https://github.com/$FULL/actions and press 'Run workflow'."

sleep 6
RUN_ID="$(gh run list -R "$FULL" --workflow job-agent.yml --limit 1 --json databaseId -q '.[0].databaseId')"
if gh run watch "$RUN_ID" -R "$FULL" --exit-status; then
  step "Test run passed"
  echo "Start the first real search now with:"
  echo "  gh workflow run job-agent.yml -R $FULL -f mode=daily"
  echo "After that it runs by itself every morning. Runs: https://github.com/$FULL/actions"
else
  echo
  echo "The test run failed. See the log with:"
  echo "  gh run view $RUN_ID -R $FULL --log-failed"
  echo "and paste it to Claude."
  exit 1
fi
