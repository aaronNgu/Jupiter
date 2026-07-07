output "alb_dns_name" {
  description = "Hit http(s)://<this>/healthz until a domain exists; CNAME target afterwards."
  value       = aws_lb.main.dns_name
}

output "api_url" {
  value = local.https_enabled ? "https://${local.api_fqdn}" : "http://${aws_lb.main.dns_name}"
}

output "ecr_repository_url" {
  value = aws_ecr_repository.ingestion.repository_url
}

output "s3_bucket" {
  value = aws_s3_bucket.screenshots.bucket
}

output "rds_address" {
  value = aws_db_instance.main.address
}

output "database_url_secret_arn" {
  value = aws_secretsmanager_secret.database_url.arn
}

output "ecs_cluster" {
  value = aws_ecs_cluster.main.name
}

output "ecs_service" {
  value = aws_ecs_service.ingestion.name
}

output "ci_deploy_role_arn" {
  description = "Must match AWS_ROLE_ARN in .github/workflows/deploy.yml."
  value       = aws_iam_role.ci_deploy.arn
}

output "discovery_role_arn" {
  description = "Read-only screenshots role; unused until the discovery service exists."
  value       = aws_iam_role.discovery.arn
}

output "acm_validation_records" {
  description = "With external DNS: create these CNAMEs to validate the cert."
  value = local.https_enabled && !local.manage_dns ? [
    for dvo in aws_acm_certificate.api[0].domain_validation_options : {
      name  = dvo.resource_record_name
      type  = dvo.resource_record_type
      value = dvo.resource_record_value
    }
  ] : []
}
