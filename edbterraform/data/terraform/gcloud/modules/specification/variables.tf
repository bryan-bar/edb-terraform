variable "spec" {
  description = <<-EOT
  Object meant to represent inputs needed to get a valid configuration
  for use with the rest of the cloud provider module collection.
  In most cases:
  * optional() should be used so that null is passed further down for the module to handle
    * Module require null to be handled:
      * set a default if desired: optional(type,default) 
    * Need to set a default after the initial object is set:
      * dynamically set variables with the use of locals and null_resource
      * set an output variable for use with other modules
  * sibling modules should handle most errors with variable validations and preconditions
    so they are caught during terraform plan
  * provider implementations vary and errors might need to be caught eariler, as last resort, 
    use validations and preconditions here for use with terraform plan or postconditions with terraform apply
  EOT
  type = object({
    # Project Level Tags to be merged with other tags
    tags = optional(object({
      cluster_name = optional(string, "gcp-cluster")
      created_by   = optional(string, "edb-terraform")
    }), {})
    ssh_key = optional(object({
      public_path  = optional(string)
      private_path = optional(string)
      output_name  = optional(string, "ssh-id_rsa")
      use_agent    = optional(bool, false)
    }), {})
    images = optional(map(object({
      name = optional(string)
      family = optional(string)
      project = optional(string)
      ssh_user = optional(string)
    })))
    regions = map(object({
      cidr_block = string
      zones      = optional(map(object({
        zone = optional(string)
        cidr = optional(string)
      })), {})
      service_ports = optional(list(object({
        port        = optional(number)
        protocol    = string
        description = string
        ingress_cidrs = optional(list(string), ["0.0.0.0/0"])
        egress_cidrs = optional(list(string))
      })), [])
      region_ports = optional(list(object({
        port        = optional(number)
        protocol    = string
        description = string
        ingress_cidrs = optional(list(string))
        egress_cidrs = optional(list(string))
      })), [])
    }))
    machines = optional(map(object({
      type          = optional(string)
      image_name    = string
      count         = optional(number, 1)
      region        = string
      zone_name     = string
      instance_type = string
      ip_forward    = optional(bool)
      volume = object({
        type      = string
        size_gb   = number
        iops      = optional(number)
        encrypted = optional(bool)
      })
      additional_volumes = optional(list(object({
        mount_point = string
        size_gb     = number
        iops        = optional(number)
        type        = string
        encrypted   = optional(bool)
      })), [])
      tags = optional(map(string), {})
    })), {})
    databases = optional(map(object({
      region         = string
      zone           = string
      engine         = string
      engine_version = number
      instance_type  = string
      dbname         = string
      username       = string
      password       = string
      port           = number
      volume = object({
        size_gb   = number
        type      = string
        iops      = number
        encrypted = bool
      })
      settings = optional(list(object({
        name  = string
        value = number
      })), [])
      tags = optional(map(string), {})
    })), {})
    alloy = optional(map(object({
      region    = string
      cpu_count = number
      password  = string
      settings = optional(list(object({
        name  = string
        value = string
      })), [])
      tags = optional(map(string), {})
    })), {})
    kubernetes = optional(map(object({
      region        = string
      zone_name     = string
      node_count    = number
      instance_type = string
      tags          = optional(map(string), {})
    })), {})
  })

  validation {
    condition = (
      alltrue([
        for machine in var.spec.machines :
        length(machine.additional_volumes) == 0 ||
        anytrue([for service_port in var.spec.regions[machine.region].service_ports :
          service_port.port == 22
        ])
      ])
    )
    error_message = (
      <<-EOT
When using machines with additional volumes, SSH must be open.
Ensure each region listed below has port 22 open under service_ports.
Region - Machine:
%{for name, spec in var.spec.machines~}
%{if length(spec.additional_volumes) != 0~}
  ${spec.region} - ${name}
%{endif~}
%{endfor~}
EOT
    )
  }
}