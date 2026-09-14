resource "hcloud_network" "staging" {
  name     = "${local.name}-net"
  ip_range = var.network_cidr
  labels   = local.labels
}

resource "hcloud_network_subnet" "staging" {
  network_id   = hcloud_network.staging.id
  type         = "cloud"
  network_zone = "eu-central"
  ip_range     = var.subnet_cidr
}
