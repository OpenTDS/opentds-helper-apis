# Certificates, custom domains, DNS and base-path mappings.
#   https://<api_host>/      -> root API (landing page, /llms.txt, /.well-known/*)
#   https://<api_host>/v1/   -> v1 API
#   https://<domain>/        -> root API too, when serve_apex_site = true

locals {
  certs = merge(
    { api = local.api_host },
    var.serve_apex_site ? { apex = var.domain } : {}
  )
}

resource "aws_acm_certificate" "this" {
  for_each          = local.certs
  domain_name       = each.value
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

# CloudFormation creates these validation records itself (DomainValidationOptions
# with HostedZoneId); Terraform needs them spelled out.
resource "aws_route53_record" "cert_validation" {
  for_each = {
    for c in flatten([
      for k, cert in aws_acm_certificate.this : [
        for o in cert.domain_validation_options : {
          key   = "${k}:${o.domain_name}"
          name  = o.resource_record_name
          type  = o.resource_record_type
          value = o.resource_record_value
        }
      ]
    ]) : c.key => c
  }

  zone_id         = local.zone_id
  name            = each.value.name
  type            = each.value.type
  records         = [each.value.value]
  ttl             = 300
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "this" {
  for_each                = aws_acm_certificate.this
  certificate_arn         = each.value.arn
  validation_record_fqdns = [for k, r in aws_route53_record.cert_validation : r.fqdn if startswith(k, "${each.key}:")]
}

resource "aws_api_gateway_domain_name" "this" {
  for_each                 = local.certs
  domain_name              = each.value
  regional_certificate_arn = aws_acm_certificate_validation.this[each.key].certificate_arn
  security_policy          = "TLS_1_2"

  endpoint_configuration {
    types = ["REGIONAL"]
  }
}

resource "aws_route53_record" "domain" {
  for_each = aws_api_gateway_domain_name.this
  zone_id  = local.zone_id
  name     = each.value.domain_name
  type     = "A"

  alias {
    name                   = each.value.regional_domain_name
    zone_id                = each.value.regional_zone_id
    evaluate_target_health = false
  }
}

resource "aws_api_gateway_base_path_mapping" "root" {
  for_each    = aws_api_gateway_domain_name.this
  domain_name = each.value.domain_name
  api_id      = aws_api_gateway_rest_api.root.id
  stage_name  = aws_api_gateway_stage.root.stage_name
}

# After the root mapping: API Gateway rejects concurrent updates to one custom domain.
resource "aws_api_gateway_base_path_mapping" "v1" {
  domain_name = aws_api_gateway_domain_name.this["api"].domain_name
  base_path   = "v1"
  api_id      = aws_api_gateway_rest_api.v1.id
  stage_name  = aws_api_gateway_stage.v1.stage_name

  depends_on = [aws_api_gateway_base_path_mapping.root]
}
