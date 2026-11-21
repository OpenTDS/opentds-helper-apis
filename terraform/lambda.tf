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

# The build stamp: every function carries the same BUILD_ID, BUILD_COMMIT and
# DEPLOYED_TIME (X-Build header, health/get; health/check flags a function on
# another build). Unless var.build_id is given, the build is the time the code
# package last changed - time_static is re-created only when the zip changes.
resource "time_static" "build" {
  triggers = {
    src = data.archive_file.src.output_base64sha256
  }
}

locals {
  build_id      = var.build_id != "" ? var.build_id : formatdate("YYYYMMDD.hhmm", time_static.build.rfc3339)
  deployed_time = var.deployed_time != 0 ? var.deployed_time : time_static.build.unix
  build_env = {
    BUILD_ID      = local.build_id
    BUILD_COMMIT  = var.build_commit
    DEPLOYED_TIME = tostring(local.deployed_time)
  }

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
    recon = {
      runtime = var.python_runtime, handler = "recon.handler", role = "recon", memory = 512, timeout = 29
      env = {
        BASE_URL    = local.base_url
        RECON_TABLE = aws_dynamodb_table.recon.name
        PERF_TABLE  = aws_dynamodb_table.this["perf"].name
      }
    }
    sms = {
      runtime = var.python_runtime, handler = "sms.handler", role = "sms", memory = 512, timeout = 29
      env = {
        BASE_URL            = local.base_url
        SMS_TABLE           = aws_dynamodb_table.sms.name
        SMS_DEVICE_KEYS     = join(",", local.sms_device_keys)
        SMS_ALLOWED_NUMBERS = join(",", var.sms_send_allowed_numbers)
        SMS_REGION          = var.sms_region
        SMS_SEND_INTERVAL   = tostring(var.sms_send_interval_seconds)
      }
    }
    # Speech comes from Polly in var.voice_region; the audio is stored in this region,
    # and transcribed (voice/listen) in this region, where the bucket is.
    voice = {
      runtime = var.python_runtime, handler = "voice.handler", role = "voice", memory = 512, timeout = 29
      env = {
        BASE_URL                   = local.base_url
        VOICE_BUCKET               = aws_s3_bucket.this["voice"].id
        VOICE_TABLE                = aws_dynamodb_table.this["voice"].name
        VOICE_REGION               = var.voice_region
        VOICE_NAME                 = var.voice_name
        VOICE_ENGINE               = var.voice_engine
        VOICE_DAILY_CHARACTERS     = tostring(var.voice_daily_characters)
        VOICE_LANGUAGE             = var.voice_language
        VOICE_DAILY_LISTEN_SECONDS = tostring(var.voice_daily_listen_seconds)
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
    # The public view of an X-Ray trace of this deployment (health returns its URL).
    trace = {
      runtime = var.python_runtime, handler = "trace_view.handler", role = "trace", memory = 256, timeout = 29
      env = {
        BASE_URL     = local.base_url
        TRACE_PREFIX = "${var.name_prefix}-"
      }
    }
    # Probes every service through the public API (deadline 20 s, under API
    # Gateway's 29). HEALTH_FUNCTIONS lists the functions whose build it checks.
    health = {
      runtime = var.python_runtime, handler = "health.handler", role = "health", memory = 256, timeout = 29
      env = {
        BASE_URL         = local.base_url
        MAIL_DOMAIN      = var.domain
        HEALTH_TABLE     = aws_dynamodb_table.health.name
        HEALTH_FUNCTIONS = join(",", [for k, n in local.fn_name : "${k}=${n}" if k != "health"])
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
    variables = merge(local.build_env, local.functions[each.key].env)
  }

  tracing_config {
    mode = var.tracing ? "Active" : "PassThrough"
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
    recon      = "v1"
    health     = "v1"
    trace      = "v1"
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
