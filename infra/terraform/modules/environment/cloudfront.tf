# AWS owns the public HTTPS certificate. The origin is an internal ALB reached
# through CloudFront-managed ENIs, never an internet-facing HTTP origin.
resource "aws_lb_listener" "private" {
  count             = local.aws_https ? 1 : 0
  load_balancer_arn = aws_lb.api.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}
resource "aws_cloudfront_vpc_origin" "api" {
  count = local.aws_https ? 1 : 0
  vpc_origin_endpoint_config {
    name                   = local.name
    arn                    = aws_lb.api.arn
    http_port              = 80
    https_port             = 443
    origin_protocol_policy = "http-only"
    origin_ssl_protocols {
      items    = ["TLSv1.2"]
      quantity = 1
    }
  }
  depends_on = [aws_lb_listener.private, aws_vpc_security_group_ingress_rule.cloudfront, aws_internet_gateway.main]
}

# Supplying a team domain later adds an alias to the same distribution. Existing
# AWS URLs keep working and no database or API service replacement is required.
resource "aws_acm_certificate" "edge" {
  count             = local.aws_https && var.api_domain != null ? 1 : 0
  provider          = aws.edge
  domain_name       = var.api_domain
  validation_method = "DNS"
  lifecycle { create_before_destroy = true }
}
resource "aws_route53_record" "edge_certificate" {
  count   = local.aws_https && var.api_domain != null ? 1 : 0
  zone_id = var.zone_id
  name    = one(aws_acm_certificate.edge[0].domain_validation_options).resource_record_name
  type    = one(aws_acm_certificate.edge[0].domain_validation_options).resource_record_type
  ttl     = 60
  records = [one(aws_acm_certificate.edge[0].domain_validation_options).resource_record_value]
}
resource "aws_acm_certificate_validation" "edge" {
  count                   = local.aws_https && var.api_domain != null ? 1 : 0
  provider                = aws.edge
  certificate_arn         = aws_acm_certificate.edge[0].arn
  validation_record_fqdns = [aws_route53_record.edge_certificate[0].fqdn]
}
resource "aws_cloudfront_distribution" "api" {
  count           = local.aws_https ? 1 : 0
  enabled         = true
  is_ipv6_enabled = true
  comment         = "${local.name} API; private origin; no response caching"
  aliases         = var.api_domain == null ? [] : [var.api_domain]
  price_class     = "PriceClass_100"
  web_acl_id      = aws_wafv2_web_acl.edge[0].arn
  origin {
    domain_name = aws_lb.api.dns_name
    origin_id   = local.name
    vpc_origin_config {
      vpc_origin_id            = aws_cloudfront_vpc_origin.api[0].id
      origin_read_timeout      = 60
      origin_keepalive_timeout = 5
    }
  }
  default_cache_behavior {
    target_origin_id       = local.name
    viewer_protocol_policy = "https-only"
    allowed_methods        = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods         = ["GET", "HEAD"]
    compress               = false
    min_ttl                = 0
    default_ttl            = 0
    max_ttl                = 0
    # Forward authentication, query strings and cookies. A zero minimum TTL is
    # mandatory: authenticated responses and event streams must never be cached.
    forwarded_values {
      query_string = true
      headers      = ["*"]
      cookies { forward = "all" }
    }
  }
  dynamic "custom_error_response" {
    for_each = toset([400, 403, 404, 405, 414, 416, 500, 501, 502, 503, 504])
    content {
      error_code            = custom_error_response.value
      error_caching_min_ttl = 0
    }
  }
  restrictions {
    geo_restriction { restriction_type = "none" }
  }
  viewer_certificate {
    cloudfront_default_certificate = var.api_domain == null
    acm_certificate_arn             = var.api_domain == null ? null : aws_acm_certificate_validation.edge[0].certificate_arn
    ssl_support_method              = var.api_domain == null ? null : "sni-only"
    minimum_protocol_version        = var.api_domain == null ? "TLSv1" : "TLSv1.2_2021"
  }
}
resource "aws_route53_record" "edge_api" {
  count   = local.aws_https && var.api_domain != null ? 1 : 0
  zone_id = var.zone_id
  name    = var.api_domain
  type    = "A"
  alias {
    name                   = aws_cloudfront_distribution.api[0].domain_name
    zone_id                = aws_cloudfront_distribution.api[0].hosted_zone_id
    evaluate_target_health = false
  }
}

# Apply rate limits at the edge, where AWS WAF sees the real viewer IP instead
# of aggregating every client behind CloudFront's origin connection addresses.
resource "aws_wafv2_web_acl" "edge" {
  count    = local.aws_https ? 1 : 0
  provider = aws.edge
  name     = "${local.name}-edge"
  scope    = "CLOUDFRONT"
  default_action {
    allow {}
  }
  rule {
    name     = "per-ip-rate-limit"
    priority = 1
    action {
      block {}
    }
    statement {
      rate_based_statement {
        limit                 = 300
        aggregate_key_type    = "IP"
        evaluation_window_sec = 60
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-edge-rate"
      sampled_requests_enabled   = false
    }
  }
  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${local.name}-edge-waf"
    sampled_requests_enabled   = false
  }
}
