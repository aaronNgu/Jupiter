data "aws_caller_identity" "current" {}

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  name = var.project_name
  azs  = slice(data.aws_availability_zones.available.names, 0, 2)

  https_enabled = var.domain_name != ""
  manage_dns    = var.domain_name != "" && var.hosted_zone_id != ""
  api_fqdn      = local.https_enabled ? "api.${var.domain_name}" : ""

  # Globally-unique bucket name without hardcoding the account id in .tf.
  bucket_name = "${var.project_name}-screenshots-${data.aws_caller_identity.current.account_id}"
}
