resource "hcloud_ssh_key" "ci" {
  name       = "${local.name}-ci"
  public_key = var.ssh_public_key
  labels     = local.labels
}
