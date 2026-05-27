# ── Core connection outputs ───────────────────────────────────────────────────

output "eip_public_ip" {
  description = "Elastic IP address. Create an A record at Cloudflare: haystack.willowisp.net → this IP."
  value       = aws_eip.haystack.public_ip
}

output "ssh_command" {
  description = "SSH command for first-login tasks (FB auth, rsync data from Mac)."
  value       = "ssh -i ~/.ssh/${var.ssh_key_name}.pem ec2-user@${aws_eip.haystack.public_ip}"
}

output "instance_id" {
  description = "EC2 instance ID. Useful for aws ec2 commands and AWS Console."
  value       = aws_instance.haystack.id
}

output "data_volume_id" {
  description = "EBS data volume ID. Keep this safe: it holds listings.db and .fb_profile/."
  value       = aws_ebs_volume.data.id
}

output "backup_vault_name" {
  description = "AWS Backup vault name. Reference when triggering manual on-demand backups."
  value       = aws_backup_vault.haystack.name
}

output "backup_bucket_name" {
  description = "S3 bucket name for manual sqlite + fb-profile backups."
  value       = aws_s3_bucket.backups.id
}

output "kms_key_arn" {
  description = "Data CMK ARN (EBS + S3 + Backup vault). Reference when granting access to other principals."
  value       = aws_kms_key.data.arn
}

output "cloudwatch_log_group" {
  description = "CloudWatch log group for app logs. Tail with: aws logs tail /haystack/app --follow"
  value       = aws_cloudwatch_log_group.app.name
}

# ── SSM secret populate commands ─────────────────────────────────────────────
# Copy-paste these into your terminal after `terraform apply` to set real values.
# The placeholder values Terraform created are harmless — the app won't start
# correctly until these are populated.
#
# If you have the real values ready, you can pipe these to bash:
#   terraform output -raw ssm_populate_commands | bash
#
# WARNING: piping directly is convenient but leaks secrets into shell history.
# Prefer copy-pasting each command individually.

output "ssm_populate_commands" {
  description = "aws ssm put-parameter commands to populate secrets after apply. Edit the VALUE field for each."
  sensitive   = false # The commands contain only parameter NAMES, not actual values
  value       = <<-EOT
# Run these commands to populate SSM secrets (replace VALUE with real credentials):
# Region: us-west-1

aws ssm put-parameter \
  --name "/haystack/prod/EBAY_APP_ID" \
  --value "VALUE" \
  --type SecureString \
  --overwrite \
  --region us-west-1

aws ssm put-parameter \
  --name "/haystack/prod/EBAY_CERT_ID" \
  --value "VALUE" \
  --type SecureString \
  --overwrite \
  --region us-west-1

aws ssm put-parameter \
  --name "/haystack/prod/MARKTPLAATS_CLIENT_ID" \
  --value "VALUE" \
  --type SecureString \
  --overwrite \
  --region us-west-1

aws ssm put-parameter \
  --name "/haystack/prod/MARKTPLAATS_CLIENT_SECRET" \
  --value "VALUE" \
  --type SecureString \
  --overwrite \
  --region us-west-1

aws ssm put-parameter \
  --name "/haystack/prod/SMTP_USER" \
  --value "your-gmail@gmail.com" \
  --type SecureString \
  --overwrite \
  --region us-west-1

aws ssm put-parameter \
  --name "/haystack/prod/SMTP_PASS" \
  --value "your-gmail-app-password" \
  --type SecureString \
  --overwrite \
  --region us-west-1

aws ssm put-parameter \
  --name "/haystack/prod/DIGEST_RECIPIENTS" \
  --value "recipient@example.com" \
  --type SecureString \
  --overwrite \
  --region us-west-1

# ── Cloudflare Origin CA cert + key (PEM bytes) ──────────────────────────────
# Generate once in CF dashboard: SSL/TLS → Origin Server → Create Certificate
#   Hostnames: haystack.willowisp.net  (or *.willowisp.net for wildcard reuse)
#   Validity:  15 years (RSA 2048)
# Then save the cert to cf-origin.crt and the key to cf-origin.key locally and:

aws ssm put-parameter \
  --name "/haystack/tls/CF_ORIGIN_CERT" \
  --value "file://cf-origin.crt" \
  --type SecureString \
  --overwrite \
  --region us-west-1

aws ssm put-parameter \
  --name "/haystack/tls/CF_ORIGIN_KEY" \
  --value "file://cf-origin.key" \
  --type SecureString \
  --overwrite \
  --region us-west-1

# IMPORTANT: After Origin cert is in SSM, also flip Cloudflare's SSL mode to
# "Full (strict)" — CF dashboard → SSL/TLS → Overview → Encryption mode.
# Without this CF won't validate the Origin CA cert at the edge.

# Verify all parameters are set:
aws ssm get-parameters-by-path \
  --path "/haystack/" \
  --recursive \
  --with-decryption \
  --region us-west-1 \
  --query "Parameters[].{Name:Name,Length:length(Value)}" \
  --output table
EOT
}

# ── Cloudflare DNS instructions ───────────────────────────────────────────────

output "cloudflare_dns_instructions" {
  description = "Instructions for adding the Cloudflare A record."
  value       = <<-EOT
Cloudflare DNS setup (do this after apply):
  1. Log into dash.cloudflare.com
  2. Select the willowisp.net zone
  3. DNS → Add record:
       Type:    A
       Name:    haystack
       Content: ${aws_eip.haystack.public_ip}
       Proxy:   Proxied (orange cloud ON — CF terminates TLS at edge)
       TTL:     Auto
  4. Wait ~30s for propagation, then: curl -I https://haystack.willowisp.net
EOT
}

# ── Data migration rsync commands ─────────────────────────────────────────────

output "data_migration_commands" {
  description = "rsync commands to copy listings.db and .fb_profile/ from your Mac to the instance."
  value       = <<-EOT
# Run these from your Mac AFTER:
#   1. The instance is up (ssh works)
#   2. You've run `python run.py --fb-login` on the Mac to ensure .fb_profile/ is current

# Copy SQLite database:
rsync -avz --progress \
  -e "ssh -i ~/.ssh/${var.ssh_key_name}.pem" \
  /path/to/find_my_ride/listings.db \
  ec2-user@${aws_eip.haystack.public_ip}:/var/lib/haystack/listings.db

# Copy Facebook profile (authenticated Playwright session):
rsync -avz --progress \
  -e "ssh -i ~/.ssh/${var.ssh_key_name}.pem" \
  /path/to/find_my_ride/.fb_profile/ \
  ec2-user@${aws_eip.haystack.public_ip}:/var/lib/haystack/.fb_profile/

# Fix ownership after rsync (run on the instance via SSH):
ssh -i ~/.ssh/${var.ssh_key_name}.pem ec2-user@${aws_eip.haystack.public_ip} \
  "sudo chown -R haystack:haystack /var/lib/haystack/ && sudo systemctl restart haystack-web"
EOT
}
