resource "hcloud_ssh_key" "ci" {
  name       = "${local.name}-ci"
  public_key = var.ssh_public_key
  labels     = local.labels
}

resource "hcloud_ssh_key" "operator" {
  count      = var.ssh_operator_public_key != "" ? 1 : 0
  name       = "${local.name}-operator"
  public_key = var.ssh_operator_public_key
  labels     = local.labels
}
