# Server Runbook

How to stand up and operate the always-on scraping server. Architecture and
cost reasoning live in [`../docs/CLOUD_PLAN.md`](../docs/CLOUD_PLAN.md); this
file is the button-by-button version.

**Rules that do not change on a server:** frozen 10-column schema, configured
crawl delays, one process per domain, honest research UA by default (browser UA
only for approved Duke/Yale paths), no proxies or IP rotation, corpus CSVs never
in Git, and no multi-hour run without explicit approval.

---

## Files here

| File | Purpose |
| --- | --- |
| `server_bootstrap.sh` | One-time setup of a fresh Ubuntu server |
| `deploy.sh` | Pull reviewed code from `main` onto the server |
| `newspaper-scraper@.service` | systemd template, one instance per newspaper |
| `newspaper-backup.service` / `.timer` | Nightly backup to S3 |
| `backup.sh` | The backup itself (CSVs, checkpoints, failures, manifest) |
| `probe_urls.json` | Handful of URLs used by the access probe |

Related scripts: `../scripts/access_probe.py` (the pre-run gate) and
`../scripts/status_report.py` (progress you can read from anywhere).

---

## 1. Create the server

- Ubuntu 24.04 LTS, ~4 vCPU / 16 GB RAM / 250 GB SSD
- SSH key only; inbound SSH restricted to your IP; outbound open
- Set a billing alert before you launch anything

## 2. Bootstrap

```bash
ssh -i your-key.pem ubuntu@SERVER_IP
sudo useradd --system --create-home --shell /bin/bash scraper   # bootstrap also does this
sudo -u scraper ssh-keygen -t ed25519 -N '' -f /home/scraper/.ssh/deploy_key
sudo cat /home/scraper/.ssh/deploy_key.pub
```

Add that public key to GitHub as a **read-only deploy key**
(repo → Settings → Deploy keys → Add, leave write access unchecked). Then:

```bash
curl -O https://raw.githubusercontent.com/valentinasilva8/college-newspaper-scraper/main/deploy/server_bootstrap.sh
sudo bash server_bootstrap.sh
sudo nano /etc/newspaper-scraper.env    # set BACKUP_BUCKET
```

Add `INSTALL_PLAYWRIGHT=yes` before the command only when you need Yale-class
browser scraping on this box.

## 3. Access probe — the gate

Do this **before any full run**. A datacenter IP may be blocked where your home
IP was not; Duke's WAF and Yale's bot checkpoint are the known risks.

```bash
sudo -u scraper /opt/newspaper-scraper/.venv/bin/python \
  /opt/newspaper-scraper/scripts/access_probe.py
```

| Verdict | What to do |
| --- | --- |
| `OK` | Cleared for a bounded test on that site |
| `BLOCKED` | Run that site from an approved machine instead. Never add proxies or rotate IPs |
| `NETWORK_ERROR` | Check egress rules, then retry |

## 4. Bounded test before the real thing

```bash
sudo -u scraper /opt/newspaper-scraper/.venv/bin/python \
  /opt/newspaper-scraper/run.py --site chicago --mode full --max-fetch 200
```

Inspect the CSV: schema order, no duplicate URLs, no `UNPARSED:` dates, bodies
non-empty. Only then continue.

## 5. Start a full run

```bash
sudo systemctl enable --now newspaper-scraper@chicago
systemctl status newspaper-scraper@chicago
journalctl -u newspaper-scraper@chicago -f
```

One unit per newspaper. Roughly **8–12** request-based scrapers at once on this
size of box, plus **1–2** Playwright ones. Never two units for the same domain —
the domain lock will refuse the second anyway.

Stop, resume, list:

```bash
sudo systemctl stop newspaper-scraper@chicago      # progress is checkpointed
sudo systemctl start newspaper-scraper@chicago     # resumes, does not refetch
systemctl list-units 'newspaper-scraper@*'
```

## 6. Backups

```bash
sudo systemctl enable --now newspaper-backup.timer
systemctl list-timers newspaper-backup.timer
sudo -u scraper bash /opt/newspaper-scraper/deploy/backup.sh   # run one now
```

## 7. Checking progress from anywhere

```bash
sudo -u scraper /opt/newspaper-scraper/.venv/bin/python \
  /opt/newspaper-scraper/scripts/status_report.py
```

The nightly backup also uploads `status/status.json` to the bucket, so you can
read progress from your phone without SSH.

## 8. Shipping new code

Cloud agents open pull requests; you merge. Then:

```bash
sudo -u scraper bash /opt/newspaper-scraper/deploy/deploy.sh
sudo systemctl restart newspaper-scraper@<site>   # only when you want it applied
```

The server never builds from an unmerged branch.

---

## Troubleshooting

| Symptom | Likely cause and fix |
| --- | --- |
| Service keeps restarting | Check `journalctl -u newspaper-scraper@<site> -n 100`. Stop the unit before investigating so it cannot hammer the site |
| `Domain lock held by another process` | A run for that domain is already active, or a previous process died hard. Confirm nothing is running, then remove the stale file in `logs/locks/` |
| Rows stop growing | Look at `logs/failed_<site>.csv`. A burst of `network_error` usually means the site or the egress path is the problem, not the code |
| Disk filling | `status_report.py` shows free space. Corpus lives in S3 too; prune rotated logs first |
| Wrong data after a merge | Revert the commit on GitHub, re-run `deploy.sh`, restart. Checkpoints keep prior rows |
