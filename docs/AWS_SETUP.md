# AWS Setup — Click-by-Click

First-time setup of the scraping server in the PI's AWS account. Written for
someone who has not used the AWS console before. Architecture reasoning is in
[`CLOUD_PLAN.md`](CLOUD_PLAN.md); day-to-day operation is in
[`../deploy/README.md`](../deploy/README.md).

**Account:** PI's AWS account `253372858234`
**Sign-in:** https://253372858234.signin.aws.amazon.com/console
**Region to use everywhere:** `us-east-1` (N. Virginia) — cheapest and the default

---

## Permissions needed (send to PI)

| Need | Managed policy | Why |
| --- | --- | --- |
| Create and manage the server | `AmazonEC2FullAccess` | Launch the VM, key pair, firewall rules |
| Create and write the backup bucket | `AmazonS3FullAccess` (or scoped to one bucket) | Nightly corpus backups |
| Attach a storage role to the server | `iam:CreateRole`, `iam:CreateInstanceProfile`, `iam:AddRoleToInstanceProfile`, `iam:AttachRolePolicy`, `iam:PassRole`, `iam:GetRole`, `iam:ListRoles` | Lets the server write to S3 **without** long-lived keys |

**Access keys / API keys: not needed.** The server will use an IAM *instance
role*, which hands out short-lived credentials automatically. That is the safer
pattern and means no secret ever lands in the repo.

**Budget alert: PI should create it** (billing is account-level and usually
restricted to the account owner). Suggested: notify at **$100/month**.

If IAM role creation is not granted, the PI can create the role once using
[Step 3](#step-3--create-the-servers-s3-role) and everything else stays unchanged.

---

## Cost expectation

| Item | Choice | ~Monthly |
| --- | --- | --- |
| EC2 instance | `t3.large` (2 vCPU, 8 GB), running 24/7 | ~$60 |
| Disk | 100 GB gp3 | ~$8 |
| Public IPv4 | 1 address | ~$4 |
| S3 storage | tens of GB | ~$1–5 |
| **Total** | | **~$75/month** |

Start small on purpose. The instance type can be changed later by stopping the
instance, picking a bigger size, and starting it again — no rebuild needed.

**Billing note:** a *running* instance bills by the hour whether or not a scrape
is active. Stopping it stops compute charges, but the disk keeps billing.

---

## Step 0 — Sign in and set the region

1. Open https://253372858234.signin.aws.amazon.com/console
2. Enter the IAM username and password the PI created. Change the password if prompted.
3. **Top right corner:** click the region dropdown (it may say "Ohio" or similar)
   and choose **US East (N. Virginia) us-east-1**.

Every resource below must be created in the same region or they will not see
each other.

> Optional but recommended: click your username (top right) → **Security
> credentials** → **Assign MFA device** to add two-factor auth.

---

## Step 1 — Create the S3 bucket (backups)

1. In the top search bar type **S3** and open the S3 console.
2. Click **Create bucket**.
3. **Bucket name:** must be globally unique. Suggested:
   `columbia-newspaper-corpus` (add a suffix if taken).
4. **Region:** US East (N. Virginia) us-east-1.
5. **Block Public Access:** leave **all four boxes checked** (default). The
   corpus must not be public.
6. **Bucket Versioning:** select **Enable**. This protects against an accidental
   overwrite of a good CSV.
7. Leave everything else default → **Create bucket**.
8. Write the exact bucket name down; you will need it later as
   `BACKUP_BUCKET=s3://your-bucket-name`.

---

## Step 2 — Create the SSH key pair

1. Search **EC2** in the top bar and open the EC2 console.
2. Left sidebar → under *Network & Security* → **Key Pairs**.
3. **Create key pair**.
4. **Name:** `newspaper-scraper-key`
5. **Key pair type:** `ED25519`
6. **Private key file format:** `.pem`
7. **Create key pair** — the browser downloads `newspaper-scraper-key.pem`
   **once**. There is no second chance to download it.
8. Move it somewhere safe and lock its permissions:

```bash
mkdir -p ~/.ssh
mv ~/Downloads/newspaper-scraper-key.pem ~/.ssh/
chmod 400 ~/.ssh/newspaper-scraper-key.pem
```

> If you lose this file you are not stuck — the console has a browser-based
> terminal (see Step 6b).

---

## Step 3 — Create the server's S3 role

This lets the server upload backups with no stored passwords.

1. Search **IAM** → open the IAM console.
2. Left sidebar → **Roles** → **Create role**.
3. **Trusted entity type:** AWS service
4. **Use case:** EC2 → **Next**
5. **Permissions:** search `AmazonS3FullAccess`, tick it → **Next**
   *(Tighter option for later: a custom policy limited to the one bucket.)*
6. **Role name:** `newspaper-scraper-ec2-role`
7. **Create role**

If you get "access denied" here, the PI needs to either run these seven clicks
or grant the IAM permissions listed at the top of this document.

---

## Step 4 — Launch the server

1. EC2 console → big orange **Launch instance** button.
2. **Name:** `newspaper-scraper`
3. **Application and OS Images:** click **Ubuntu**, then pick an **LTS** release
   (24.04 LTS or 26.04 LTS), architecture **64-bit (x86)**.
4. **Instance type:** change from `t2.micro` to **`t3.large`**.
5. **Key pair (login):** select `newspaper-scraper-key`.
6. **Network settings** → click **Edit**:
   - **Auto-assign public IP:** Enable
   - **Firewall (security groups):** Create security group
   - **Security group name:** `newspaper-scraper-sg`
   - Rule 1: Type **SSH**, Source type **My IP**
     *(Not "Anywhere". If your home IP changes later you can edit this rule.)*
   - Do **not** add any other inbound rules. Outbound is open by default, which
     is what the scrapers need.
7. **Configure storage:** change `8 GiB` to **100 GiB**, volume type **gp3**.
8. **Advanced details** (expand it) → scroll to **IAM instance profile** →
   select `newspaper-scraper-ec2-role`.
9. Review the summary on the right → **Launch instance**.
10. Click **View all instances**. Wait until **Instance state** is *Running* and
    **Status check** shows *2/2 checks passed* (takes 1–3 minutes).
11. Click the instance and copy its **Public IPv4 address**.

---

## Step 5 — (Optional) Give it a fixed IP

Without this, the public IP changes every time the instance is stopped and
started. You are already paying for one IPv4 address, so this is effectively
free and saves confusion later.

1. EC2 sidebar → *Network & Security* → **Elastic IPs**
2. **Allocate Elastic IP address** → **Allocate**
3. Select it → **Actions** → **Associate Elastic IP address**
4. Resource type **Instance**, choose `newspaper-scraper` → **Associate**
5. Use this address from now on.

---

## Step 6 — Connect to the server

### 6a. From your Mac terminal

```bash
ssh -i ~/.ssh/newspaper-scraper-key.pem ubuntu@PUBLIC_IP
```

Type `yes` at the authenticity prompt the first time. A prompt like
`ubuntu@ip-172-31-x-x:~$` means you are in.

### 6b. If SSH fails — browser terminal

EC2 console → select the instance → **Connect** → **EC2 Instance Connect** tab →
**Connect**. This opens a terminal in the browser with no key file required.

Common SSH failures:

| Message | Cause |
| --- | --- |
| `Permission denied (publickey)` | Wrong username — it is `ubuntu`, not `root` or your name |
| Hangs then times out | Security group SSH rule does not match your current IP. Edit the rule to **My IP** again |
| `UNPROTECTED PRIVATE KEY FILE` | Run `chmod 400 ~/.ssh/newspaper-scraper-key.pem` |

---

## Step 7 — Create the deploy key and bootstrap

Run these **on the server** (after connecting in Step 6).

```bash
sudo useradd --system --create-home --shell /bin/bash scraper
sudo -u scraper mkdir -p /home/scraper/.ssh
sudo -u scraper ssh-keygen -t ed25519 -N '' -f /home/scraper/.ssh/deploy_key
sudo cat /home/scraper/.ssh/deploy_key.pub
```

Copy the printed line (starts with `ssh-ed25519`). Then in your browser:

1. Go to the GitHub repo → **Settings** → **Deploy keys** → **Add deploy key**
2. **Title:** `aws-scraper-server`
3. **Key:** paste the line
4. **Leave "Allow write access" UNCHECKED** — the server must never push
5. **Add key**

Back on the server:

```bash
curl -O https://raw.githubusercontent.com/valentinasilva8/college-newspaper-scraper/main/deploy/server_bootstrap.sh
sudo bash server_bootstrap.sh
```

This installs Python, clones the repo to `/opt/newspaper-scraper`, builds the
virtualenv, and installs the systemd units. It does **not** start any scrape.

Then set the bucket name:

```bash
sudo nano /etc/newspaper-scraper.env
# change BACKUP_BUCKET to s3://your-bucket-name
# Ctrl-O, Enter, Ctrl-X to save and exit
```

---

## Step 8 — The access probe (do not skip)

This is the gate. A datacenter IP can be blocked where your home IP was fine.

```bash
sudo -u scraper /opt/newspaper-scraper/.venv/bin/python \
  /opt/newspaper-scraper/scripts/access_probe.py
```

| Verdict | Meaning |
| --- | --- |
| `OK` | Cleared for a bounded test on that site |
| `BLOCKED` | Run that site from your Mac instead. No proxies, no IP rotation |
| `NETWORK_ERROR` | Check outbound rules and retry |

Expect Chicago and Northwestern to pass. Duke and Yale are the real questions.

---

## Step 9 — Bounded test, then the first real run

```bash
# small supervised test first
sudo -u scraper /opt/newspaper-scraper/.venv/bin/python \
  /opt/newspaper-scraper/run.py --site northwestern --mode full --max-fetch 200

# check it
sudo -u scraper /opt/newspaper-scraper/.venv/bin/python \
  /opt/newspaper-scraper/scripts/status_report.py
```

Only after that looks right:

```bash
sudo systemctl enable --now newspaper-scraper@northwestern
sudo systemctl enable --now newspaper-backup.timer
journalctl -u newspaper-scraper@northwestern -f     # Ctrl-C to stop watching
```

From here on, use [`../deploy/README.md`](../deploy/README.md) for day-to-day
operation.

---

## Safety habits

- Never paste AWS keys, the `.pem` file, or the deploy key into the repo or chat.
- Stop the instance if the project pauses for weeks: EC2 → select → **Instance
  state** → **Stop instance**. Disk charges continue; compute charges stop.
- Check the S3 bucket has real backups before trusting the server as the only copy.
- Keep the Chicago run on your Mac going until a server run has proven itself.
