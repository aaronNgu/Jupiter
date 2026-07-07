variable "aws_region" {
  description = "Region everything deploys to."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Prefix for resource names."
  type        = string
  default     = "luminque"
}

# --- DNS / TLS -------------------------------------------------------------
# No domain has been chosen yet, so both default empty and the ALB serves
# plain HTTP on :80. Once a domain exists, set domain_name (and hosted_zone_id
# if the zone is in Route53) and re-apply: that adds the ACM cert, the :443
# listener, and flips :80 to a redirect. Do not enroll agents before then —
# the endpoint URL is baked into their keyrings.

variable "domain_name" {
  description = "Apex domain fronting the service (the cert/record are for api.<domain_name>). Empty = HTTP only, no cert."
  type        = string
  default     = ""
}

variable "hosted_zone_id" {
  description = "Route53 hosted zone id for domain_name. Empty with domain_name set = external DNS; create the validation + api CNAMEs yourself from the outputs."
  type        = string
  default     = ""
}

# --- Data stores -----------------------------------------------------------

variable "retention_days" {
  description = "Days screenshots live in S3 before the lifecycle rule expires them (security baseline: retention is a feature)."
  type        = number
  default     = 30
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "db_allocated_storage" {
  type    = number
  default = 20
}

variable "db_name" {
  type    = string
  default = "luminque"
}

variable "db_username" {
  type    = string
  default = "luminque"
}

variable "db_skip_final_snapshot" {
  description = "Phase 1 default. Flip to false before there is customer data worth keeping."
  type        = bool
  default     = true
}

# --- Compute ---------------------------------------------------------------

variable "task_cpu" {
  type    = number
  default = 256 # 0.25 vCPU
}

variable "task_memory" {
  type    = number
  default = 512
}

variable "desired_count" {
  type    = number
  default = 1
}

variable "container_port" {
  type    = number
  default = 8000
}

variable "image_tag" {
  description = "Stable tag the task definition tracks; CI pushes :sha and :latest and force-redeploys, so applies never change the image."
  type        = string
  default     = "latest"
}

# --- CI --------------------------------------------------------------------

variable "github_org" {
  type    = string
  default = "aaronNgu"
}

variable "github_repo" {
  type    = string
  default = "Jupiter"
}

variable "github_branch" {
  description = "Branch the CI deploy role trusts (deploys run on push to this branch only)."
  type        = string
  default     = "main"
}
