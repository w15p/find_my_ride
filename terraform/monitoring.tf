# ── CloudWatch log group ──────────────────────────────────────────────────────
# Application logs from the haystack-web.service systemd unit and scraper
# cron services are shipped here via the CloudWatch agent (configured in
# user-data). 30-day retention keeps roughly a month of debugging history
# while keeping CloudWatch Logs costs near zero (~$0.50/GB ingested).
#
# TODO: Add a CloudWatch metric filter + alarm for "OOM" or "Killed" in the
# log group to catch Playwright Chromium OOM kills before they degrade the
# scrape pipeline silently. Pair with an SNS topic → email alert.

resource "aws_cloudwatch_log_group" "app" {
  name              = "/haystack/app"
  retention_in_days = 30

  tags = {
    Name = "haystack-app-logs"
  }
}

# ── AWS Budget ────────────────────────────────────────────────────────────────
# Monthly cost budget for the entire haystack AWS account spend.
# Alert thresholds: 50% ($5), 80% ($8), 100% ($10) of the monthly limit.
# Alerts go to notification_email via SNS-backed Budget notifications.
#
# Expected steady-state cost breakdown:
#   t3.micro (1 month, on-demand):     ~$8.50  (free tier year 1: $0)
#   EBS gp3 root 30 GiB:               ~$2.40
#   EBS gp3 data 20 GiB (KMS-enc):     ~$1.60
#   Elastic IP (while attached):        ~$0.00
#   S3 backups (first 30 days, std):    ~$0.10
#   KMS CMK:                            ~$1.00
#   CloudWatch Logs:                    ~$0.10
#   AWS Backup (EBS snapshots × 7):    ~$0.50
#   SSM Standard parameters:           ~$0.00  (free tier)
#   AWS Budget itself:                 ~$0.00  (2 active budgets free)
#   ─────────────────────────────────────────
#   Estimated year-1 (free tier EC2):  ~$6-7/mo
#   Estimated year-2 (paid EC2):       ~$14-15/mo
#
# If the 100% alert fires, check for: rogue data transfer, accidental EBS
# snapshots not covered by the lifecycle rule, or KMS API call spikes.

resource "aws_budgets_budget" "monthly" {
  name              = "haystack-monthly-budget"
  budget_type       = "COST"
  limit_amount      = tostring(var.monthly_budget_usd)
  limit_unit        = "USD"
  time_unit         = "MONTHLY"
  time_period_start = "2024-01-01_00:00"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 50
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.notification_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.notification_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.notification_email]
  }

  # Forecasted spend alert — fires when AWS predicts you'll exceed budget
  # before the month ends. Useful for catching runaway costs early.
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.notification_email]
  }
}
