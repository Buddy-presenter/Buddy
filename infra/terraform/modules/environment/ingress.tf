resource "aws_acm_certificate" "api" {
  count = local.aws_https ? 0 : 1
  domain_name       = var.api_domain
  validation_method = "DNS"
  lifecycle { create_before_destroy = true }
}
resource "aws_route53_record" "certificate" {
  count = local.aws_https ? 0 : 1
  zone_id = var.zone_id
  name    = one(aws_acm_certificate.api[0].domain_validation_options).resource_record_name
  type    = one(aws_acm_certificate.api[0].domain_validation_options).resource_record_type
  ttl     = 60
  records = [one(aws_acm_certificate.api[0].domain_validation_options).resource_record_value]
}
resource "aws_acm_certificate_validation" "api" {
  count = local.aws_https ? 0 : 1
  certificate_arn         = aws_acm_certificate.api[0].arn
  validation_record_fqdns = [aws_route53_record.certificate[0].fqdn]
}
resource "aws_lb" "api" {
  name                       = local.name
  internal                   = local.aws_https
  load_balancer_type         = "application"
  security_groups            = [aws_security_group.ingress.id]
  subnets                    = local.aws_https ? aws_subnet.private[*].id : aws_subnet.public[*].id
  enable_deletion_protection = true
  drop_invalid_header_fields = true
  desync_mitigation_mode     = "strictest"
  idle_timeout               = 120
}
resource "aws_lb_target_group" "api" {
  name                 = local.name
  port                 = 8000
  protocol             = "HTTP"
  vpc_id               = aws_vpc.main.id
  target_type          = "ip"
  deregistration_delay = 30
  health_check {
    path                = "/api/v1/ready"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}
resource "aws_lb_listener" "https" {
  count = local.aws_https ? 0 : 1
  load_balancer_arn = aws_lb.api.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate_validation.api[0].certificate_arn
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}
resource "aws_lb_listener" "redirect" {
  count = local.aws_https ? 0 : 1
  load_balancer_arn = aws_lb.api.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}
resource "aws_route53_record" "api" {
  count = local.aws_https ? 0 : 1
  zone_id = var.zone_id
  name    = var.api_domain
  type    = "A"
  alias {
    name                   = aws_lb.api.dns_name
    zone_id                = aws_lb.api.zone_id
    evaluate_target_health = true
  }
}
resource "aws_wafv2_web_acl_association" "api" {
  count = local.aws_https ? 0 : 1
  resource_arn = aws_lb.api.arn
  web_acl_arn  = aws_wafv2_web_acl.main.arn
}

moved {
  from = aws_acm_certificate.api
  to   = aws_acm_certificate.api[0]
}

moved {
  from = aws_route53_record.certificate
  to   = aws_route53_record.certificate[0]
}

moved {
  from = aws_acm_certificate_validation.api
  to   = aws_acm_certificate_validation.api[0]
}

moved {
  from = aws_lb_listener.https
  to   = aws_lb_listener.https[0]
}

moved {
  from = aws_lb_listener.redirect
  to   = aws_lb_listener.redirect[0]
}

moved {
  from = aws_route53_record.api
  to   = aws_route53_record.api[0]
}

moved {
  from = aws_wafv2_web_acl_association.api
  to   = aws_wafv2_web_acl_association.api[0]
}
