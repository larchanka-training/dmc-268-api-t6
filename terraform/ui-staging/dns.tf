data "hcloud_zone" "staging" {
  count = local.dns_enabled && !var.create_dns_zone ? 1 : 0
  name  = var.dns_zone
}

resource "hcloud_zone" "staging" {
  count  = local.dns_enabled && var.create_dns_zone ? 1 : 0
  name   = var.dns_zone
  mode   = "primary"
  ttl    = var.dns_ttl
  labels = local.labels
}

resource "hcloud_zone_rrset" "ui_a" {
  count = local.dns_enabled ? 1 : 0
  zone  = local.zone_name
  name  = var.dns_record_name
  type  = "A"
  ttl   = var.dns_ttl

  records = [
    {
      value   = hcloud_primary_ip.ipv4.ip_address
      comment = local.name
    },
  ]
}

resource "hcloud_zone_rrset" "ui_aaaa" {
  count = local.dns_enabled ? 1 : 0
  zone  = local.zone_name
  name  = var.dns_record_name
  type  = "AAAA"
  ttl   = var.dns_ttl

  records = [
    {
      value   = hcloud_server.staging.ipv6_address
      comment = local.name
    },
  ]
}

resource "hcloud_rdns" "ipv4" {
  count      = local.fqdn != "" ? 1 : 0
  server_id  = hcloud_server.staging.id
  ip_address = hcloud_server.staging.ipv4_address
  dns_ptr    = local.fqdn
}

resource "hcloud_rdns" "ipv6" {
  count      = local.fqdn != "" ? 1 : 0
  server_id  = hcloud_server.staging.id
  ip_address = hcloud_server.staging.ipv6_address
  dns_ptr    = local.fqdn
}
