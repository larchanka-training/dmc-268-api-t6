provider "hcloud" {
  token = var.hcloud_token != "" ? var.hcloud_token : null
}
