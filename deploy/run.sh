#!/usr/bin/env bash
# One VPS check, mirroring .github/workflows/bot.yml:
# update code + config from main → restore state from the data branch → run → publish data branch.
# Usage: run.sh [run|digest|test-notify]     (TRIGGER=manual skips the interval check)
set -euo pipefail

MODE="${1:-run}"
TRIGGER="${TRIGGER:-schedule}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PUSH_URL="${DEALBOT_PUSH_URL:-git@github.com:cflank/deal-bot.git}"
KEY_FILE="${DEALBOT_DEPLOY_KEY:-/etc/dealbot/deploy_key}"
if [[ -f "$KEY_FILE" ]]; then
  export GIT_SSH_COMMAND="ssh -i $KEY_FILE -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
fi

cd "$REPO_DIR"
git fetch -q origin main
git reset -q --hard origin/main   # settings saved on the web page arrive here

rm -rf data && mkdir -p data
if git ls-remote --exit-code --heads origin data >/dev/null 2>&1; then
  git fetch -q --depth=1 origin data
  git archive FETCH_HEAD | tar -x -C data
fi

out="$(mktemp)"
trap 'rm -f "$out"' EXIT
GITHUB_OUTPUT="$out" python3 -m dealbot --mode "$MODE" --trigger "$TRIGGER" --data data

if grep -q '^ran=true' "$out"; then
  cd data
  rm -rf .git
  git init -q -b data
  git add -A
  git -c user.name="deal-bot (VPS)" -c user.email="deal-bot@localhost" commit -qm "data $(date -u +%Y-%m-%dT%H:%MZ) (vps)"
  git push -qf "$PUSH_URL" data
  echo "已更新 data 分支。"
fi
