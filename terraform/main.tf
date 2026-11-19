data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

# deploy.ps1 checks: the zone exists, is public, and is the zone for domain.
# With no hosted_zone_id, find the public zone named domain (as deploy.ps1 does).
data "aws_route53_zone" "this" {
  zone_id      = var.hosted_zone_id != "" ? var.hosted_zone_id : null
  name         = var.hosted_zone_id == "" ? var.domain : null
  private_zone = var.hosted_zone_id == "" ? false : null
}

locals {
  account   = data.aws_caller_identity.current.account_id
  partition = data.aws_partition.current.partition
  region    = var.aws_region

  zone_id  = data.aws_route53_zone.this.zone_id
  api_host = "${var.api_host_label}.${var.domain}"
  base_url = "https://${local.api_host}/v1"

  # Bucket names are fixed strings (not resource references) so IAM policies,
  # the Lambda permission and the bucket policy can name them without a
  # dependency cycle - the same reason template.yaml builds them with !Sub.
  bucket = {
    imports     = "${var.bucket_prefix}-imports-${local.account}-${local.region}"
    attachments = "${var.bucket_prefix}-mail-attachments-${local.account}-${local.region}"
    inbound     = "${var.bucket_prefix}-mail-inbound-${local.account}-${local.region}"
    voice       = "${var.bucket_prefix}-voice-${local.account}-${local.region}"
  }
  bucket_arn = { for k, v in local.bucket : k => "arn:${local.partition}:s3:::${v}" }

  # Function names are fixed strings too, so ListsFunction can be told the
  # worker's name without referring to itself.
  fn_name = {
    lists        = "${var.name_prefix}-lists"
    lists_worker = "${var.name_prefix}-lists-worker"
    mail         = "${var.name_prefix}-mail"
    mail_inbox   = "${var.name_prefix}-mail-inbox"
    mail_receive = "${var.name_prefix}-mail-receive"
    webhook      = "${var.name_prefix}-webhook"
    totp         = "${var.name_prefix}-totp"
    lock         = "${var.name_prefix}-lock"
    perf         = "${var.name_prefix}-perf"
    sms          = "${var.name_prefix}-sms"
    voice        = "${var.name_prefix}-voice"
    names        = "${var.name_prefix}-names"
    qr           = "${var.name_prefix}-qr"
    discovery    = "${var.name_prefix}-discovery"
  }

  # SES receipt rule set handling (template.yaml Conditions).
  use_existing_rule_set = var.existing_receipt_rule_set != ""
  make_rule_set         = var.create_receipt_rule && !local.use_existing_rule_set
  activate_rule_set     = local.make_rule_set && var.activate_receipt_rule_set
  rule_set_name         = local.use_existing_rule_set ? var.existing_receipt_rule_set : "${var.name_prefix}-inbound"

  # SMS device keys: given, else the generated one, else none (sms/receive off).
  sms_device_keys = (
    length(var.sms_device_keys) > 0 ? var.sms_device_keys :
    var.generate_sms_device_key ? [random_password.sms_device_key[0].result] : []
  )
}

resource "terraform_data" "zone_check" {
  input = data.aws_route53_zone.this.name

  lifecycle {
    precondition {
      condition     = trimsuffix(data.aws_route53_zone.this.name, ".") == var.domain
      error_message = "hosted_zone_id is the zone for ${data.aws_route53_zone.this.name}, not ${var.domain}."
    }
    precondition {
      condition     = !data.aws_route53_zone.this.private_zone
      error_message = "hosted_zone_id is a private zone; certificate validation and inbound mail need the public zone."
    }
  }
}

# Like deploy.ps1's sms-device-keys.txt: a random key on first apply, then kept.
resource "random_password" "sms_device_key" {
  count   = var.generate_sms_device_key ? 1 : 0
  length  = 32
  special = false
}
