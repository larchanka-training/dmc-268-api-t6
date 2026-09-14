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

variable "server_type" {
  type        = string
  description = "Hetzner Cloud server type for staging."
  default     = "cx22"
}

variable "ssh_public_key" {
  type        = string
  description = "Public SSH key used by CI to deploy."
}

variable "ssh_allowed_cidrs" {
  type        = list(string)
  description = "CIDR ranges allowed to reach SSH. Restrict to the CI egress / office network in production."

  validation {
    condition     = length(var.ssh_allowed_cidrs) > 0
    error_message = "Provide at least one CIDR allowed to use SSH."
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
  default     = "larchanka-training/dmc-268-api-t6"
}

variable "network_cidr" {
  type        = string
  description = "Private network CIDR for the staging stack."
  default     = "10.21.0.0/16"
}

variable "subnet_cidr" {
  type        = string
  description = "Private subnet CIDR for the staging stack."
  default     = "10.21.1.0/24"
}

variable "server_private_ip" {
  type        = string
  description = "Static private IPv4 of the staging VM."
  default     = "10.21.1.10"
}
