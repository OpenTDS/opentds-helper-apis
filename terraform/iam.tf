# One role per function family, scoped to its own tables and buckets
# (the same statements as template.yaml).

locals {
  t = { for k, v in aws_dynamodb_table.this : k => v.arn }

  ses_identity_arn = "arn:${local.partition}:ses:${local.region}:${local.account}"

  role_policies = {
    lists = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query", "dynamodb:BatchWriteItem"]
        Resource = [local.t.lists, local.t.lists_cleanup]
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"]
        Resource = [local.t.lists_vars, local.t.lists_counters]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = ["${local.bucket_arn.imports}/imports/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = [local.bucket_arn.imports]
      },
      {
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = ["arn:${local.partition}:lambda:${local.region}:${local.account}:function:${local.fn_name.lists_worker}"]
      },
    ]

    lists_worker = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query", "dynamodb:BatchWriteItem"]
        Resource = [local.t.lists]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = ["${local.bucket_arn.imports}/imports/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = [local.bucket_arn.imports]
      },
    ]

    mail = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:BatchWriteItem"]
        Resource = [local.t.mail]
      },
      {
        Effect = "Allow"
        Action = ["ses:SendEmail", "ses:SendRawEmail"]
        Resource = [
          "${local.ses_identity_arn}:identity/${var.domain}",
          "${local.ses_identity_arn}:identity/*.${var.domain}",
          "${local.ses_identity_arn}:identity/*@${var.domain}",
          "${local.ses_identity_arn}:configuration-set/*",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = ["${local.bucket_arn.attachments}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = [local.bucket_arn.attachments, local.bucket_arn.inbound]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"] # PutObject: attachments/make without a key
        Resource = ["${local.bucket_arn.inbound}/attachments/*"]
      },
    ]

    mail_receive = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:UpdateItem", "dynamodb:PutItem"]
        Resource = [local.t.mail]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = ["${local.bucket_arn.inbound}/inbound/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = ["${local.bucket_arn.inbound}/attachments/*"]
      },
    ]

    webhook = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:Query", "dynamodb:DeleteItem", "dynamodb:BatchWriteItem"]
        Resource = [local.t.webhook]
      },
    ]

    totp = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem"]
        Resource = [local.t.totp]
      },
    ]

    lock = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
        Resource = [aws_dynamodb_table.lock.arn]
      },
    ]

    perf = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:BatchGetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:BatchWriteItem", "dynamodb:Query"]
        Resource = [local.t.perf]
      },
    ]

    # recon also feeds processing lag into a perf key in-process (perfkey=).
    recon = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:BatchGetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:BatchWriteItem", "dynamodb:Query"]
        Resource = [aws_dynamodb_table.recon.arn, "${aws_dynamodb_table.recon.arn}/index/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:UpdateItem"]
        Resource = [local.t.perf]
      },
    ]

    sms = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"]
        Resource = [aws_dynamodb_table.sms.arn, "${aws_dynamodb_table.sms.arn}/index/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["sns:Publish"]
        Resource = ["*"]
      },
    ]

    voice = [
      {
        Effect   = "Allow"
        Action   = ["polly:SynthesizeSpeech", "polly:DescribeVoices"]
        Resource = ["*"]
      },
      # A transcription job reads its audio from, and writes its transcript
      # to, the voice bucket with this role's own S3 permissions (below);
      # Transcribe needs none of its own.
      {
        Effect   = "Allow"
        Action   = ["transcribe:StartTranscriptionJob", "transcribe:GetTranscriptionJob", "transcribe:DeleteTranscriptionJob"]
        Resource = ["*"]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = ["${local.bucket_arn.voice}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = [local.bucket_arn.voice]
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
        Resource = [local.t.voice]
      },
    ]

    # health/check calls the API over HTTPS like any caller: its own table, and
    # read access to the deployment's function configurations (builds).
    health = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem"]
        Resource = [aws_dynamodb_table.health.arn]
      },
      {
        Effect   = "Allow"
        Action   = ["lambda:GetFunctionConfiguration"]
        Resource = ["arn:${local.partition}:lambda:${local.region}:${local.account}:function:${var.name_prefix}-*"]
      },
    ]

    # trace/get reads X-Ray (BatchGetTraces has no resource-level permissions)
    # and shows only this deployment's segments.
    trace = [
      {
        Effect   = "Allow"
        Action   = ["xray:BatchGetTraces"]
        Resource = ["*"]
      },
    ]

    # names, qr, discovery: logs only.
    stateless = []
  }
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "this" {
  for_each           = local.role_policies
  name               = "${var.name_prefix}-${replace(each.key, "_", "-")}"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "logs" {
  for_each   = aws_iam_role.this
  role       = each.value.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy_attachment" "xray" {
  for_each   = var.tracing ? aws_iam_role.this : {}
  role       = each.value.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/AWSXRayDaemonWriteAccess"
}

resource "aws_iam_role_policy" "service" {
  for_each = { for k, v in local.role_policies : k => v if length(v) > 0 }
  name     = "service"
  role     = aws_iam_role.this[each.key].id
  policy   = jsonencode({ Version = "2012-10-17", Statement = each.value })
}
