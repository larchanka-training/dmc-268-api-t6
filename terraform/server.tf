resource "hcloud_server" "staging" {
  name         = local.name
  server_type  = var.server_type
  image        = "debian-12"
  location     = var.location
  ssh_keys     = [hcloud_ssh_key.ci.id]
  firewall_ids = [hcloud_firewall.staging.id]
  user_data    = templatefile("${path.module}/templates/cloud-init.yaml.tftpl", {})
  labels       = local.labels

  public_net {
    ipv4_enabled = true
    ipv6_enabled = true
  }

  depends_on = [hcloud_network_subnet.staging]
}

resource "hcloud_server_network" "staging" {
  server_id  = hcloud_server.staging.id
  network_id = hcloud_network.staging.id
  ip         = var.server_private_ip
}
