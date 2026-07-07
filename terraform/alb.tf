resource "aws_lb" "main" {
  name               = local.name
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id
}

resource "aws_lb_target_group" "ingestion" {
  name        = "${local.name}-ingestion"
  port        = var.container_port
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = aws_vpc.main.id

  health_check {
    path                = "/healthz"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

# :80 — forwards while no domain/cert exists; becomes a 301 → HTTPS redirect
# the moment domain_name is set.
resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  dynamic "default_action" {
    for_each = local.https_enabled ? [] : [1]

    content {
      type             = "forward"
      target_group_arn = aws_lb_target_group.ingestion.arn
    }
  }

  dynamic "default_action" {
    for_each = local.https_enabled ? [1] : []

    content {
      type = "redirect"

      redirect {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }
  }
}

resource "aws_lb_listener" "https" {
  count = local.https_enabled ? 1 : 0

  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate_validation.api[0].certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.ingestion.arn
  }
}

# --- ACM + DNS (created only once domain_name is set) ------------------------

resource "aws_acm_certificate" "api" {
  count = local.https_enabled ? 1 : 0

  domain_name       = local.api_fqdn
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

# Route53-managed zone: validation + alias records are automatic.
resource "aws_route53_record" "acm_validation" {
  for_each = local.manage_dns ? {
    for dvo in aws_acm_certificate.api[0].domain_validation_options :
    dvo.domain_name => {
      name   = dvo.resource_record_name
      type   = dvo.resource_record_type
      record = dvo.resource_record_value
    }
  } : {}

  zone_id         = var.hosted_zone_id
  name            = each.value.name
  type            = each.value.type
  records         = [each.value.record]
  ttl             = 300
  allow_overwrite = true
}

# With external DNS this waits (up to its timeout) for you to create the
# validation CNAME from the acm_validation_records output.
resource "aws_acm_certificate_validation" "api" {
  count = local.https_enabled ? 1 : 0

  certificate_arn = aws_acm_certificate.api[0].arn
  validation_record_fqdns = local.manage_dns ? [
    for r in aws_route53_record.acm_validation : r.fqdn
  ] : null
}

resource "aws_route53_record" "api" {
  count = local.manage_dns ? 1 : 0

  zone_id = var.hosted_zone_id
  name    = local.api_fqdn
  type    = "A"

  alias {
    name                   = aws_lb.main.dns_name
    zone_id                = aws_lb.main.zone_id
    evaluate_target_health = true
  }
}
