output "environment" {
  description = "Environment name."
  value       = var.environment
}

output "server_id" {
  description = "Hetzner Cloud ID of the staging VM."
  value       = hcloud_server.staging.id
}

output "server_name" {
  description = "Name of the staging VM."
  value       = hcloud_server.staging.name
}

output "network_id" {
  description = "Private network ID."
  value       = hcloud_network.staging.id
}

output "subnet_id" {
  description = "Private subnet ID."
  value       = hcloud_network_subnet.staging.id
}

output "firewall_id" {
  description = "Firewall ID attached to the staging VM."
  value       = hcloud_firewall.staging.id
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
  description = "Public IPv4 of the staging VM. Survives server rebuilds."
  value       = hcloud_primary_ip.ipv4.ip_address
}

output "staging_ipv6" {
  description = "Public IPv6 of the staging VM."
  value       = hcloud_server.staging.ipv6_address
}

output "staging_private_ip" {
  description = "Private IPv4 of the staging VM."
  value       = hcloud_server_network.staging.ip
}

output "dns_fqdn" {
  description = "Public hostname when DNS is configured."
  value       = local.fqdn
}

output "dns_nameservers" {
  description = "Hetzner nameservers to publish at the registrar when the zone is created here."
  value       = try(hcloud_zone.staging[0].authoritative_nameservers.assigned, [])
}

output "health_url" {
  description = "HTTP health-check URL after a successful deploy."
  value       = "http://${local.health_host}/health"
}

output "ssh_host" {
  description = "Host to use for SSH and GitHub secret STAGING_HOST."
  value       = local.health_host
}
