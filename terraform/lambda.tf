# Lambda functions - all from the one code package in ../src, as in
# template.yaml. Timeouts of 29 s match API Gateway's integration limit
# (wait= caps at 20).

data "archive_file" "src" {
  type        = "zip"
  source_dir  = "${path.module}/../src"
  output_path = "${path.module}/.build/src.zip"
  excludes = concat(
    tolist(fileset("${path.module}/../src", "**/__pycache__/**")),
    tolist(fileset("${path.module}/../src", "**/*.pyc")),
  )
}

locals {
  mail_env = {
    BASE_URL          = local.base_url
    MAIL_TABLE        = aws_dynamodb_table.this["mail"].name
    MAIL_DOMAIN       = var.domain
    INBOUND_BUCKET    = local.bucket.inbound
    ATTACHMENT_BUCKET = aws_s3_bucket.this["attachments"].id
    ALLOWED_TO        = join(",", var.email_send_allowed_to)
    ALLOWED_DOMAINS   = join(",", var.email_send_allowed_domains)
  }

  functions = {
    lists = {
      runtime = var.nodejs_runtime, handler = "lists.handler", role = "lists", memory = 512, timeout = 29
      env = {
        BASE_URL        = local.base_url
        LISTS_TABLE     = aws_dynamodb_table.this["lists"].name
        VARS_TABLE      = aws_dynamodb_table.this["lists_vars"].name
        COUNTERS_TABLE  = aws_dynamodb_table.this["lists_counters"].name
        CLEANUP_TABLE   = aws_dynamodb_table.this["lists_cleanup"].name
        IMPORT_BUCKET   = aws_s3_bucket.this["imports"].id
        WORKER_FUNCTION = local.fn_name.lists_worker
      }
    }
    # Background bulk import and shard cleanup, invoked asynchronously by lists.
    lists_worker = {
      runtime = var.nodejs_runtime, handler = "lists_import.worker", role = "lists_worker", memory = 1769, timeout = 900
      env = {
        LISTS_TABLE   = aws_dynamodb_table.this["lists"].name
        IMPORT_BUCKET = aws_s3_bucket.this["imports"].id
      }
    }
    # Send and attachments: PDF/Office extraction and large MIME builds are CPU-bound.
    mail = {
      runtime = var.python_runtime, handler = "mail.handler", role = "mail", memory = 1769, timeout = 29
      env     = local.mail_env
    }
    # Same code, sized for pop/reset/clear - pop?wait=20 holds it open.
    mail_inbox = {
      runtime = var.python_runtime, handler = "mail.handler", role = "mail", memory = 256, timeout = 29
      env     = local.mail_env
    }
    # S3-triggered: parses mail SES stored under inbound/.
    mail_receive = {
      runtime = var.python_runtime, handler = "mail_receive.handler", role = "mail_receive", memory = 1024, timeout = 60
      env = {
        MAIL_TABLE  = aws_dynamodb_table.this["mail"].name
        MAIL_DOMAIN = var.domain
      }
    }
    webhook = {
      runtime = var.python_runtime, handler = "webhook.handler", role = "webhook", memory = 256, timeout = 29
      env = {
        BASE_URL               = local.base_url
        WEBHOOK_TABLE          = aws_dynamodb_table.this["webhook"].name
        WEBHOOK_RETENTION_DAYS = tostring(var.webhook_retention_days)
      }
    }
    totp = {
      runtime = var.python_runtime, handler = "totp.handler", role = "totp", memory = 128, timeout = 10
      env = {
        BASE_URL   = local.base_url
        TOTP_TABLE = aws_dynamodb_table.this["totp"].name
      }
    }
    lock = {
      runtime = var.python_runtime, handler = "lock.handler", role = "lock", memory = 128, timeout = 29
      env = {
        BASE_URL   = local.base_url
        LOCK_TABLE = aws_dynamodb_table.lock.name
      }
    }
    perf = {
      runtime = var.python_runtime, handler = "perf.handler", role = "perf", memory = 512, timeout = 29
      env = {
        BASE_URL   = local.base_url
        PERF_TABLE = aws_dynamodb_table.this["perf"].name
      }
    }
    sms = {
      runtime = var.python_runtime, handler = "sms.handler", role = "sms", memory = 128, timeout = 29
      env = {
        BASE_URL            = local.base_url
        SMS_TABLE           = aws_dynamodb_table.sms.name
        SMS_DEVICE_KEYS     = join(",", local.sms_device_keys)
        SMS_ALLOWED_NUMBERS = join(",", var.sms_send_allowed_numbers)
        SMS_REGION          = var.sms_region
        SMS_SEND_INTERVAL   = tostring(var.sms_send_interval_seconds)
      }
    }
    # Speech comes from Polly in var.voice_region; the audio is stored in this region.
    voice = {
      runtime = var.python_runtime, handler = "voice.handler", role = "voice", memory = 512, timeout = 29
      env = {
        BASE_URL               = local.base_url
        VOICE_BUCKET           = aws_s3_bucket.this["voice"].id
        VOICE_TABLE            = aws_dynamodb_table.this["voice"].name
        VOICE_REGION           = var.voice_region
        VOICE_NAME             = var.voice_name
        VOICE_ENGINE           = var.voice_engine
        VOICE_DAILY_CHARACTERS = tostring(var.voice_daily_characters)
      }
    }
    names = {
      runtime = var.python_runtime, handler = "names.handler", role = "stateless", memory = 256, timeout = 10
      env     = { BASE_URL = local.base_url }
    }
    qr = {
      runtime = var.python_runtime, handler = "qr.handler", role = "stateless", memory = 512, timeout = 29
      env     = { BASE_URL = local.base_url }
    }
    discovery = {
      runtime = var.python_runtime, handler = "discovery.handler", role = "stateless", memory = 256, timeout = 10
      env = {
        BASE_URL    = local.base_url
        MAIL_DOMAIN = var.domain
      }
    }
  }
}

resource "aws_lambda_function" "this" {
  for_each         = local.fn_name
  function_name    = local.fn_name[each.key]
  role             = aws_iam_role.this[local.functions[each.key].role].arn
  runtime          = local.functions[each.key].runtime
  handler          = local.functions[each.key].handler
  memory_size      = local.functions[each.key].memory
  timeout          = local.functions[each.key].timeout
  filename         = data.archive_file.src.output_path
  source_code_hash = data.archive_file.src.output_base64sha256

  environment {
    variables = local.functions[each.key].env
  }

  depends_on = [aws_iam_role_policy_attachment.logs]
}

resource "aws_lambda_function_event_invoke_config" "lists_worker" {
  function_name          = aws_lambda_function.this["lists_worker"].function_name
  qualifier              = "$LATEST"
  maximum_retry_attempts = 0
}

resource "aws_lambda_permission" "mail_receive_s3" {
  statement_id   = "AllowS3Invoke"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.this["mail_receive"].function_name
  principal      = "s3.amazonaws.com"
  source_account = local.account
  source_arn     = local.bucket_arn.inbound
}

# API Gateway may invoke each API-facing function.
locals {
  api_invokers = {
    lists      = "v1"
    names      = "v1"
    lock       = "v1"
    mail       = "v1"
    mail_inbox = "v1"
    sms        = "v1"
    voice      = "v1"
    webhook    = "v1"
    totp       = "v1"
    qr         = "v1"
    perf       = "v1"
  }
}

resource "aws_lambda_permission" "api" {
  for_each      = local.api_invokers
  statement_id  = "AllowApiGatewayV1"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.this[each.key].function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.v1.execution_arn}/*"
}

resource "aws_lambda_permission" "discovery_v1" {
  statement_id  = "AllowApiGatewayV1"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.this["discovery"].function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.v1.execution_arn}/*"
}

resource "aws_lambda_permission" "discovery_root" {
  statement_id  = "AllowApiGatewayRoot"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.this["discovery"].function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.root.execution_arn}/*"
}
