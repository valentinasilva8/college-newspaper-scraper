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

echo "==> Refreshing scrape queue"
.venv/bin/python scripts/refresh_queue.py || true

echo "==> Installing/updating systemd units"
sudo install -m 644 deploy/newspaper-scraper@.service \
  /etc/systemd/system/newspaper-scraper@.service
sudo install -m 644 deploy/newspaper-fill-slot.service \
  /etc/systemd/system/newspaper-fill-slot.service
sudo install -m 644 deploy/newspaper-fill-slot.timer \
  /etc/systemd/system/newspaper-fill-slot.timer
sudo install -m 755 deploy/fill_slot.sh deploy/fill_slot.sh
if [[ ! -f /etc/sudoers.d/newspaper-fill-slot ]]; then
  echo "scraper ALL=NOPASSWD: /bin/systemctl start newspaper-fill-slot.service" \
    | sudo tee /etc/sudoers.d/newspaper-fill-slot >/dev/null
  sudo chmod 440 /etc/sudoers.d/newspaper-fill-slot
fi
sudo systemctl daemon-reload
# Timer is safe with an empty ready list (no-op). Enable so slots fill when ready.
sudo systemctl enable newspaper-fill-slot.timer
sudo systemctl start newspaper-fill-slot.timer || true

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

Fill-slot timer is enabled. After a 200-test passes:

  sudo -u scraper /opt/newspaper-scraper/.venv/bin/python \
    /opt/newspaper-scraper/scripts/refresh_queue.py --mark-ready SITE_KEY
  sudo systemctl start newspaper-fill-slot.service
EOF
