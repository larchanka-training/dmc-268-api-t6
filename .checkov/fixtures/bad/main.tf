# Deliberately insecure fixture: every custom policy in .checkov/policies must FAIL here.
# Excluded from the normal scan via skip-path in .checkov.yaml.

terraform {
  required_version = ">= 1.10.0"

  required_providers {
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "~> 1.54"
    }
  }
}

# CKV2_HCLOUD_1 and CKV2_HCLOUD_3: no firewall_ids, no ssh_keys.
resource "hcloud_server" "no_firewall_no_keys" {
  name        = "fixture-bare"
  server_type = "cx22"
  image       = "debian-12"
}

# CKV2_HCLOUD_1 and CKV2_HCLOUD_3: empty lists.
resource "hcloud_server" "empty_lists" {
  name         = "fixture-empty"
  server_type  = "cx22"
  image        = "debian-12"
  firewall_ids = []
  ssh_keys     = []
}

# CKV2_HCLOUD_2: port 22 open to the whole IPv4 internet.
resource "hcloud_firewall" "ssh22_ipv4" {
  name = "fixture-ssh22-ipv4"

  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "22"
    source_ips = ["0.0.0.0/0"]
  }
}

# CKV2_HCLOUD_2: port 22 (as a number) open to the whole IPv6 internet next to a restricted rule.
resource "hcloud_firewall" "ssh22_ipv6" {
  name = "fixture-ssh22-ipv6"

  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = 22
    source_ips = ["203.0.113.10/32", "::/0"]
  }
}
