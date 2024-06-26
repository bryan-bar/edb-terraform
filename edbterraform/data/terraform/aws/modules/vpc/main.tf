variable "vpc_cidr_block" {}
variable "name_id" { default = "0" }
variable "tags" {}

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
    }
  }
}

resource "aws_vpc" "main" {
  cidr_block = var.vpc_cidr_block

  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = merge({
    Name = "${var.name_id}"
  }, var.tags)
}

