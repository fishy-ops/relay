variable "region" {
  type    = string
  default = "us-east-1"
}

variable "environment" {
  type    = string
  default = "prod"
}

variable "github_repository" {
  description = "owner/repo allowed to deploy through GitHub OIDC"
  type        = string
}

variable "github_branch" {
  type    = string
  default = "main"
}

variable "image_tag" {
  description = "ECR image tag to run. CI sets this to the commit SHA."
  type        = string
  default     = "latest"
}

variable "api_cpu" {
  type    = number
  default = 512
}

variable "api_memory" {
  type    = number
  default = 1024
}

variable "api_desired_count" {
  type    = number
  default = 2
}

variable "worker_cpu" {
  type    = number
  default = 1024
}

variable "worker_memory" {
  type    = number
  default = 2048
}

variable "worker_min_count" {
  type    = number
  default = 1
}

variable "worker_max_count" {
  type    = number
  default = 20
}

variable "backlog_per_worker_target" {
  description = "Queued jobs per running worker that autoscaling aims for"
  type        = number
  default     = 25
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.medium"
}

variable "db_multi_az" {
  type    = bool
  default = true
}

variable "db_deletion_protection" {
  type    = bool
  default = true
}

variable "redis_node_type" {
  type    = string
  default = "cache.t4g.small"
}

variable "certificate_arn" {
  description = "ACM certificate for the HTTPS listener. Leave null to serve HTTP only (dev)."
  type        = string
  default     = null
}

variable "alarm_email" {
  description = "Address that receives CloudWatch alarm notifications"
  type        = string
  default     = null
}

variable "allow_private_webhooks" {
  type    = bool
  default = false
}
