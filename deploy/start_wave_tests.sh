#!/usr/bin/env bash
# Probe + start bounded 200-tests for a list of site keys, then watch/mark-ready/fill.
#
#   sudo -u scraper bash deploy/start_wave_tests.sh wellesley villanova ...
#   WAVE=20 sudo -u scraper bash deploy/start_wave_tests.sh baylor paladin ...
#
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/newspaper-scraper}"
PYTHON="${APP_DIR}/.venv/bin/python"
WAVE="${WAVE:-next}"
LOG="${APP_DIR}/logs/wave${WAVE}_watch.log"
MIN_ROWS="${MIN_ROWS:-80}"

if (($# < 1)); then
  echo "usage: $0 SITE_KEY [SITE_KEY...]" >&2
  exit 2
fi

cd "$APP_DIR"
sites=("$@")

probe_args=()
for s in "${sites[@]}"; do
  probe_args+=(--site "$s")
done
"$PYTHON" scripts/access_probe.py "${probe_args[@]}"

mkdir -p "$(dirname "$LOG")"
for site in "${sites[@]}"; do
  if pgrep -u scraper -f "run.py --site ${site} .*max-fetch 200" >/dev/null; then
    echo "already testing ${site}"
    continue
  fi
  nohup "$PYTHON" -u run.py --site "$site" --mode full --max-fetch 200 \
    --lastmod-year 2024 --refresh-discovery \
    >"logs/${site}-200test.log" 2>&1 &
  echo "started 200-test ${site} pid=$!"
done

# Background watcher for this wave
(
  while true; do
    alive=0
    for s in "${sites[@]}"; do
      if pgrep -u scraper -f "run.py --site ${s} .*max-fetch 200" >/dev/null; then
        alive=$((alive + 1))
      fi
    done
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) alive=${alive}" >>"$LOG"
    if ((alive == 0)); then
      for s in "${sites[@]}"; do
        n="$(python3 -c "import pathlib; p=pathlib.Path('output/${s}.csv'); print(max(0,sum(1 for _ in open(p))-1) if p.is_file() else 0)")"
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) ${s} rows=${n}" >>"$LOG"
        if ((n >= MIN_ROWS)); then
          "$PYTHON" scripts/refresh_queue.py --mark-ready "$s" >>"$LOG" 2>&1 || true
        fi
      done
      sudo -n bash "${APP_DIR}/deploy/fill_slot.sh" >>"$LOG" 2>&1 \
        || bash "${APP_DIR}/deploy/fill_slot.sh" >>"$LOG" 2>&1 \
        || true
      echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) wave${WAVE} watcher done" >>"$LOG"
      exit 0
    fi
    sleep 60
  done
) >/dev/null 2>&1 &
echo "watcher pid=$! log=$LOG"
