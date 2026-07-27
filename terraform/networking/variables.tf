variable "aws_region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "us-west-2"
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC."
  type        = string
  default     = "10.0.0.0/16"
}

variable "azs" {
  description = "Availability zones to spread subnets across. RDS Multi-AZ requires at least 2."
  type        = list(string)
  default     = ["us-west-2a", "us-west-2b"]
}

variable "single_nat_gateway" {
  description = "Use one shared NAT gateway instead of one per AZ. Cheaper (~$32/mo vs ~$64/mo), at the cost of a single point of failure if that AZ has an outage -- reasonable for this project's current stage."
  type        = bool
  default     = true
}
