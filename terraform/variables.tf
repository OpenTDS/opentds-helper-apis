# Variables mirror the CloudFormation parameters (snake_case), plus
# aws_region / name_prefix / tags. The checks deploy.ps1 makes before a
# CloudFormation deploy are validations and preconditions here.

# ------------------------------------------------------------------ domain
variable "aws_region" {
  description = "Region to deploy to. It must support SES email RECEIVING (e.g. us-east-1, us-west-2, eu-west-1, ap-southeast-2)."
  type        = string
  default     = "ap-southeast-2"
}

variable "domain" {
  description = "Root domain. The API is https://<api_host_label>.<domain>/v1 and SES receives mail for <anything>@<domain>."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]*(\\.[a-z0-9][a-z0-9-]*)+$", var.domain))
    error_message = "domain must be a bare lower-case domain such as example.com."
  }
}

variable "hosted_zone_id" {
  description = "PUBLIC Route 53 hosted zone for domain, in this account (e.g. Z0123456789ABC). Leave empty to look it up from domain, as deploy.ps1 does."
  type        = string
  default     = ""

  validation {
    condition     = var.hosted_zone_id == "" || can(regex("^[A-Z0-9]{10,32}$", var.hosted_zone_id))
    error_message = "hosted_zone_id must be a Route 53 zone id such as Z0123456789ABC, without /hostedzone/ - or leave it empty to look the zone up from domain."
  }
}

variable "api_host_label" {
  description = "Host label for the API (api -> api.<domain>)."
  type        = string
  default     = "api"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{0,62}$", var.api_host_label))
    error_message = "api_host_label must be a single lower-case DNS label."
  }
}

variable "serve_apex_site" {
  description = "Also serve the landing page, /llms.txt and /.well-known/* at https://<domain>/. Set false if <domain> already has an A record or is hosted elsewhere."
  type        = bool
  default     = true
}

variable "name_prefix" {
  description = "Prefix for function, role, table and API names, and the SES rule set. Change it for a second copy in the same account."
  type        = string
  default     = "testhelper-v1"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,30}$", var.name_prefix))
    error_message = "name_prefix must be 2-31 lower-case letters, digits or hyphens."
  }
}

variable "bucket_prefix" {
  description = "Prefix for S3 bucket names (<prefix>-<purpose>-<account>-<region>). Change it for a second copy in the same account."
  type        = string
  default     = "testhelper-v1"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,16}$", var.bucket_prefix))
    error_message = "bucket_prefix must be 2-17 lower-case letters, digits or hyphens (bucket names are capped at 63 characters)."
  }
}

variable "tags" {
  description = "Tags added to every resource."
  type        = map(string)
  default     = { Application = "testhelper-v1" }
}

# ------------------------------------------------------------------ email (SES)
variable "create_ses_identity" {
  description = "Create the SES domain identity (Easy DKIM, custom MAIL FROM) and its DKIM records. false if the identity already exists in this account/region."
  type        = bool
  default     = true
}

variable "manage_mail_dns_records" {
  description = "Create the apex MX (inbound mail) and the mail.<domain> MAIL FROM MX/TXT records. false if they already exist."
  type        = bool
  default     = true
}

variable "manage_spf_dmarc_records" {
  description = "Create the apex SPF TXT and _dmarc TXT records. false if TXT records already exist there - then add include:amazonses.com to the SPF yourself."
  type        = bool
  default     = true
}

variable "create_receipt_rule" {
  description = "Create the SES receipt rule that stores mail for domain in the inbound bucket. false if inbound mail for domain is handled elsewhere."
  type        = bool
  default     = true
}

variable "existing_receipt_rule_set" {
  description = "Only ONE SES receipt rule set can be active per account and region. If another stack already owns the active set, name it here: the rule is added to that set instead of creating (and activating) a new one."
  type        = string
  default     = ""
}

variable "activate_receipt_rule_set" {
  description = "Make this configuration's own rule set the active one (ignored when existing_receipt_rule_set is set). This REPLACES any other active rule set in the region - `terraform plan` warns when one exists."
  type        = bool
  default     = true
}

variable "dmarc_policy" {
  description = "DMARC p= value for _dmarc.<domain>. none suits a test domain."
  type        = string
  default     = "none"

  validation {
    condition     = contains(["none", "quarantine", "reject"], var.dmarc_policy)
    error_message = "dmarc_policy must be none, quarantine or reject."
  }
}

variable "email_send_allowed_to" {
  description = "Extra recipient ADDRESSES mail/send may deliver to. Addresses at domain are always allowed."
  type        = list(string)
  default     = []
}

variable "email_send_allowed_domains" {
  description = "Extra recipient DOMAINS mail/send may deliver to. example.com matches that domain only; *.example.com matches any subdomain (not the apex). Keep this short: the endpoint is public."
  type        = list(string)
  default     = []
}

# ------------------------------------------------------------------ limits
variable "throttle_rate_limit" {
  description = "Steady-state requests per second, applied per method of each API stage."
  type        = number
  default     = 500

  validation {
    condition     = var.throttle_rate_limit >= 1
    error_message = "throttle_rate_limit must be at least 1."
  }
}

variable "throttle_burst_limit" {
  description = "Burst request limit, applied per method of each API stage."
  type        = number
  default     = 1000

  validation {
    condition     = var.throttle_burst_limit >= 1
    error_message = "throttle_burst_limit must be at least 1."
  }
}

variable "webhook_retention_days" {
  description = "Days a captured webhook is kept (DynamoDB TTL)."
  type        = number
  default     = 30

  validation {
    condition     = var.webhook_retention_days >= 1
    error_message = "webhook_retention_days must be at least 1."
  }
}

# ------------------------------------------------------------------ text messages (SMS)
variable "sms_device_keys" {
  description = <<-EOT
    Secret keys capture handsets send in the X-Device-Key header of sms/receive.
    Leave empty to use a generated key (see generate_sms_device_key). Two keys
    allow rotation: add the new one, update the phones, remove the old one.
  EOT
  type        = list(string)
  default     = []
  sensitive   = true

  validation {
    condition     = alltrue([for k in var.sms_device_keys : can(regex("^[^,\\s]+$", k))])
    error_message = "SMS device keys may not contain commas or whitespace."
  }
}

variable "generate_sms_device_key" {
  description = "When sms_device_keys is empty, use a random key generated on first apply and kept in the state (`terraform output -raw sms_device_key`). false with no keys disables sms/receive."
  type        = bool
  default     = true
}

variable "sms_send_allowed_numbers" {
  description = "E.164 numbers of the registered handsets sms/send may text. Matched on the last nine digits. Empty disables real sends (sms/mocksend still works)."
  type        = list(string)
  default     = []
}

variable "sms_region" {
  description = "Region whose SNS sends the real texts - where the account's SNS SMS production access and spend limit are configured."
  type        = string
  default     = "ap-southeast-2"
}

variable "sms_send_interval_seconds" {
  description = "Minimum seconds between real texts to the same handset (0 = no limit)."
  type        = number
  default     = 10

  validation {
    condition     = var.sms_send_interval_seconds >= 0
    error_message = "sms_send_interval_seconds cannot be negative."
  }
}

# ------------------------------------------------------------------ voice
variable "voice_region" {
  description = "Region whose Amazon Polly speaks for voice/say. It need not be aws_region: the neural and generative engines are not offered everywhere (us-west-1 has neither; us-west-2 and ap-southeast-2 have both)."
  type        = string
  default     = "ap-southeast-2"
}

variable "voice_name" {
  description = "The Polly voice voice/say uses when the caller names none."
  type        = string
  default     = "Olivia"

  validation {
    condition     = can(regex("^[A-Za-z]{2,20}$", var.voice_name))
    error_message = "voice_name is a Polly voice id: 2-20 letters."
  }
}

variable "voice_engine" {
  description = "The Polly engine voice/say uses when the caller names none."
  type        = string
  default     = "neural"

  validation {
    condition     = contains(["standard", "neural", "generative"], var.voice_engine)
    error_message = "voice_engine must be standard, neural or generative."
  }
}

variable "voice_daily_characters" {
  description = "Characters voice/say may send to Polly per UTC day. Every cache miss is billed per character and the endpoint is open, so new synthesis is capped; stored audio is always served. 0 switches synthesis off. At list prices (USD 16 neural, USD 30 generative per million characters) the default is at most about USD 0.40 to 0.75 a day."
  type        = number
  default     = 25000

  validation {
    condition     = var.voice_daily_characters >= 0
    error_message = "voice_daily_characters cannot be negative."
  }
}

variable "voice_language" {
  description = "The language voice/listen expects to hear when the caller names none: an Amazon Transcribe language code such as en-AU, or auto to have each recording's language identified."
  type        = string
  default     = "en-AU"

  validation {
    condition     = can(regex("^([a-z]{2,3}-[A-Z]{2}|auto)$", var.voice_language))
    error_message = "voice_language is a language code such as en-AU, or auto."
  }
}

variable "voice_daily_listen_seconds" {
  description = "Seconds of audio voice/listen may send to Amazon Transcribe per UTC day. Transcription is billed by the second and the endpoint is open, so new listening is capped; audio already transcribed is always served. 0 switches listening off."
  type        = number
  default     = 1800

  validation {
    condition     = var.voice_daily_listen_seconds >= 0
    error_message = "voice_daily_listen_seconds cannot be negative."
  }
}

# ------------------------------------------------------------------ tracing
variable "tracing" {
  description = "AWS X-Ray tracing of the v1 API stage and every function. Billed per trace recorded (the default sampling rule keeps 1 request a second plus 5% of the rest; health calls are always kept); no standing cost. health/get and health/check return the URL of their trace."
  type        = bool
  default     = true
}

# ------------------------------------------------------------------ build stamp
variable "build_id" {
  description = "Build identifier stamped on every function (X-Build header, health/get). Empty (the default): the UTC time the code last changed, as yyyymmdd.hhmm - it changes when ../src changes, not on every apply."
  type        = string
  default     = ""

  validation {
    condition     = can(regex("^[A-Za-z0-9._+-]{0,64}$", var.build_id))
    error_message = "build_id: up to 64 letters, digits and . _ + -."
  }
}

variable "build_commit" {
  description = "Source commit of this build, e.g. -var build_commit=$(git rev-parse --short HEAD). May be empty."
  type        = string
  default     = ""
}

variable "deployed_time" {
  description = "When this build was deployed, UTC epoch seconds. 0 (the default): the time the code last changed, recorded with build_id."
  type        = number
  default     = 0
}

# ------------------------------------------------------------------ runtimes
variable "python_runtime" {
  description = "Lambda runtime for the Python services."
  type        = string
  default     = "python3.12"
}

variable "nodejs_runtime" {
  description = "Lambda runtime for the lists service. Older AWS provider versions may not know nodejs24.x yet - upgrade the provider, or use nodejs22.x."
  type        = string
  default     = "nodejs24.x"
}
