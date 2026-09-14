locals {
  name = "dmc-268-api-${var.environment}"

  labels = {
    project     = "dmc-268"
    team        = "6"
    component   = "api"
    environment = var.environment
    managed_by  = "terraform"
  }

  image_base = "${var.container_registry}/${var.image_repository}"
}
