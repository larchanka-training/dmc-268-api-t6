resource "hcloud_primary_ip" "ipv4" {
  name        = "${local.name}-ipv4"
  location    = var.location
  type        = "ipv4"
  auto_delete = false
  labels      = local.labels
}

resource "hcloud_primary_ip" "ipv6" {
  name        = "${local.name}-ipv6"
  location    = var.location
  type        = "ipv6"
  auto_delete = false
  labels      = local.labels
}
