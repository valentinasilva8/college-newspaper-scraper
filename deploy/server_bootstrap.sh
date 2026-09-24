#!/usr/bin/env bash
# Prepare a fresh Ubuntu 22.04/24.04 server to run corpus scrapes.
#
# Run as a sudo-capable user on a NEW server:
#   sudo bash server_bootstrap.sh
#
# What it does:
#   - installs Python, git, and build basics
#   - creates the unprivileged "scraper" user
#   - clones the repo to /opt/newspaper-scraper using a read-only deploy key
#   - creates the virtualenv and installs requirements
#   - installs the systemd unit template and log rotation
#
# It does NOT start any scrape. Launching a full run is a separate, approved
# step -- see deploy/README.md.

set -euo pipefail

APP_USER="${APP_USER:-scraper}"
APP_DIR="${APP_DIR:-/opt/newspaper-scraper}"
REPO_URL="${REPO_URL:-git@github.com:valentinasilva8/college-newspaper-scraper.git}"
DEPLOY_KEY="${DEPLOY_KEY:-/home/${APP_USER}/.ssh/deploy_key}"
INSTALL_PLAYWRIGHT="${INSTALL_PLAYWRIGHT:-no}"

log() { printf '\n==> %s\n' "$1"; }

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo." >&2
  exit 1
fi

log "Installing system packages"
apt-get update -y
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip git ca-certificates curl unzip

log "Creating ${APP_USER} user"
if ! id -u "$APP_USER" >/dev/null 2>&1; then
  useradd --system --create-home --shell /bin/bash "$APP_USER"
fi
install -d -o "$APP_USER" -g "$APP_USER" -m 700 "/home/${APP_USER}/.ssh"

if [[ ! -f "$DEPLOY_KEY" ]]; then
  cat >&2 <<EOF

Missing deploy key at ${DEPLOY_KEY}.

Create one, then add the PUBLIC half to GitHub as a READ-ONLY deploy key
(repo Settings -> Deploy keys -> Add deploy key, leave write access unchecked):

  sudo -u ${APP_USER} ssh-keygen -t ed25519 -N '' -f ${DEPLOY_KEY}
  sudo cat ${DEPLOY_KEY}.pub

Then re-run this script.
EOF
  exit 1
fi

log "Trusting github.com host key"
sudo -u "$APP_USER" bash -c \
  "ssh-keyscan -t ed25519 github.com >> /home/${APP_USER}/.ssh/known_hosts 2>/dev/null"
sudo -u "$APP_USER" bash -c "sort -u -o /home/${APP_USER}/.ssh/known_hosts /home/${APP_USER}/.ssh/known_hosts"

cat > "/home/${APP_USER}/.ssh/config" <<EOF
Host github.com
  IdentityFile ${DEPLOY_KEY}
  IdentitiesOnly yes
EOF
chown "$APP_USER:$APP_USER" "/home/${APP_USER}/.ssh/config"
chmod 600 "/home/${APP_USER}/.ssh/config"

log "Cloning or updating ${APP_DIR}"
install -d -o "$APP_USER" -g "$APP_USER" "$APP_DIR"
if [[ -d "${APP_DIR}/.git" ]]; then
  sudo -u "$APP_USER" git -C "$APP_DIR" fetch --prune origin
  sudo -u "$APP_USER" git -C "$APP_DIR" reset --hard origin/main
else
  sudo -u "$APP_USER" git clone --branch main "$REPO_URL" "$APP_DIR"
fi

log "Creating virtualenv and installing requirements"
sudo -u "$APP_USER" python3 -m venv "${APP_DIR}/.venv"
sudo -u "$APP_USER" "${APP_DIR}/.venv/bin/pip" install --upgrade pip
sudo -u "$APP_USER" "${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

if [[ "$INSTALL_PLAYWRIGHT" == "yes" ]]; then
  log "Installing Playwright browsers (needed only for Yale-class sites)"
  sudo -u "$APP_USER" "${APP_DIR}/.venv/bin/playwright" install --with-deps chromium
fi

log "Creating output and log directories"
sudo -u "$APP_USER" mkdir -p "${APP_DIR}/output" "${APP_DIR}/logs/checkpoints" \
  "${APP_DIR}/logs/locks" "${APP_DIR}/logs/cache"

log "Installing systemd units"
install -m 644 "${APP_DIR}/deploy/newspaper-scraper@.service" \
  /etc/systemd/system/newspaper-scraper@.service
install -m 644 "${APP_DIR}/deploy/newspaper-backup.service" \
  /etc/systemd/system/newspaper-backup.service
install -m 644 "${APP_DIR}/deploy/newspaper-backup.timer" \
  /etc/systemd/system/newspaper-backup.timer
systemctl daemon-reload

log "Installing logrotate policy"
cat > /etc/logrotate.d/newspaper-scraper <<EOF
${APP_DIR}/logs/*.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
    copytruncate
    su ${APP_USER} ${APP_USER}
}
EOF

if [[ ! -f /etc/newspaper-scraper.env ]]; then
  log "Creating /etc/newspaper-scraper.env placeholder"
  cat > /etc/newspaper-scraper.env <<'EOF'
# Secrets and deployment settings. NEVER commit this file.
# S3 bucket used by deploy/backup.sh
BACKUP_BUCKET=s3://CHANGE-ME-bucket-name
AWS_DEFAULT_REGION=us-east-1
EOF
  chmod 600 /etc/newspaper-scraper.env
fi

cat <<EOF

Bootstrap complete.

Next steps (in order):
  1. Edit /etc/newspaper-scraper.env and set BACKUP_BUCKET.
  2. GATE -- confirm this IP is not blocked before any full run:
       sudo -u ${APP_USER} ${APP_DIR}/.venv/bin/python ${APP_DIR}/scripts/access_probe.py
  3. Bounded test for one site, e.g.:
       sudo -u ${APP_USER} ${APP_DIR}/.venv/bin/python ${APP_DIR}/run.py \\
         --site chicago --mode full --max-fetch 200
  4. Only after review, enable a full run:
       sudo systemctl enable --now newspaper-scraper@chicago
  5. Enable nightly backups:
       sudo systemctl enable --now newspaper-backup.timer
EOF
