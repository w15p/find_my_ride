# ── Data sources ─────────────────────────────────────────────────────────────

data "aws_caller_identity" "current" {}

# Amazon Linux 2023 x86_64 — most recent AMI in us-west-1.
# Using SSM parameter so the AMI ID stays current across applies without
# hardcoding a specific image ID that will eventually be deprecated.
data "aws_ssm_parameter" "al2023_ami" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

# ── VPC ──────────────────────────────────────────────────────────────────────
# Minimal single-public-subnet topology. No private subnets, no NAT Gateway
# (saves $33/mo; the instance has a public IP for outbound). All internet
# traffic — both inbound via Cloudflare and outbound scraper calls — flows
# through the IGW.
#
# TODO: Add private subnet + NAT GW if the scraper workload grows such that
# outbound origin IPs matter (e.g. marketplace IP allowlisting). Cost: ~$33/mo.

resource "aws_vpc" "main" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = "haystack-vpc"
  }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name = "haystack-igw"
  }
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.0.1.0/24"
  availability_zone       = "us-west-1a"
  map_public_ip_on_launch = false # We manage the EIP ourselves

  tags = {
    Name = "haystack-public-1a"
  }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = {
    Name = "haystack-public-rt"
  }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# ── Security group ────────────────────────────────────────────────────────────
# Ingress :22 — locked to admin_ip_cidr (override the default in tfvars!).
# Ingress :443 — open to all initially; Cloudflare terminates TLS at edge and
#               Caddy terminates the origin leg. Once Cloudflare Access is
#               configured and you've verified traffic flows correctly, restrict
#               this to Cloudflare IP ranges:
#               https://www.cloudflare.com/ips/
#
# TODO: Add an ingress rule for each Cloudflare IP range and remove 0.0.0.0/0
#       on :443 once CF Access is wired up. This prevents direct-IP access
#       bypassing CF Access authentication entirely.

resource "aws_security_group" "haystack" {
  name        = "haystack-sg"
  # AWS rejects non-ASCII in GroupDescription (the em-dash bit us once).
  description = "Haystack app - SSH (admin only) + HTTPS (Cloudflare)"
  vpc_id      = aws_vpc.main.id

  # SSH — admin only. Override admin_ip_cidr in terraform.tfvars.
  ingress {
    description = "SSH admin access"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.admin_ip_cidr]
  }

  # HTTPS — Cloudflare origin traffic. TODO: restrict to CF IP ranges.
  ingress {
    description = "HTTPS via Cloudflare (TODO: restrict to CF IP ranges)"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # All egress open — scraper needs to reach EU marketplaces, eBay API, SMTP,
  # SSM endpoints, S3, CloudWatch. A fine-grained egress allowlist would
  # require VPC endpoints for AWS services (cost) plus tracking all scraper
  # target IPs (fragile). Open egress is acceptable for a single-user app.
  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "haystack-sg"
  }
}

# ── IAM instance role ─────────────────────────────────────────────────────────
# Minimum-privilege policy:
#   - SSM: read-only on /haystack/prod/* parameters (no ssm:PutParameter)
#   - KMS: Decrypt with the data CMK (encrypt not needed from the instance;
#     AWS Backup + S3 SSE use the CMK via their own service roles)
#   - S3: PutObject + GetObject scoped to the backup bucket paths only
#   - CloudWatch: PutMetricData scoped to Haystack/* namespace
#   - CloudWatch Logs: create + write to /haystack/app log group only
#   - No ec2:*, no iam:*, no wildcards.

resource "aws_iam_role" "haystack_instance" {
  name = "haystack-instance-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "ec2.amazonaws.com" }
        Action    = "sts:AssumeRole"
      }
    ]
  })

  tags = {
    Name = "haystack-instance-role"
  }
}

resource "aws_iam_role_policy" "haystack_instance" {
  name = "haystack-instance-policy"
  role = aws_iam_role.haystack_instance.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # SSM: read secrets at boot and on demand. No PutParameter — that stays
      # on the admin's local machine.
      {
        Sid    = "SSMReadSecrets"
        Effect = "Allow"
        Action = [
          "ssm:GetParameter",
          "ssm:GetParametersByPath"
        ]
        # Covers both /haystack/prod/* (app secrets → .env) and
        # /haystack/tls/* (CF Origin cert + key → Caddy).
        Resource = "arn:aws:ssm:us-west-1:${data.aws_caller_identity.current.account_id}:parameter/haystack/*"
      },
      # KMS: allow the instance to decrypt the data volume (EBS) and any S3
      # objects it reads back. The CMK key policy (in storage.tf) further
      # constrains this to the instance role principal.
      {
        Sid    = "KMSDecryptData"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey" # Required for S3 SSE-KMS PutObject calls
        ]
        Resource = aws_kms_key.data.arn
      },
      # S3: upload backups (listings.db snapshots + FB profile archives) and
      # restore from them. Scoped to the two backup prefixes, not the whole
      # bucket, to prevent accidental object deletion.
      {
        Sid    = "S3BackupReadWrite"
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:GetObject"
        ]
        Resource = [
          "arn:aws:s3:::haystack-backups-${data.aws_caller_identity.current.account_id}/fb-profile/*",
          "arn:aws:s3:::haystack-backups-${data.aws_caller_identity.current.account_id}/sqlite/*"
        ]
      },
      # CloudWatch Metrics: custom metrics from the scraper/app.
      # Namespace condition prevents writing to unrelated namespaces.
      {
        Sid    = "CloudWatchMetrics"
        Effect = "Allow"
        Action = "cloudwatch:PutMetricData"
        Resource = "*" # PutMetricData does not support resource-level permissions;
        # the namespace condition below provides the scoping.
        Condition = {
          StringLike = {
            "cloudwatch:namespace" = "Haystack/*"
          }
        }
      },
      # CloudWatch Logs: write app logs. Scoped to the /haystack/app log group.
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:DescribeLogStreams"
        ]
        Resource = "arn:aws:logs:us-west-1:${data.aws_caller_identity.current.account_id}:log-group:/haystack/app:*"
      }
    ]
  })
}

resource "aws_iam_instance_profile" "haystack" {
  name = "haystack-instance-profile"
  role = aws_iam_role.haystack_instance.name

  tags = {
    Name = "haystack-instance-profile"
  }
}

# ── EC2 instance ──────────────────────────────────────────────────────────────

resource "aws_instance" "haystack" {
  ami           = data.aws_ssm_parameter.al2023_ami.value
  instance_type = "t3.micro"

  # t3 uses T3 Unlimited credit mode by default; the scraper + Playwright
  # workload is bursty-CPU, not sustained. If Playwright OOMs (typically at
  # ~600 MB per Chromium tab), upgrade to t3.small (2 GB RAM, still ~$15/mo).
  # TODO: monitor /haystack/app CloudWatch logs for OOM kills; if seen, run:
  #   aws ec2 modify-instance-attribute --instance-id <id> --instance-type t3.small

  subnet_id                   = aws_subnet.public.id
  vpc_security_group_ids      = [aws_security_group.haystack.id]
  key_name                    = var.ssh_key_name
  iam_instance_profile        = aws_iam_instance_profile.haystack.name
  associate_public_ip_address = false # Using EIP below instead

  # IMDSv2 required — prevents SSRF-based metadata exfiltration. Any code
  # that calls the metadata service must include the X-aws-ec2-metadata-token
  # header (the AWS SDK and CLI do this automatically since 2019).
  metadata_options {
    http_tokens                 = "required"
    http_endpoint               = "enabled"
    http_put_response_hop_limit = 1 # Only the instance itself (hop_limit=1 blocks containers)
  }

  # Root volume: 30 GiB gp3. Default 8 GiB is too small for:
  #   - AL2023 base: ~2 GiB
  #   - Python 3.13 venv + requirements: ~1 GiB
  #   - Playwright Chromium binary: ~350 MB
  #   - Node + npm + built React app: ~300 MB
  #   - OS + dnf cache + headroom: ~2 GiB
  # 30 GiB leaves comfortable headroom at ~$2.40/mo (gp3 $0.08/GiB-mo).
  root_block_device {
    volume_type           = "gp3"
    volume_size           = 30
    encrypted             = true # Encrypt root with default AWS-managed key
    delete_on_termination = true # Root vol is reproducible from AMI + user-data
    tags = {
      Name = "haystack-root"
    }
  }

  # EC2 user-data has a 16 KB raw limit. Our bootstrap script is ~19 KB, so
  # we gzip + base64-encode it; cloud-init on AL2023 auto-detects the gzip
  # header and unpacks transparently. Typical compression on this script is
  # ~3-4x, leaving plenty of headroom for the script to grow.
  user_data_base64 = base64gzip(templatefile("${path.module}/user_data.sh.tftpl", {
    data_device = var.data_volume_device
    repo_url    = var.repo_url
    aws_region  = "us-west-1"
  }))

  # Replacing user-data causes in-place instance replacement (destroy + create).
  # This is intentional: user-data only runs on first boot, so changes to
  # user-data don't take effect on a running instance anyway. A replacement
  # gives us a fresh boot with the new script, which is the correct behavior.
  lifecycle {
    create_before_destroy = true
  }

  tags = {
    Name = "haystack"
  }
}

# ── EBS data volume ───────────────────────────────────────────────────────────
# Separate from the root volume so it survives instance replacement.
# DeleteOnTermination=false is the critical setting: if you accidentally
# terminate the EC2 instance (or terraform destroy is run without -target),
# the data volume is retained in AWS and can be re-attached to a new instance.
# It holds: listings.db, .fb_profile/ (authenticated Playwright session),
# cache/ (image proxy cache). All of these are hard or slow to rebuild.

resource "aws_ebs_volume" "data" {
  availability_zone = "us-west-1a"
  size              = 20
  type              = "gp3"
  encrypted         = true
  kms_key_id        = aws_kms_key.data.arn

  tags = {
    Name = "haystack-data"
  }
}

resource "aws_volume_attachment" "data" {
  device_name  = var.data_volume_device
  volume_id    = aws_ebs_volume.data.id
  instance_id  = aws_instance.haystack.id
  force_detach = false # Prevent data loss from accidental concurrent attach/detach
}

# ── Elastic IP ────────────────────────────────────────────────────────────────
# Static public IP for DNS. The user creates the Cloudflare A record pointing
# to this address (output below). Allocated independently of the instance so
# the IP survives instance replacement without requiring a DNS change.

resource "aws_eip" "haystack" {
  domain = "vpc"

  tags = {
    Name = "haystack-eip"
  }
}

resource "aws_eip_association" "haystack" {
  instance_id   = aws_instance.haystack.id
  allocation_id = aws_eip.haystack.id
}
