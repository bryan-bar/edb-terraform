variable "machine" {}
variable "public_cidrblocks" {}
variable "service_cidrblocks" {}
variable "internal_cidrblocks" {}
variable "force_ssh_access" {
  description = "Force append a service rule for ssh access"
  default = false
  type = bool
  nullable = false
}
locals {
  ssh_defined = anytrue([for port in var.machine.spec.ports: port.port == var.machine.spec.ssh_port])
  machine_ports = concat(var.machine.spec.ports, (!var.force_ssh_access || local.ssh_defined ? [] : [
        {"access":"allow", "type":"ingress", "defaults":"service", "cidrs": [], "protocol": "tcp", "port": var.machine.spec.ssh_port, "to_port": var.machine.spec.ssh_port, "description": "Force SSH Access"},
        {"access":"allow", "type":"egress", "defaults":"service", "cidrs": [], "protocol": "tcp", "port": var.machine.spec.ssh_port, "to_port": var.machine.spec.ssh_port, "description": "Force SSH Access"},
      ])
    )
}
variable "zone" {}
variable "ssh_priv_key" {}
variable "ssh_pub_key" {}
variable "cluster_name" {}
variable "operating_system" {
  type = object({
    name = optional(string)
    family = optional(string)
    project = optional(string)
    ssh_user = string
    ssh_public_key_file = string
    ssh_private_key_file = string
  })

  validation {
    condition = (
      (var.operating_system.name == null ? 0 : 1) + 
      (var.operating_system.family == null ? 0 : 1) == 1
    )
    error_message = "only one, name or family must be defined"
  }
}
variable "network_name" {}
variable "subnet_name" {}
variable "name_id" { default = "0" }
variable "use_agent" {
  default = false
}
variable "ip_forward" {
  type     = bool
  default  = false
  nullable = false
}
variable "tags" {
  type    = map(string)
  default = {}
}
locals {
  # gcloud label restrictions:
  # - lowercase letters, numeric characters, underscores and dashes
  # - 63 characters max
  # to match other providers as close as possible,
  # we will do any needed handling and continue to treat
  # key-values as tags even though they are labels under gcloud
  labels = { for key,value in var.tags: key => lower(replace(value, ":", "_"))}

  # public ip name and machine name must match regex: "^(?:[a-z](?:[-a-z0-9]{0,61}[a-z0-9])?)$"
  # Handle underscore in machine name
  name = lower(replace(var.machine.name, "_", "-"))
  name_id = lower(replace(var.name_id, "_", "-"))
  cluster_name = lower(replace(var.cluster_name, "_", "-"))
  machine_name = format("%s-%s-%s", local.cluster_name, local.name, local.name_id)
  public_ip_name = format("public-ip-%s-%s", local.machine_name, var.name_id)
}

locals {
  additional_volumes_length = length(lookup(var.machine.spec, "additional_volumes", []))
  additional_volumes_count = local.additional_volumes_length > 0 ? 1 : 0
  additional_volumes_map = { for i, v in lookup(var.machine.spec, "additional_volumes", []) : i => v }

  prefix = "/dev/disk/by-id/google-"
  base = ["sd"]
  letters = [
    "f", "g", "h", "i", 
    "j", "k", "l", "m", 
    "n", "o", "p", "q"
  ]
  # List(List(String))
  # [[ "/dev/disk/by-id/google-sdf" ], ]
  linux_device_names = [
    for letter in local.letters:
      formatlist("${local.prefix}%s${letter}", local.base)
  ]
  # Default filesystem related variables
  filesystem = "xfs"
  mount_options = ["noatime", "nodiratime", "logbsize=256k", "allocsize=1m"]
}
