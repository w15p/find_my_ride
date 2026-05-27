# ── SSM Parameter Store — application secrets ────────────────────────────────
# SecureString parameters under /haystack/prod/ namespace.
# KMS encryption uses the AWS-managed SSM key (aws/ssm) rather than the data
# CMK because SSM's GetParametersByPath already scopes access via IAM, and
# using a separate key for SSM vs EBS/S3 is good separation-of-concern.
#
# IMPORTANT: All parameters are created with placeholder values.
# lifecycle.ignore_changes on value means Terraform will NEVER overwrite
# values you set via aws ssm put-parameter — real secrets never touch
# terraform state (local or remote).
#
# Populate after first apply:
#   aws ssm put-parameter \
#     --name "/haystack/prod/EBAY_APP_ID" \
#     --value "your-real-value" \
#     --type SecureString \
#     --overwrite \
#     --region us-west-1
#
# The outputs.tf file generates copy-paste commands for all 7 parameters.

locals {
  ssm_prefix = "/haystack/prod"
  # Secret names sourced from:
  #   - .env.example / README.md credential table
  #   - grep os.getenv across scrapers/*.py
  # If you add new secrets, add them here and re-apply. The instance
  # user-data pulls ALL parameters under /haystack/prod/ via GetParametersByPath,
  # so new parameters are picked up on the next instance reboot or manual
  # .env refresh without code changes.
  secret_names = [
    "EBAY_APP_ID",
    "EBAY_CERT_ID",
    "MARKTPLAATS_CLIENT_ID",
    "MARKTPLAATS_CLIENT_SECRET",
    "SMTP_USER",
    "SMTP_PASS",
    "DIGEST_RECIPIENTS",
  ]
}

resource "aws_ssm_parameter" "secrets" {
  for_each = toset(local.secret_names)

  name        = "${local.ssm_prefix}/${each.key}"
  description = "Haystack application secret - set via aws ssm put-parameter after apply"
  type        = "SecureString"

  # Placeholder value. The lifecycle block below prevents Terraform from
  # ever seeing or overwriting the real value once you've set it.
  value = "PLACEHOLDER_SET_VIA_CLI"

  lifecycle {
    # Do not overwrite the value on subsequent applies. This means:
    #   1. Real secrets never appear in terraform state.
    #   2. Running terraform apply after setting a real value won't revert it.
    # If you need to rotate a secret, use aws ssm put-parameter --overwrite.
    ignore_changes = [value]
  }

  tags = {
    Name   = "haystack-secret-${each.key}"
    Secret = each.key
  }
}

# ── Cloudflare Origin CA cert + key ───────────────────────────────────────────
# Stored under a SEPARATE namespace (/haystack/tls/*) so the refresh-env.sh
# script that flattens /haystack/prod/* into .env doesn't accidentally drop
# PEM bytes into an environment variable. The user_data bootstrap pulls
# /haystack/tls/* via a dedicated refresh-tls.sh that writes /etc/caddy/tls/.
#
# Generate the cert + key once in the CF dashboard:
#   SSL/TLS → Origin Server → Create Certificate → 15-year RSA
#   Common Name: haystack.willowisp.net (or *.willowisp.net for a wildcard)
# Then aws ssm put-parameter for both values (instructions in outputs.tf).
locals {
  tls_secret_names = [
    "CF_ORIGIN_CERT", # PEM-encoded certificate (BEGIN CERTIFICATE)
    "CF_ORIGIN_KEY",  # PEM-encoded private key (BEGIN PRIVATE KEY)
  ]
}

resource "aws_ssm_parameter" "tls" {
  for_each = toset(local.tls_secret_names)

  name        = "/haystack/tls/${each.key}"
  description = "Cloudflare Origin CA TLS material - generated in CF dashboard, set via aws ssm put-parameter"
  type        = "SecureString"
  value       = "PLACEHOLDER_SET_VIA_CLI"

  lifecycle {
    ignore_changes = [value]
  }

  tags = {
    Name   = "haystack-tls-${each.key}"
    Secret = each.key
  }
}
