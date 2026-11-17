# SES: identity (Easy DKIM + custom MAIL FROM), DNS, inbound receipt rule.
# Verification is asynchronous: sending/receiving start once SES sees the DKIM
# records, usually minutes after apply. A new account is in the SES sandbox
# (verified recipients only) until production access is granted - see README.

resource "aws_sesv2_email_identity" "domain" {
  count          = var.create_ses_identity ? 1 : 0
  email_identity = var.domain

  dkim_signing_attributes {
    next_signing_key_length = "RSA_2048_BIT"
  }
}

resource "aws_sesv2_email_identity_mail_from_attributes" "domain" {
  count                  = var.create_ses_identity ? 1 : 0
  email_identity         = aws_sesv2_email_identity.domain[0].email_identity
  mail_from_domain       = "mail.${var.domain}"
  behavior_on_mx_failure = "USE_DEFAULT_VALUE"
}

# Easy DKIM always issues three tokens; count (not for_each) because the
# tokens are only known after the identity exists.
resource "aws_route53_record" "dkim" {
  count   = var.create_ses_identity ? 3 : 0
  zone_id = var.hosted_zone_id
  name    = "${aws_sesv2_email_identity.domain[0].dkim_signing_attributes[0].tokens[count.index]}._domainkey.${var.domain}"
  type    = "CNAME"
  ttl     = 300
  records = ["${aws_sesv2_email_identity.domain[0].dkim_signing_attributes[0].tokens[count.index]}.dkim.amazonses.com"]
}

resource "aws_route53_record" "inbound_mx" {
  count   = var.manage_mail_dns_records ? 1 : 0
  zone_id = var.hosted_zone_id
  name    = var.domain
  type    = "MX"
  ttl     = 300
  records = ["10 inbound-smtp.${local.region}.amazonaws.com"]
}

# MAIL FROM subdomain: bounces/complaints come from mail.<domain> so SPF
# aligns. SES forbids receiving on it, hence the feedback MX.
resource "aws_route53_record" "mail_from_mx" {
  count   = var.manage_mail_dns_records ? 1 : 0
  zone_id = var.hosted_zone_id
  name    = "mail.${var.domain}"
  type    = "MX"
  ttl     = 300
  records = ["10 feedback-smtp.${local.region}.amazonses.com"]
}

resource "aws_route53_record" "mail_from_spf" {
  count   = var.manage_mail_dns_records ? 1 : 0
  zone_id = var.hosted_zone_id
  name    = "mail.${var.domain}"
  type    = "TXT"
  ttl     = 300
  records = ["v=spf1 include:amazonses.com ~all"]
}

resource "aws_route53_record" "spf" {
  count   = var.manage_spf_dmarc_records ? 1 : 0
  zone_id = var.hosted_zone_id
  name    = var.domain
  type    = "TXT"
  ttl     = 300
  records = ["v=spf1 include:amazonses.com ~all"]
}

resource "aws_route53_record" "dmarc" {
  count   = var.manage_spf_dmarc_records ? 1 : 0
  zone_id = var.hosted_zone_id
  name    = "_dmarc.${var.domain}"
  type    = "TXT"
  ttl     = 300
  records = ["v=DMARC1; p=${var.dmarc_policy};"]
}

# ------------------------------------------------------------------ receiving
resource "aws_ses_receipt_rule_set" "inbound" {
  count         = local.make_rule_set ? 1 : 0
  rule_set_name = local.rule_set_name
}

# Stores every message for <domain> under inbound/ in the inbound bucket. The
# bucket policy must exist first or SES rejects the action.
resource "aws_ses_receipt_rule" "store" {
  count         = var.create_receipt_rule ? 1 : 0
  name          = "${var.name_prefix}-store-to-s3"
  rule_set_name = local.make_rule_set ? aws_ses_receipt_rule_set.inbound[0].rule_set_name : var.existing_receipt_rule_set
  recipients    = [var.domain]
  enabled       = true
  scan_enabled  = true
  tls_policy    = "Optional"

  s3_action {
    bucket_name       = aws_s3_bucket.this["inbound"].id
    object_key_prefix = "inbound/"
    position          = 1
  }

  depends_on = [aws_s3_bucket_policy.inbound]
}

# Only one rule set can be active per account and region. template.yaml uses
# a custom resource that refuses to replace a different active set; Terraform
# activates directly, so this check warns at plan time instead - read the plan
# before you approve it. (When no set is active the lookup fails, which a
# check block reports as a harmless warning.)
check "active_receipt_rule_set" {
  data "aws_ses_active_receipt_rule_set" "current" {}

  assert {
    condition = (
      !local.activate_rule_set ||
      data.aws_ses_active_receipt_rule_set.current.rule_set_name == local.rule_set_name
    )
    error_message = <<-EOT
      Receipt rule set '${data.aws_ses_active_receipt_rule_set.current.rule_set_name}' is already
      ACTIVE in ${local.region}. Applying will replace it with '${local.rule_set_name}' and stop
      whatever inbound mail depends on it. To keep it, set
      existing_receipt_rule_set = "${data.aws_ses_active_receipt_rule_set.current.rule_set_name}"
      (adds this rule to that set) or activate_receipt_rule_set = false.
    EOT
  }
}

resource "aws_ses_active_receipt_rule_set" "this" {
  count         = local.activate_rule_set ? 1 : 0
  rule_set_name = aws_ses_receipt_rule_set.inbound[0].rule_set_name

  depends_on = [aws_ses_receipt_rule.store]
}
