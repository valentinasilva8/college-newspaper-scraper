#!/usr/bin/env bash
# Nightly backup of corpus CSVs, checkpoints, and failure logs to S3.
#
# Corpus data never goes to GitHub. This bucket is the authoritative copy.
#
#   sudo systemctl start newspaper-backup.service
#
# Requires BACKUP_BUCKET in /etc/newspaper-scraper.env and an instance role
# (preferred) or AWS credentials available to the scraper user.

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/newspaper-scraper}"
# The env file is root-only (mode 600). Under systemd, EnvironmentFile= already
# injected it, so only source it when this user can actually read it.
# shellcheck disable=SC1091
if [[ -r /etc/newspaper-scraper.env ]]; then
  source /etc/newspaper-scraper.env
fi

if [[ -z "${BACKUP_BUCKET:-}" || "$BACKUP_BUCKET" == *CHANGE-ME* ]]; then
  echo "BACKUP_BUCKET is not set in /etc/newspaper-scraper.env" >&2
  exit 1
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
cd "$APP_DIR"

echo "==> Writing checksum manifest"
mkdir -p logs/manifests
( cd output && sha256sum ./*.csv 2>/dev/null || true ) > "logs/manifests/output_${STAMP}.sha256"

echo "==> Syncing corpus CSVs"
aws s3 sync output/ "${BACKUP_BUCKET}/output/" \
  --exclude '*' --include '*.csv' --only-show-errors

echo "==> Syncing checkpoints, failures, and manifests"
aws s3 sync logs/checkpoints/ "${BACKUP_BUCKET}/checkpoints/" --only-show-errors
aws s3 sync logs/manifests/ "${BACKUP_BUCKET}/manifests/" --only-show-errors
for f in logs/failed_*.csv; do
  [[ -e "$f" ]] || continue
  aws s3 cp "$f" "${BACKUP_BUCKET}/failures/$(basename "$f")" --only-show-errors
done

echo "==> Publishing status snapshot"
.venv/bin/python scripts/status_report.py --json logs/status.json >/dev/null
aws s3 cp logs/status.json "${BACKUP_BUCKET}/status/status.json" --only-show-errors
aws s3 cp logs/status.json "${BACKUP_BUCKET}/status/history/status_${STAMP}.json" --only-show-errors

echo "==> Backup complete at ${STAMP}"
