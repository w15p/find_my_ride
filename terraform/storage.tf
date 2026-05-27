# ── KMS customer-managed key (data) ──────────────────────────────────────────
# One CMK for the EBS data volume, S3 backup bucket, and AWS Backup vault.
# Keeping them on a single key simplifies key policy management while still
# satisfying the "customer controls the key" requirement. If you need to
# revoke backup-vault access independently of volume access, split into two
# CMKs later.

resource "aws_kms_key" "data" {
  description             = "Haystack data encryption (EBS data vol + S3 backups + Backup vault)"
  deletion_window_in_days = 14   # Minimum is 7; 14 days gives a recovery window
  enable_key_rotation     = true # Annual automatic rotation (AWS-managed rotation)
  multi_region            = false

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # Root account full control — required so the key can be managed via IAM.
      # Without this statement, even the account root cannot administer the key.
      {
        Sid    = "RootFullControl"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"
        }
        Action   = "kms:*"
        Resource = "*"
      },
      # EC2 instance role: decrypt EBS + S3 objects. GenerateDataKey is
      # required by S3 SSE-KMS when the instance calls PutObject.
      {
        Sid    = "InstanceRoleDecrypt"
        Effect = "Allow"
        Principal = {
          AWS = aws_iam_role.haystack_instance.arn
        }
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey",
          "kms:DescribeKey"
        ]
        Resource = "*"
      },
      # AWS Backup service role needs to encrypt/decrypt snapshots.
      # Reference via resource ARN (not hardcoded string) so Terraform creates
      # the IAM role before updating the key policy.
      {
        Sid    = "BackupServiceRole"
        Effect = "Allow"
        Principal = {
          AWS = aws_iam_role.backup.arn
        }
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey",
          "kms:DescribeKey",
          "kms:CreateGrant"
        ]
        Resource = "*"
      },
      # AWS services (EBS, S3) need CreateGrant to use the key for encryption
      # on behalf of the instance. Without this, EBS volume attachment fails.
      {
        Sid    = "AWSServiceGrants"
        Effect = "Allow"
        Principal = {
          Service = [
            "ec2.amazonaws.com",
            "s3.amazonaws.com",
            "backup.amazonaws.com"
          ]
        }
        Action = [
          "kms:CreateGrant",
          "kms:ListGrants",
          "kms:RevokeGrant"
        ]
        Resource = "*"
        Condition = {
          Bool = {
            "kms:GrantIsForAWSResource" = "true"
          }
        }
      }
    ]
  })

  tags = {
    Name = "haystack-data-key"
  }
}

resource "aws_kms_alias" "data" {
  name          = "alias/haystack-data"
  target_key_id = aws_kms_key.data.key_id
}

# ── S3 backup bucket ──────────────────────────────────────────────────────────
# Stores: sqlite/ (listings.db snapshots) + fb-profile/ (Playwright session
# tar archives). Name includes account ID to ensure global uniqueness.
#
# Object Lock governance mode: provides WORM protection against accidental
# deletion and ransomware. Governance mode (vs Compliance) allows you to
# delete objects if you have the s3:BypassGovernanceRetention permission —
# useful when you need to clean up old backups manually. Compliance mode
# would prevent deletion even with that permission for the retention period.

resource "aws_s3_bucket" "backups" {
  bucket = "haystack-backups-${data.aws_caller_identity.current.account_id}"

  # Prevent terraform destroy from accidentally deleting the bucket and all
  # backup data. Remove this if you actually want to destroy the bucket.
  lifecycle {
    prevent_destroy = true
  }

  tags = {
    Name = "haystack-backups"
  }
}

# Object Lock must be enabled at bucket creation time — cannot be added later.
# Terraform handles this via the object_lock_enabled attribute on the bucket.
# AWS additionally requires versioning to be ENABLED on the bucket before the
# Object Lock configuration can be applied; without depends_on, terraform
# parallelism races these two and the Object Lock call hits InvalidBucketState.
resource "aws_s3_bucket_object_lock_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id

  rule {
    default_retention {
      mode = "GOVERNANCE"
      days = 30
    }
  }

  depends_on = [aws_s3_bucket_versioning.backups]
}

resource "aws_s3_bucket_versioning" "backups" {
  bucket = aws_s3_bucket.backups.id

  versioning_configuration {
    status = "Enabled" # Required for Object Lock
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.data.arn
    }
    bucket_key_enabled = true # Reduces KMS API call costs significantly
  }
}

resource "aws_s3_bucket_public_access_block" "backups" {
  bucket = aws_s3_bucket.backups.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Lifecycle: move to Glacier Instant Retrieval after 30 days, delete at 180
# days. Instant Retrieval gives < 1 second restore latency at a fraction of
# Standard cost. 180-day hard delete keeps the bucket from growing unbounded.
resource "aws_s3_bucket_lifecycle_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id

  rule {
    id     = "haystack-backup-lifecycle"
    status = "Enabled"

    # Empty filter = match all objects in the bucket. Required by the
    # provider (would error in a future version without it); the old
    # implicit "no filter = match everything" behavior is being removed.
    filter {}

    transition {
      days          = 30
      storage_class = "GLACIER_IR"
    }

    expiration {
      days = 180
    }

    # Also clean up incomplete multipart uploads to prevent orphaned charges.
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# Bucket policy: deny all principals except the instance role and account root.
# This is defence-in-depth; the IAM role policy already scopes the instance to
# specific prefixes, but a bucket-level deny catches any future IAM mistakes.
resource "aws_s3_bucket_policy" "backups" {
  bucket = aws_s3_bucket.backups.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # Deny everything from principals that are NOT the instance role or root.
      {
        Sid    = "DenyNonHaystackPrincipals"
        Effect = "Deny"
        Principal = {
          AWS = "*"
        }
        Action   = "s3:*"
        Resource = [
          "arn:aws:s3:::haystack-backups-${data.aws_caller_identity.current.account_id}",
          "arn:aws:s3:::haystack-backups-${data.aws_caller_identity.current.account_id}/*"
        ]
        Condition = {
          StringNotLike = {
            "aws:PrincipalArn" = [
              "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root",
              aws_iam_role.haystack_instance.arn,
              # AWS Backup needs access to the bucket for restore operations.
              aws_iam_role.backup.arn
            ]
          }
        }
      },
      # Enforce TLS for all requests — reject plaintext HTTP.
      {
        Sid    = "DenyNonTLS"
        Effect = "Deny"
        Principal = {
          AWS = "*"
        }
        Action   = "s3:*"
        Resource = [
          "arn:aws:s3:::haystack-backups-${data.aws_caller_identity.current.account_id}",
          "arn:aws:s3:::haystack-backups-${data.aws_caller_identity.current.account_id}/*"
        ]
        Condition = {
          Bool = {
            "aws:SecureTransport" = "false"
          }
        }
      }
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.backups]
}

# ── AWS Backup ────────────────────────────────────────────────────────────────
# Nightly EBS snapshot of the data volume only. Root volume is NOT backed up
# because it's reproduced identically from the AMI + user-data script.
# Cron schedule: 07:00 UTC = 00:00 Pacific (midnight local time, off-peak).

resource "aws_iam_role" "backup" {
  name = "haystack-backup-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "backup.amazonaws.com" }
        Action    = "sts:AssumeRole"
      }
    ]
  })

  tags = {
    Name = "haystack-backup-role"
  }
}

resource "aws_iam_role_policy_attachment" "backup" {
  role       = aws_iam_role.backup.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForBackup"
}

resource "aws_backup_vault" "haystack" {
  name        = "haystack-backup-vault"
  kms_key_arn = aws_kms_key.data.arn

  tags = {
    Name = "haystack-backup-vault"
  }
}

resource "aws_backup_plan" "haystack" {
  name = "haystack-nightly-ebs"

  rule {
    rule_name         = "nightly-0700-utc"
    target_vault_name = aws_backup_vault.haystack.name
    schedule          = "cron(0 7 * * ? *)" # 07:00 UTC = 00:00 Pacific

    lifecycle {
      delete_after = 7 # Retain 7 daily snapshots (~1 week of point-in-time recovery)
    }

    recovery_point_tags = {
      Project     = "haystack"
      Environment = "prod"
    }
  }
}

resource "aws_backup_selection" "data_volume" {
  name         = "haystack-data-volume"
  plan_id      = aws_backup_plan.haystack.id
  iam_role_arn = aws_iam_role.backup.arn

  resources = [
    aws_ebs_volume.data.arn
  ]
}
