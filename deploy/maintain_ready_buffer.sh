#!/usr/bin/env bash
# Keep CAP full and surface when the ready/test pipeline is too thin.
#
# Goals:
#   1. Always run fill_slot (start ready papers into free CAP slots).
#   2. Log how many papers are ready / mid-200-test / running.
#   3. Warn when the staged pipeline is below TARGET_PIPELINE so the agent
#      can configure + 200-test the next wave before slots go idle.
#
#   sudo -u scraper bash /opt/newspaper-scraper/deploy/maintain_ready_buffer.sh
#   (also via newspaper-ready-buffer.timer)

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/newspaper-scraper}"
CAP="${CAP:-16}"
# Keep this many papers in ready + active 200-test so finishes never idle CAP.
TARGET_PIPELINE="${TARGET_PIPELINE:-6}"
LOG="${APP_DIR}/logs/ready_buffer.log"
PYTHON="${APP_DIR}/.venv/bin/python"

mkdir -p "$(dirname "$LOG")"
ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { echo "$(ts) $*" | tee -a "$LOG"; }

cd "$APP_DIR"

running="$(systemctl list-units --type=service --state=running 'newspaper-scraper@*' --no-pager --no-legend | wc -l | tr -d ' ')"

ready_n="$("$PYTHON" - <<'PY'
import csv
from pathlib import Path
n = 0
p = Path("data/scrape_queue.csv")
if p.is_file():
    with p.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if row.get("queue_state") == "ready" and row.get("site_key"):
                n += 1
print(n)
PY
)"

# Active bounded 200-tests (not full systemd units).
testing_n="$(pgrep -u scraper -af 'run.py --site .*--max-fetch 200' 2>/dev/null | grep -vc grep || true)"
testing_n="${testing_n:-0}"

pipeline=$((ready_n + testing_n))
free=$((CAP - running))
if (( free < 0 )); then free=0; fi

log "running=${running}/${CAP} free=${free} ready=${ready_n} testing=${testing_n} pipeline=${pipeline} target=${TARGET_PIPELINE}"

# Always try to fill free slots from ready list.
if [[ -x "${APP_DIR}/deploy/fill_slot.sh" ]]; then
  if [[ "$(id -u)" -eq 0 ]]; then
    bash "${APP_DIR}/deploy/fill_slot.sh" >>"$LOG" 2>&1 || true
  else
    sudo -n bash "${APP_DIR}/deploy/fill_slot.sh" >>"$LOG" 2>&1 \
      || bash "${APP_DIR}/deploy/fill_slot.sh" >>"$LOG" 2>&1 \
      || true
  fi
fi

if (( pipeline < TARGET_PIPELINE )); then
  log "WARN thin_ready_buffer pipeline=${pipeline}<${TARGET_PIPELINE} — stage next wave (yaml+probe+200-test+mark-ready) before more slots free"
  # List configured candidates that still need a 200-test / mark-ready.
  "$PYTHON" - <<'PY' | tee -a "$LOG"
import csv
from pathlib import Path
queue = Path("data/scrape_queue.csv")
if not queue.is_file():
    raise SystemExit(0)
print("candidates_needing_stage:")
with queue.open(encoding="utf-8", newline="") as fh:
    for row in csv.DictReader(fh):
        if row.get("queue_state") == "candidate" and row.get("site_key"):
            key = row["site_key"]
            note = (row.get("notes") or row.get("reason") or "")[:80]
            print(f"  {key:16} {row.get('domain','')}  {note}")
PY
fi

exit 0
