output "environment" {
  description = "Environment name."
  value       = var.environment
}

output "container_registry" {
  description = "Configured OCI registry host."
  value       = var.container_registry
}

output "image_repository" {
  description = "Fully qualified image repository without a tag."
  value       = local.image_base
}

output "staging_ipv4" {
  description = "Public IPv4 of the staging VM."
  value       = hcloud_server.staging.ipv4_address
}

output "staging_ipv6" {
  description = "Public IPv6 of the staging VM."
  value       = hcloud_server.staging.ipv6_address
}

output "staging_private_ip" {
  description = "Private IPv4 of the staging VM."
  value       = hcloud_server_network.staging.ip
}

output "health_url" {
  description = "HTTP health-check URL after a successful deploy."
  value       = "http://${hcloud_server.staging.ipv4_address}/healthcheck"
}
