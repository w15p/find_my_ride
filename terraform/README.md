# Haystack — Terraform runbook

Deploys a single-instance EC2 setup for the haystack classic-car scraper + review app to AWS us-west-1.

---

## Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| Terraform | >= 1.6.0 | [terraform.io/downloads](https://developer.hashicorp.com/terraform/install) |
| AWS CLI | >= 2.x | [aws.amazon.com/cli](https://aws.amazon.com/cli/) |
| jq | any | `brew install jq` |

You need an AWS account with admin-equivalent permissions in us-west-1. Configure the CLI:

```
aws configure
```

Confirm identity:

```
aws sts get-caller-identity
```

---

## Step 1 — Edit terraform.tfvars

```
cp terraform.tfvars.example terraform.tfvars
```

Edit `terraform.tfvars` and set:

| Variable | What to put |
|----------|-------------|
| `admin_ip_cidr` | Your current public IP + `/32`. Find it: `curl -s https://checkip.amazonaws.com` |
| `ssh_key_name` | Name of an EC2 key pair you already have in us-west-1 (see below) |
| `notification_email` | Email that receives AWS Budget cost alerts |
| `repo_url` | GitHub URL for your fork/copy of the repo |

**Creating an EC2 key pair** (if you don't have one in us-west-1):

```
aws ec2 create-key-pair \
  --key-name haystack \
  --region us-west-1 \
  --query 'KeyMaterial' \
  --output text > ~/.ssh/haystack.pem

chmod 600 ~/.ssh/haystack.pem
```

---

## Step 2 — Init, Plan, Apply

```
cd terraform/

terraform init

terraform plan -out=haystack.tfplan

# Review the plan output carefully before applying.
# Expected: ~25 resources to add, 0 to change, 0 to destroy.

terraform apply haystack.tfplan
```

Apply takes 3-5 minutes. At the end, Terraform prints all outputs. Keep the terminal open.

---

## Step 3 — Cloudflare DNS

Copy the `eip_public_ip` from the Terraform output:

```
terraform output eip_public_ip
```

In the Cloudflare dashboard (dash.cloudflare.com):

1. Select the `willowisp.net` zone
2. DNS → Add record:
   - Type: **A**
   - Name: **haystack**
   - Content: *paste the EIP here*
   - Proxy: **Proxied** (orange cloud ON — Cloudflare terminates TLS at edge)
   - TTL: Auto
3. Wait ~30 seconds, then test:

```
curl -I https://haystack.willowisp.net
```

At this point you'll see a 502 or connection refused — the app is still bootstrapping. That's expected.

---

## Step 4 — Populate SSM secrets (app secrets + CF Origin TLS cert)

The instance bootstraps from SSM at first boot, but the parameters contain placeholder values until you set them. Two namespaces to populate:

- `/haystack/prod/*` — the 7 app secrets (eBay, Marktplaats, SMTP). Values come from your local `.env`.
- `/haystack/tls/*` — the Cloudflare Origin CA cert + key (PEM bytes). Generated in the CF dashboard, not in `.env`.

Print all the populate commands:

```
terraform output ssm_populate_commands
```

### 4a. App secrets (from local .env)

Run each `aws ssm put-parameter` command for the `/haystack/prod/*` namespace, replacing `VALUE` with the real credential from `cat /path/to/find_my_ride/.env`.

### 4b. Cloudflare Origin CA cert + key

The webapp uses a Cloudflare Origin CA cert (15-year, RSA, free) instead of Let's Encrypt. This avoids the chicken-and-egg of needing port 80 open for ACME HTTP-01 challenges, and is the right choice anyway once Cloudflare is in the request path.

**One-time generation in the CF dashboard:**

1. Log into the CF dashboard → select the `willowisp.net` zone
2. **SSL/TLS** → **Origin Server** → **Create Certificate**
3. Settings:
   - **Private key type:** RSA (2048-bit)
   - **Hostnames:** `haystack.willowisp.net` (or `*.willowisp.net` if you want one cert for multiple subdomains)
   - **Certificate validity:** 15 years
4. CF shows you two text blocks: **Origin Certificate** (PEM, starts with `-----BEGIN CERTIFICATE-----`) and **Private Key** (PEM, starts with `-----BEGIN PRIVATE KEY-----`).
5. Save each to a local file:
   - Origin Certificate → `cf-origin.crt`
   - Private Key → `cf-origin.key`
6. Upload both to SSM (the `file://` prefix lets the AWS CLI read the file contents):
   ```
   aws ssm put-parameter --name "/haystack/tls/CF_ORIGIN_CERT" \
     --value "file://cf-origin.crt" --type SecureString --overwrite --region us-west-1
   aws ssm put-parameter --name "/haystack/tls/CF_ORIGIN_KEY" \
     --value "file://cf-origin.key" --type SecureString --overwrite --region us-west-1
   ```
7. **Delete the local PEM files** — they're now in SSM (encrypted at rest with the data CMK).
8. **Flip CF SSL mode to "Full (strict)"** — CF dashboard → **SSL/TLS** → **Overview** → set encryption mode to **Full (strict)**. Without this, Cloudflare won't validate the Origin CA cert at the edge and you'll see SSL handshake errors.

### Verify all 9 parameters are set (no PLACEHOLDER values remaining)

```
aws ssm get-parameters-by-path \
  --path "/haystack/" \
  --recursive \
  --with-decryption \
  --region us-west-1 \
  --query "Parameters[].{Name:Name,Length:length(Value)}" \
  --output table
```

The `Length` column should show real byte counts (not 22, the length of the placeholder string).

---

## Step 5 — Wait for bootstrap, then SSH in

The instance runs the user-data bootstrap script on first boot. This takes 5-10 minutes (dnf update + Playwright Chromium download is the slow part).

Watch bootstrap progress:

```
# Get the instance ID from Terraform output
INSTANCE_ID=$(terraform output -raw instance_id)

# Tail the cloud-init log (once SSH is available)
ssh -i ~/.ssh/haystack.pem ec2-user@$(terraform output -raw eip_public_ip) \
  "tail -f /var/log/haystack-bootstrap.log"
```

Once the bootstrap log shows "=== Haystack bootstrap complete ===", the app is running.

Check that the web service is up:

```
ssh -i ~/.ssh/haystack.pem ec2-user@$(terraform output -raw eip_public_ip) \
  "sudo systemctl status haystack-web && curl -s http://localhost:8002/api/stats"
```

---

## Step 6 — Migrate data from your Mac

Print rsync commands:

```
terraform output data_migration_commands
```

Run them from your Mac, editing the local path to your `find_my_ride/` checkout:

```
# SQLite database
rsync -avz --progress \
  -e "ssh -i ~/.ssh/haystack.pem" \
  /path/to/find_my_ride/listings.db \
  ec2-user@<EIP>:/var/lib/haystack/listings.db

# Facebook Playwright session (authenticated cookie profile)
rsync -avz --progress \
  -e "ssh -i ~/.ssh/haystack.pem" \
  /path/to/find_my_ride/.fb_profile/ \
  ec2-user@<EIP>:/var/lib/haystack/.fb_profile/

# Fix ownership on the instance
ssh -i ~/.ssh/haystack.pem ec2-user@<EIP> \
  "sudo chown -R haystack:haystack /var/lib/haystack/ && sudo systemctl restart haystack-web"
```

---

## Step 7 — Verify the app

Open `https://haystack.willowisp.net` in your browser.

You should see the React card grid. If not:

```
# Check Caddy
ssh -i ~/.ssh/haystack.pem ec2-user@<EIP> "sudo systemctl status caddy"

# Check the web service
ssh -i ~/.ssh/haystack.pem ec2-user@<EIP> "sudo journalctl -u haystack-web -n 50"

# Check Caddy logs
ssh -i ~/.ssh/haystack.pem ec2-user@<EIP> "sudo tail -50 /var/log/caddy/access.log"
```

---

## Step 8 — Refresh .env from SSM after setting secrets

After you populate SSM parameters (Step 4), the instance .env file still has placeholder values. Refresh it:

```
ssh -i ~/.ssh/haystack.pem ec2-user@<EIP> \
  "sudo bash /opt/haystack/scripts/refresh-env.sh && sudo systemctl restart haystack-web"
```

---

## Step 9 — Facebook login (if not migrating .fb_profile/)

If you're not migrating `.fb_profile/` from your Mac, you need to authenticate Facebook interactively. The problem: `--fb-login` opens a headed Chromium window, but the instance has no display.

Options:

**Option A (recommended) — rsync from Mac (Step 6 above).** Your local `.fb_profile/` is already authenticated. Fastest path.

**Option B — SSH X11 forwarding:**

```
# Mac: install XQuartz (https://www.xquartz.org/) first
ssh -X -i ~/.ssh/haystack.pem ec2-user@<EIP>
sudo -u haystack /opt/haystack/.venv/bin/python run.py --fb-login
```

**Option C — Xvfb virtual framebuffer:**

```
ssh -i ~/.ssh/haystack.pem ec2-user@<EIP>
sudo -u haystack Xvfb :99 -screen 0 1280x1024x24 &
export DISPLAY=:99
sudo -u haystack /opt/haystack/.venv/bin/python run.py --fb-login
```

---

## Step 10 — Lock down the security group (deferred)

After confirming traffic flows through Cloudflare correctly, restrict the HTTPS ingress rule to Cloudflare IP ranges only. This prevents direct-IP access that bypasses Cloudflare Access.

Add the Cloudflare IP ranges to `variables.tf` as a list variable and replace the `0.0.0.0/0` ingress rule in `main.tf`. Current CF IP ranges:
- https://www.cloudflare.com/ips-v4
- https://www.cloudflare.com/ips-v6

---

## Useful day-to-day commands

```
# Tail app logs live
ssh -i ~/.ssh/haystack.pem ec2-user@<EIP> \
  "sudo tail -f /var/log/haystack/web.log"

# Trigger a manual scrape run
ssh -i ~/.ssh/haystack.pem ec2-user@<EIP> \
  "sudo systemctl start haystack-scrape"

# Check timer schedules
ssh -i ~/.ssh/haystack.pem ec2-user@<EIP> \
  "systemctl list-timers haystack-*"

# Tail CloudWatch logs (from Mac)
aws logs tail /haystack/app --follow --region us-west-1

# On-demand EBS snapshot (before risky migrations)
aws backup start-backup-job \
  --backup-vault-name haystack-backup-vault \
  --resource-arn $(terraform output -raw data_volume_id | xargs -I{} \
      aws ec2 describe-volumes --volume-ids {} --query 'Volumes[0].VolumeId' --output text) \
  --iam-role-arn arn:aws:iam::$(aws sts get-caller-identity --query Account --output text):role/haystack-backup-role \
  --region us-west-1

# Rotate a secret
aws ssm put-parameter \
  --name "/haystack/prod/SMTP_PASS" \
  --value "new-app-password" \
  --type SecureString \
  --overwrite \
  --region us-west-1
# Then refresh .env on the instance:
ssh -i ~/.ssh/haystack.pem ec2-user@<EIP> \
  "sudo bash /opt/haystack/scripts/refresh-env.sh && sudo systemctl restart haystack-web"
```

---

## Architecture decisions (recorded here for future reference)

| Decision | Rationale |
|----------|-----------|
| us-west-1 (N. California) | User is in California; Facebook cookie geo-continuity (FB returns geo-restricted results from the browser's apparent location) |
| t3.micro | Free tier year 1; upgrade to t3.small if Playwright OOMs (Chromium needs ~600 MB RAM per tab) |
| Amazon Linux 2023 | t3 is x86_64; AL2023 ships Python 3.11+ and has a modern systemd. Ships Python 3.13 via dnf |
| Cloudflare DNS (not Route 53) | Zone already on CF; CF proxy provides DDoS protection, edge caching, and CF Access for auth |
| Caddy (not nginx/apache) | Single binary, sane defaults, simple `tls cert key` directive for the CF Origin CA cert. ACME disabled globally (`auto_https off`) since we use CF Origin CA — no port 80 needed |
| CF Origin CA cert (not Let's Encrypt) | 15-year RSA cert, generated in CF dashboard, stored in SSM `/haystack/tls/*`, refreshed via `refresh-tls.sh`. No HTTP-01 challenge → no port 80 requirement → smaller attack surface. Requires CF SSL mode = "Full (strict)" |
| No NAT Gateway | Saves $33/mo; instance has public IP for outbound. Add if marketplace IPs need to be stable/allowlisted |
| No ALB | Single instance; Caddy handles TLS termination. Add ALB if auto-scaling or health-check integration is needed |
| SSM Parameter Store (not Secrets Manager) | Standard params are always-free; Secrets Manager costs $0.40/secret/mo × 7 = $2.80/mo extra |
| EBS data volume separate from root | Root vol (30 GiB) is reproducible from AMI + user-data; data vol (20 GiB) holds listings.db + .fb_profile/ and must survive instance replacement. DeleteOnTermination=false on data vol |
| AWS Backup for EBS snapshots | Managed service handles scheduling, retention, and vault encryption without cron scripts |
| KMS CMK (not AWS-managed key) | Customer controls key policy and can audit usage in CloudTrail; required for cross-service grants (EBS → Backup → S3) |
| Cloudflare Access for auth | Free tier (≤50 users), zero-code auth, integrates with CF Zero Trust. Configured in CF dashboard, not Terraform |
| Local Terraform state (default) | Single developer; no CI pipeline yet. S3+DDB backend stub provided for when it's needed |

---

## Cost estimate

Year 1 (EC2 free tier):

| Resource | Monthly cost |
|----------|-------------|
| t3.micro (750h free tier) | $0.00 |
| EBS root 30 GiB gp3 | $2.40 |
| EBS data 20 GiB gp3 (KMS) | $1.60 |
| Elastic IP (attached) | $0.00 |
| KMS CMK | $1.00 |
| S3 backups (~100 MB) | $0.01 |
| AWS Backup snapshots (7×20 GiB) | $0.56 |
| CloudWatch Logs | $0.10 |
| SSM Standard params | $0.00 |
| **Total** | **~$6/mo** |

Year 2 (EC2 paid):

| Resource | Monthly cost |
|----------|-------------|
| t3.micro on-demand | $8.50 |
| Everything else | ~$6.00 |
| **Total** | **~$14-15/mo** |

The $10/mo Budget will fire at 80% in year 2. Raise `monthly_budget_usd` to 20 after the free tier expires.
