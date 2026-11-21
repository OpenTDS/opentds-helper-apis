output "base_url" {
  description = "v1 base URL"
  value       = local.base_url
}

output "llms_guide" {
  description = "Point an AI assistant here"
  value       = "${local.base_url}/llms.txt"
}

output "openapi" {
  description = "OpenAPI 3.0.3 (Postman Import > Link)"
  value       = "${local.base_url}/openapi.json"
}

output "site_url" {
  description = "Landing page"
  value       = var.serve_apex_site ? "https://${var.domain}/" : "https://${local.api_host}/"
}

output "mail_domain" {
  description = "Addresses at this domain are caught by mail/pop"
  value       = var.domain
}

output "lists_import_bucket_name" {
  value = aws_s3_bucket.this["imports"].id
}

output "mail_attachment_bucket_name" {
  value = aws_s3_bucket.this["attachments"].id
}

output "mail_inbound_bucket_name" {
  value = aws_s3_bucket.this["inbound"].id
}

output "mail_table_name" {
  value = aws_dynamodb_table.this["mail"].name
}

output "voice_bucket_name" {
  value = aws_s3_bucket.this["voice"].id
}

output "receipt_rule_set" {
  description = "The SES receipt rule set holding this deployment's inbound rule"
  value       = var.create_receipt_rule ? local.rule_set_name : null
}

output "sms_device_key" {
  description = "The generated SMS device key (build it into the capture app). Show it with: terraform output -raw sms_device_key"
  value       = var.generate_sms_device_key ? random_password.sms_device_key[0].result : null
  sensitive   = true
}

output "health_url" {
  description = "End-to-end self-test of this deployment"
  value       = "${local.base_url}/health/check"
}
