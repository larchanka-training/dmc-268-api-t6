locals {
  name = "dmc-268-ui-${var.environment}"

  labels = {
    project     = "dmc-268"
    team        = "6"
    component   = "ui"
    environment = var.environment
    managed_by  = "terraform"
  }

  image_base = "${var.container_registry}/${var.image_repository}"
  app_dir    = "/opt/dmc-268-ui"

  bootstrap_name = "dmc-268-ui-bootstrap"
  dns_enabled    = var.dns_zone != ""

  zone_name = local.dns_enabled ? (
    var.create_dns_zone ? hcloud_zone.staging[0].name : data.hcloud_zone.staging[0].name
  ) : ""

  fqdn = local.dns_enabled ? (
    var.dns_record_name == "@" ? var.dns_zone : "${var.dns_record_name}.${var.dns_zone}"
  ) : ""

  health_host = local.fqdn != "" ? local.fqdn : hcloud_server.staging.ipv4_address
  ssh_keys = concat(
    [hcloud_ssh_key.ci.id],
    var.ssh_operator_public_key != "" ? [hcloud_ssh_key.operator[0].id] : []
  )
}
