#!/usr/bin/env bash
# Update the server checkout to the latest reviewed code.
#
#   sudo -u scraper bash /opt/newspaper-scraper/deploy/deploy.sh
#
# Pulls main only. Cloud agents open pull requests; nothing reaches this server
# until a human merges. Running scrapers are NOT restarted automatically --
# restart them yourself when you want the new code to take effect, since full
# runs resume safely from checkpoints.

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/newspaper-scraper}"

cd "$APP_DIR"

echo "==> Current commit"
git --no-pager log -1 --oneline

echo "==> Fetching origin/main"
git fetch --prune origin
git reset --hard origin/main

echo "==> Installing requirements"
.venv/bin/pip install -q -r requirements.txt

echo "==> Running offline tests"
.venv/bin/python -m pytest tests -q

echo "==> Now at"
git --no-pager log -1 --oneline

cat <<'EOF'

Deploy complete. Running services still use the old code in memory.
To apply the update to a site:

  sudo systemctl restart newspaper-scraper@<site>

Check what is running:

  systemctl list-units 'newspaper-scraper@*'
EOF
