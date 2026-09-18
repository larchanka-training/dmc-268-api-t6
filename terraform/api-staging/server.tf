resource "hcloud_server" "staging" {
  name         = local.name
  server_type  = var.server_type
  image        = "debian-12"
  location     = var.location
  ssh_keys     = local.ssh_keys
  firewall_ids = [hcloud_firewall.staging.id]
  user_data = templatefile("${path.module}/templates/cloud-init.yaml.tftpl", {
    app_dir         = local.app_dir
    bootstrap_name  = local.bootstrap_name
    bootstrap_image = var.bootstrap_image
  })
  labels = local.labels

  public_net {
    ipv4_enabled = true
    ipv4         = hcloud_primary_ip.ipv4.id
    ipv6_enabled = true
    ipv6         = hcloud_primary_ip.ipv6.id
  }

  lifecycle {
    ignore_changes = [user_data]
  }

  depends_on = [hcloud_network_subnet.staging]
}

resource "hcloud_server_network" "staging" {
  server_id  = hcloud_server.staging.id
  network_id = hcloud_network.staging.id
  ip         = var.server_private_ip
}
