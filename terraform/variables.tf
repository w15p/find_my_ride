variable "admin_ip_cidr" {
  description = <<-EOT
    CIDR block allowed SSH access (port 22) to the instance.
    !! IMPORTANT !! The default 0.0.0.0/0 opens SSH to the world.
    Override this in terraform.tfvars with your actual public IP:
      admin_ip_cidr = "203.0.113.42/32"
    Find your IP: curl -s https://checkip.amazonaws.com
  EOT
  type        = string
  default     = "0.0.0.0/0" # INSECURE DEFAULT — override in terraform.tfvars
}

variable "ssh_key_name" {
  description = <<-EOT
    Name of an existing EC2 key pair in us-west-1 for SSH access.
    Create one at: AWS Console → EC2 → Key Pairs → Create key pair
    Or via CLI:
      aws ec2 create-key-pair --key-name haystack --region us-west-1 \
        --query 'KeyMaterial' --output text > ~/.ssh/haystack.pem
      chmod 600 ~/.ssh/haystack.pem
  EOT
  type        = string
}

variable "notification_email" {
  description = "Email address for AWS Budget cost alerts (50%, 80%, 100% thresholds)."
  type        = string
}

variable "repo_url" {
  description = <<-EOT
    Git remote URL for the haystack application repository.
    Used by user-data to clone the app on first boot. Public repo over
    HTTPS — no auth needed. If the repo becomes private, switch to git+ssh
    and provision a deploy key under /haystack/tls/DEPLOY_KEY.
  EOT
  type        = string
  default     = "https://github.com/w15p/find_my_ride.git"
}

variable "monthly_budget_usd" {
  description = "Monthly budget threshold in USD. Budget alerts fire at 50%, 80%, and 100%."
  type        = number
  default     = 10
}

variable "data_volume_device" {
  description = <<-EOT
    Block device name for the 20 GiB data EBS volume.
    NVMe instances (t3, c5, m5) expose all volumes as /dev/nvme*n1 regardless
    of what is set here; the user-data script uses lsblk to find the correct
    device dynamically. This value tells the EC2 API which logical slot to
    occupy (/dev/sdf → /dev/nvme1n1 on NVMe-capable instance types).
  EOT
  type        = string
  default     = "/dev/sdf"
}
