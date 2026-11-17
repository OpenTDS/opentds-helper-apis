# DynamoDB tables and S3 buckets.
#
# template.yaml marks these DeletionPolicy: Retain, so deleting the stack keeps
# the data. The Terraform equivalent is prevent_destroy: `terraform destroy`
# refuses while they are in the state. To tear down but KEEP the data (what
# CloudFormation does), remove them from the state first - see README.md.

locals {
  # name => { hash = [attr, type], range = [attr, type] | null, ttl = attr | null }
  tables = {
    lists          = { hash = ["key", "S"], range = ["index", "N"], ttl = null }
    lists_vars     = { hash = ["key", "S"], range = ["name", "S"], ttl = null }
    lists_counters = { hash = ["key", "S"], range = ["name", "S"], ttl = null }
    lists_cleanup  = { hash = ["key", "S"], range = ["index", "N"], ttl = null }
    mail           = { hash = ["EmailAddress", "S"], range = ["Serial", "N"], ttl = null }
    webhook        = { hash = ["key", "S"], range = ["seq", "S"], ttl = "ttl" }
    totp           = { hash = ["key", "S"], range = ["user", "S"], ttl = null }
    perf           = { hash = ["PK", "S"], range = ["SK", "S"], ttl = "ttl" }
    # PK "budget" / SK <UTC day>: characters sent to Polly that day.
    voice          = { hash = ["PK", "S"], range = ["SK", "S"], ttl = "ttl" }
  }
}

resource "aws_dynamodb_table" "this" {
  for_each     = local.tables
  name         = "${var.name_prefix}-${replace(each.key, "_", "-")}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = each.value.hash[0]
  range_key    = each.value.range[0]

  attribute {
    name = each.value.hash[0]
    type = each.value.hash[1]
  }
  attribute {
    name = each.value.range[0]
    type = each.value.range[1]
  }

  dynamic "ttl" {
    for_each = each.value.ttl == null ? [] : [each.value.ttl]
    content {
      attribute_name = ttl.value
      enabled        = true
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

# Service = a number's last nine digits; Received = epoch-ms sort key. The
# ByDay index lets sms/list sweep every number with a Query, not a Scan.
resource "aws_dynamodb_table" "sms" {
  name         = "${var.name_prefix}-sms"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "Service"
  range_key    = "Received"

  attribute {
    name = "Service"
    type = "S"
  }
  attribute {
    name = "Received"
    type = "S"
  }
  attribute {
    name = "Day"
    type = "S"
  }

  global_secondary_index {
    name            = "ByDay"
    hash_key        = "Day"
    range_key       = "Received"
    projection_type = "ALL"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  lifecycle {
    prevent_destroy = true
  }
}

# Locks are short-lived; not retained (as in template.yaml).
resource "aws_dynamodb_table" "lock" {
  name         = "${var.name_prefix}-lock"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockKey"

  attribute {
    name = "LockKey"
    type = "S"
  }

  ttl {
    attribute_name = "GcAt"
    enabled        = true
  }
}

# ------------------------------------------------------------------ buckets
resource "aws_s3_bucket" "this" {
  for_each = local.bucket
  bucket   = each.value

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_public_access_block" "this" {
  for_each                = aws_s3_bucket.this
  bucket                  = each.value.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  for_each = aws_s3_bucket.this
  bucket   = each.value.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Uploaded import files (and their .status.json) are kept 90 days so a key can
# be reloaded from the same file by ref.
resource "aws_s3_bucket_lifecycle_configuration" "imports" {
  bucket = aws_s3_bucket.this["imports"].id

  rule {
    id     = "expire-imports"
    status = "Enabled"
    filter {
      prefix = "imports/"
    }
    expiration {
      days = 90
    }
  }

  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "attachments" {
  bucket = aws_s3_bucket.this["attachments"].id

  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }

  # Unprotected copies made by attachments/get?password= - served once, then expire.
  rule {
    id     = "expire-unlocked-copies"
    status = "Enabled"
    filter {
      prefix = "_unlocked/"
    }
    expiration {
      days = 1
    }
  }
}

# SES writes raw mail to inbound/; the receive function extracts attachments
# to attachments/<ref>. Both expire after 7 days.
resource "aws_s3_bucket_lifecycle_configuration" "inbound" {
  bucket = aws_s3_bucket.this["inbound"].id

  rule {
    id     = "expire-raw-mail"
    status = "Enabled"
    filter {
      prefix = "inbound/"
    }
    expiration {
      days = 7
    }
  }

  rule {
    id     = "expire-received-attachments"
    status = "Enabled"
    filter {
      prefix = "attachments/"
    }
    expiration {
      days = 7
    }
  }
}

# voice audio. generated/ is the content-addressed store behind voice/say: it
# expires 30 days after an utterance's LAST use (voice_store rewrites one in
# place when it is used again). saved/ is kept until deleted.
resource "aws_s3_bucket_lifecycle_configuration" "voice" {
  bucket = aws_s3_bucket.this["voice"].id

  rule {
    id     = "expire-generated-utterances"
    status = "Enabled"
    filter {
      prefix = "generated/"
    }
    expiration {
      days = 30
    }
  }

  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

# The upload pages POST files straight to S3 (presigned POST).
resource "aws_s3_bucket_cors_configuration" "imports" {
  bucket = aws_s3_bucket.this["imports"].id

  cors_rule {
    allowed_methods = ["POST"]
    allowed_origins = ["https://${local.api_host}"]
    allowed_headers = ["*"]
    expose_headers  = ["ETag", "Location"]
    max_age_seconds = 3000
  }
}

# Upload page POSTs to S3; large downloads use presigned GETs.
resource "aws_s3_bucket_cors_configuration" "attachments" {
  bucket = aws_s3_bucket.this["attachments"].id

  cors_rule {
    allowed_methods = ["POST", "PUT", "GET", "HEAD"]
    allowed_origins = ["https://${local.api_host}"]
    allowed_headers = ["*"]
    expose_headers  = ["ETag", "Location"]
    max_age_seconds = 3000
  }
}

# Presigned GETs are fetched by test pages on any origin (a page feeding the
# audio to a voice UI), so reads are open to every origin: the signature on
# the URL is the control.
resource "aws_s3_bucket_cors_configuration" "voice" {
  bucket = aws_s3_bucket.this["voice"].id

  cors_rule {
    allowed_methods = ["GET", "HEAD"]
    allowed_origins = ["*"]
    allowed_headers = ["*"]
    expose_headers  = ["ETag"]
    max_age_seconds = 3000
  }
}

resource "aws_s3_bucket_policy" "inbound" {
  bucket = aws_s3_bucket.this["inbound"].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowSesPut"
      Effect    = "Allow"
      Principal = { Service = "ses.amazonaws.com" }
      Action    = "s3:PutObject"
      Resource  = "${local.bucket_arn.inbound}/*"
      Condition = { StringEquals = { "aws:SourceAccount" = local.account } }
    }]
  })

  depends_on = [aws_s3_bucket_public_access_block.this]
}

resource "aws_s3_bucket_notification" "inbound" {
  bucket = aws_s3_bucket.this["inbound"].id

  lambda_function {
    lambda_function_arn = aws_lambda_function.this["mail_receive"].arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "inbound/"
  }

  depends_on = [aws_lambda_permission.mail_receive_s3]
}
