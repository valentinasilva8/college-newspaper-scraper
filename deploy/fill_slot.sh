#!/usr/bin/env bash
# Keep the request-scraper cap full by starting the next *ready* paper.
#
# Only starts site_keys listed in data/queue_ready.txt that also appear as
# queue_state=ready in data/scrape_queue.csv. Never invents a new site.
#
#   sudo -u scraper bash /opt/newspaper-scraper/deploy/fill_slot.sh
#   CAP=16 sudo -u scraper bash /opt/newspaper-scraper/deploy/fill_slot.sh
#
# Wired from newspaper-scraper@.service ExecStopPost and a systemd timer.
# Cap raised to 16 on t3.xlarge (2026-10-08); hold until 403s and CPU credits look fine.

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/newspaper-scraper}"
CAP="${CAP:-16}"
QUEUE="${APP_DIR}/data/scrape_queue.csv"
READY="${APP_DIR}/data/queue_ready.txt"
LOG="${APP_DIR}/logs/fill_slot.log"
PYTHON="${APP_DIR}/.venv/bin/python"

mkdir -p "$(dirname "$LOG")"
ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { echo "$(ts) $*" | tee -a "$LOG"; }

cd "$APP_DIR"

if [[ ! -x "$PYTHON" ]]; then
  log "ERROR: missing $PYTHON"
  exit 1
fi

# Refresh queue states from Status + systemd before deciding.
if ! "$PYTHON" scripts/refresh_queue.py >>"$LOG" 2>&1; then
  log "ERROR: refresh_queue.py failed"
  exit 1
fi

running="$(systemctl list-units --type=service --state=running 'newspaper-scraper@*' --no-pager --no-legend | wc -l | tr -d ' ')"
log "running=${running} cap=${CAP}"

if (( running >= CAP )); then
  log "at capacity; nothing to start"
  exit 0
fi

need=$((CAP - running))

# Next ready site_keys in queue order (category / state sort from refresh_queue).
mapfile -t ready_keys < <(
  "$PYTHON" - <<'PY'
import csv
from pathlib import Path
queue = Path("data/scrape_queue.csv")
if not queue.is_file():
    raise SystemExit(0)
with queue.open(encoding="utf-8", newline="") as fh:
    for row in csv.DictReader(fh):
        if row.get("queue_state") == "ready" and row.get("site_key"):
            print(row["site_key"])
PY
)

if ((${#ready_keys[@]} == 0)); then
  log "no ready papers in queue; leave slot empty (stage a 200-test + --mark-ready)"
  exit 0
fi

started=0
for key in "${ready_keys[@]}"; do
  if (( started >= need )); then
    break
  fi
  unit="newspaper-scraper@${key}.service"
  if systemctl is-active --quiet "$unit"; then
    log "skip ${key}: already active"
    continue
  fi
  # systemctl enable --now needs root when invoked from scraper via sudoers, or
  # from root via the timer. Prefer sudo -n when we are not root.
  if [[ "$(id -u)" -eq 0 ]]; then
    systemctl enable --now "$unit"
  else
    sudo -n systemctl enable --now "$unit"
  fi
  log "started ${unit}"
  started=$((started + 1))
  # Drop from ready list so a crash does not restart a finished/failed paper
  # until someone re-marks it after another 200-test.
  "$PYTHON" scripts/refresh_queue.py --unready "$key" >>"$LOG" 2>&1 || true
done

log "started ${started} unit(s); now running=$(systemctl list-units --type=service --state=running 'newspaper-scraper@*' --no-pager --no-legend | wc -l | tr -d ' ')"
exit 0
