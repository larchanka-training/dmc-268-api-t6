variable "hcloud_token" {
  type        = string
  description = "Hetzner Cloud API token. Prefer HCLOUD_TOKEN in the environment."
  sensitive   = true
  default     = ""
}

variable "environment" {
  type        = string
  description = "Deployment environment name."
  default     = "staging"
}

variable "location" {
  type        = string
  description = "Hetzner Cloud location."
  default     = "nbg1"
}

variable "network_zone" {
  type        = string
  description = "Hetzner Cloud network zone for the private subnet."
  default     = "eu-central"
}

variable "server_type" {
  type        = string
  description = "Hetzner Cloud server type for staging."
  default     = "cx22"
}

variable "ssh_public_key" {
  type        = string
  description = "Public SSH key used by CI to deploy."
}

variable "ssh_operator_public_key" {
  type        = string
  description = "Optional extra public SSH key for operator access."
  default     = ""
}

variable "ssh_allowed_cidrs" {
  type        = list(string)
  description = "CIDR ranges allowed to reach SSH. World-open by design: GitHub-hosted runners have no stable egress IPs, so SSH relies on key-only auth, fail2ban and a non-standard port."
  default     = ["0.0.0.0/0", "::/0"]

  validation {
    condition     = length(var.ssh_allowed_cidrs) > 0
    error_message = "Provide at least one CIDR allowed to use SSH."
  }
}

variable "ssh_port" {
  type        = number
  description = "TCP port sshd listens on. Must match the GitHub variable STAGING_SSH_PORT."
  default     = 22022

  validation {
    condition     = var.ssh_port > 1024 && var.ssh_port < 32768 && floor(var.ssh_port) == var.ssh_port
    error_message = "ssh_port must be an integer between 1025 and 32767 (below the Linux ephemeral port range)."
  }
}

variable "container_registry" {
  type        = string
  description = "OCI registry host that stores application images."
  default     = "ghcr.io"
}

variable "image_repository" {
  type        = string
  description = "Image repository path without registry host."
  default     = "larchanka-training/dmc-268-ui-t6"
}

variable "bootstrap_image" {
  type        = string
  description = "Public image started by cloud-init until CI deploys the API."
  default     = "nginx:1.27-alpine"
}

variable "network_cidr" {
  type        = string
  description = "Private network CIDR for the staging stack."
  default     = "10.20.0.0/16"
}

variable "subnet_cidr" {
  type        = string
  description = "Private subnet CIDR for the staging stack."
  default     = "10.20.1.0/24"
}

variable "server_private_ip" {
  type        = string
  description = "Static private IPv4 of the staging VM."
  default     = "10.20.1.10"
}

variable "dns_zone" {
  type        = string
  description = "Hetzner Cloud DNS zone (example.com). Empty skips DNS records."
  default     = ""
}

variable "create_dns_zone" {
  type        = bool
  description = "Create the DNS zone. Set false to attach records to an existing zone."
  default     = true
}

variable "dns_record_name" {
  type        = string
  description = "RRSet name inside dns_zone (ui-staging → ui-staging.example.com)."
  default     = "ui-staging"
}

variable "dns_ttl" {
  type        = number
  description = "TTL for the UI A/AAAA records."
  default     = 300
}
