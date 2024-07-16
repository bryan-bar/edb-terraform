variable "tags" {
  description = "Project Level Tags to be merged with other tags"
  type        = map(string)
  default = {
    cluster_name = "Azure-Cluster-default"
    created_by   = "EDB-Terraform-Azure"
  }
  nullable = true
}

variable "ssh_key" {
  type = object({
    public_path  = optional(string)
    private_path = optional(string)
    output_name  = optional(string, "ssh-id_rsa")
    use_agent    = optional(bool, false)
  })
  default  = {}
  nullable = true
}

variable "images" {
  description = ""
  type = map(object({
    publisher = optional(string)
    offer     = optional(string)
    sku       = optional(string)
    version   = optional(string)
    accept    = optional(bool)
    ssh_user  = optional(string)
  }))
  default  = {}
  nullable = true
}

variable "regions" {
  type = map(object({
    cidr_block = string
    zones = optional(map(object({
      zone = optional(string)
      cidr = optional(string)
    })), {})
    ports = optional(list(object({
      defaults    = optional(string, "")
      port        = optional(number)
      to_port     = optional(number)
      protocol    = string
      description = optional(string, "default")
      type        = optional(string, "ingress")
      access      = optional(string, "allow")
      cidrs       = optional(list(string), [])
    })), [])
  }))
}

variable "machines" {
  type = map(object({
    type          = optional(string)
    image_name    = string
    count         = optional(number, 1)
    region        = string
    zone_name     = string
    instance_type = string
    ssh_port      = optional(number, 22)
    ports = optional(list(object({
      defaults    = optional(string, "")
      port        = optional(number)
      to_port     = optional(number)
      protocol    = string
      description = optional(string, "default")
      type        = optional(string, "ingress")
      access      = optional(string, "allow")
      cidrs       = optional(list(string), [])
      })), []
    )
    volume = object({
      type    = string
      size_gb = number
    })
    additional_volumes = optional(list(object({
      mount_point   = string
      size_gb       = number
      iops          = optional(number)
      type          = string
      filesystem    = optional(string)
      mount_options = optional(string)
    })), [])
    tags = optional(map(string), {})
  }))
  default = {}
}

variable "databases" {
  type = map(object({
    region         = string
    zone           = optional(string)
    dbname         = optional(string)
    engine         = string
    engine_version = number
    instance_type  = string
    username       = string
    password       = string
    volume = object({
      size_gb = optional(number)
    })
    settings = optional(list(object({
      name  = string
      value = number
    })), [])
    tags          = optional(map(string), {})
    public_access = optional(bool, false)
  }))
  default = {}
}

variable "biganimal" {
  type = map(object({
    project = optional(object({
      id = optional(string)
    }), {})
    password = optional(string)
    image = optional(object({
      pg    = optional(string)
      proxy = optional(string)
    }), {})
    data_groups = optional(map(object({
      cloud_account  = optional(bool)
      type           = string
      region         = string
      node_count     = number
      engine         = string
      engine_version = number
      instance_type  = string
      volume = object({
        size_gb    = number
        type       = string
        properties = string
        iops       = optional(number)
        throughput = optional(number)
      })
      wal_volume = optional(object({
        size_gb    = number
        type       = string
        properties = string
        iops       = optional(number)
        throughput = optional(number)
      }))
      pgvector = optional(bool)
      settings = optional(list(object({
        name  = string
        value = string
      })), [])
      allowed_ip_ranges = optional(list(object({
        cidr_block  = string
        description = optional(string, "default description")
      })))
      allowed_machines = optional(list(string))
    })))
    witness_groups = optional(map(object({
      region                 = string
      cloud_account          = optional(bool)
      cloud_service_provider = string
    })), {})
    tags = optional(map(string), {})
  }))
  default = {}
}

variable "kubernetes" {
  type = map(object({
    region                  = string
    ssh_user                = optional(string)
    resource_group_location = optional(string)
    log_analytics_location  = optional(string)
    node_count              = number
    instance_type           = string
    log_analytics_sku       = string
    solution_name           = string
    publisher_name          = string
    tags                    = optional(map(string), {})
  }))
  default = {}
}
