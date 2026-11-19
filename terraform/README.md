# Test Helper API v1 — Terraform

A Terraform alternative to `template.yaml` + `deploy.ps1`. It builds the same
AWS stack from the same code (`../src`): the same API Gateway routes, Lambda
functions (runtimes, handlers, memory, timeouts, environment), IAM permissions,
DynamoDB tables, S3 buckets and lifecycle rules, SES identity and inbound rule,
certificates, custom domains and DNS records. The URLs are identical:
`https://api.<domain>/v1/...`, with the landing page at `https://<domain>/`.

AWS only: the services rely on API Gateway, Lambda, DynamoDB, S3, SES
(inbound and outbound mail), SNS (SMS), ACM and Route 53.

## Prerequisites

* Terraform **1.5 or later** (or OpenTofu 1.6+). AWS provider 5.40+ (5.x or 6.x); `terraform init` fetches it.
* AWS credentials for the target account, with rights to create the resources above.
* A **public** Route 53 hosted zone for the domain, in the same account.
* A region that supports SES **email receiving** (e.g. `us-east-1`, `us-west-2`,
  `eu-west-1`, `ap-southeast-2`).
* Python 3, to check the discovery documents are current (as `deploy.ps1` does).

## Deploy

```bash
python tools/build_docs.py --check     # from the repository root: docs must be current
cd terraform
cp terraform.tfvars.example terraform.tfvars   # set domain and aws_region (hosted_zone_id is optional)
terraform init
terraform validate
terraform plan -out tf.plan            # read any warnings - see "SES receipt rules"
terraform apply tf.plan
```

The first apply takes 5–10 minutes, mostly waiting for ACM to validate the
certificates. Afterwards:

```bash
terraform output                        # base_url, llms_guide, site_url, bucket names...
terraform output -raw sms_device_key    # the X-Device-Key for the SMS capture app
```

To update after a code or docs change, run `python tools/build_docs.py`, then
`terraform apply` again. The code package is re-zipped from `../src`, and only
changed functions are updated.

### If `terraform validate` complains

This configuration validates and plans cleanly (OpenTofu 1.10 with AWS provider 6.73;
`plan` against a mocked account creates 212 resources). Provider 6.x prints a
deprecation warning for `range_key` on the DynamoDB tables; it is harmless. The
likeliest version-dependent spots, if you use older versions:

* *Invalid data source "aws_ses_active_receipt_rule_set"* (very old provider):
  delete the `check "active_receipt_rule_set"` block in `ses.tf`. It only
  produces the plan-time warning described below.
* *expected runtime to be one of ...* (provider older than the `nodejs24.x`
  runtime): upgrade the provider, or set `nodejs_runtime = "nodejs22.x"`.

## Before the first apply: things that may already exist

`deploy.ps1` looks at the account and the hosted zone and switches these off
automatically. With Terraform you set them yourself. Route 53 refuses to create
a record that is already there, so the plan or apply tells you which one applies.

| If this already exists | Set |
|---|---|
| SES identity for the domain in this region | `create_ses_identity = false` |
| MX at the domain, or MX/TXT at `mail.<domain>` | `manage_mail_dns_records = false` |
| TXT at the domain or at `_dmarc.<domain>` | `manage_spf_dmarc_records = false` (then add `include:amazonses.com` to the SPF yourself) |
| An A record at the bare domain, or it's hosted elsewhere | `serve_apex_site = false` (the landing page is then at `https://api.<domain>/`) |
| An **active** SES receipt rule set in the region | see below |

### SES receipt rules

Only one receipt rule set can be active per account and region. By default
this configuration creates `<name_prefix>-inbound` and makes it active, which
**replaces** any other active set. CloudFormation used a custom resource that
refused to do that. Here, `terraform plan` shows a **warning** naming the
active set instead. If you see it, choose one of these:

* `existing_receipt_rule_set = "<that set>"`: add this rule to the active set (recommended; nothing else changes), or
* `activate_receipt_rule_set = false`: create the set but leave it inactive, or
* apply anyway, to replace the active set deliberately.

When no set is active yet, the plan also shows a harmless warning that the
lookup found nothing.

## Variables

Every CloudFormation parameter has a variable, renamed to snake_case.
Comma-separated strings became lists.

| template.yaml parameter | Terraform variable |
|---|---|
| `Domain`, `HostedZoneId`, `ApiHostLabel`, `BucketPrefix`, `ServeApexSite` | `domain`, `hosted_zone_id`, `api_host_label`, `bucket_prefix`, `serve_apex_site` |
| `CreateSesIdentity`, `ManageMailDnsRecords`, `ManageSpfDmarcRecords` | `create_ses_identity`, `manage_mail_dns_records`, `manage_spf_dmarc_records` |
| `CreateReceiptRules`, `ExistingReceiptRuleSet`, `ActivateReceiptRuleSet` | `create_receipt_rule`, `existing_receipt_rule_set`, `activate_receipt_rule_set` |
| `ForceActivateReceiptRuleSet` | — (see "SES receipt rules") |
| `DmarcPolicy` | `dmarc_policy` |
| `EmailSendAllowedTo`, `EmailSendAllowedDomains` | `email_send_allowed_to`, `email_send_allowed_domains` (lists) |
| `ThrottleRateLimit`, `ThrottleBurstLimit`, `WebhookRetentionDays` | `throttle_rate_limit`, `throttle_burst_limit`, `webhook_retention_days` |
| `SmsDeviceKeys` | `sms_device_keys` (list, sensitive) + `generate_sms_device_key` |
| `SmsSendAllowedNumbers`, `SmsRegion`, `SmsSendIntervalSeconds` | `sms_send_allowed_numbers` (list), `sms_region`, `sms_send_interval_seconds` |
| `VoiceRegion`, `VoiceName`, `VoiceEngine`, `VoiceDailyCharacters` | `voice_region`, `voice_name`, `voice_engine`, `voice_daily_characters` |

Variables that are new in Terraform:

* `aws_region`
* `name_prefix`: names functions, roles, tables, APIs and the rule set, as the stack name did.
* `tags`: applied to everything.
* `python_runtime` / `nodejs_runtime`: if an older provider rejects `nodejs24.x`, upgrade the provider or set `nodejs22.x`.

## Differences from the CloudFormation deployment

* **Resource names.** CloudFormation generated table, function and role names. Here they are `<name_prefix>-<name>`, e.g. `testhelper-v1-lists`. Bucket names follow the same pattern as before: `<bucket_prefix>-<purpose>-<account>-<region>`.
* **API redeployment is automatic.** CloudFormation needed the deployment's logical id bumped when routes changed. Here the deployments redeploy whenever a route, integration or gateway response changes.
* **No helper Lambda for SES.** The rule set is activated with the native `aws_ses_active_receipt_rule_set` resource.
* **No staging bucket.** The code is zipped locally (`.build/src.zip`) and uploaded with each function.
* **SMS device keys.** These were kept in `sms-device-keys.txt`. Here they are kept in the Terraform state, which is therefore sensitive: keep it private, e.g. in an encrypted S3 backend (see `versions.tf`).
* **Data is protected.** CloudFormation used `DeletionPolicy: Retain` on the tables (except locks) and buckets. Here they have `prevent_destroy`, so `terraform destroy` stops with an error rather than deleting data.

### Tearing down

To remove everything **but keep the data**, as CloudFormation's Retain did, take the data out of Terraform's management first:

```bash
terraform state rm 'aws_dynamodb_table.this' 'aws_dynamodb_table.sms' \
  'aws_s3_bucket.this' 'aws_s3_bucket_public_access_block.this' \
  'aws_s3_bucket_server_side_encryption_configuration.this' \
  'aws_s3_bucket_lifecycle_configuration.imports' 'aws_s3_bucket_lifecycle_configuration.attachments' \
  'aws_s3_bucket_lifecycle_configuration.inbound' 'aws_s3_bucket_lifecycle_configuration.voice' \
  'aws_s3_bucket_cors_configuration.imports' 'aws_s3_bucket_cors_configuration.attachments' \
  'aws_s3_bucket_cors_configuration.voice' 'aws_s3_bucket_policy.inbound'
terraform destroy
```

To delete the data too: empty the four buckets, delete the `prevent_destroy` blocks in `storage.tf`, then run `terraform destroy`.

### Moving an account from the CloudFormation stack to Terraform

Don't run both in the same account and region with the same prefixes, because the bucket names and the `api.<domain>` custom domain would collide. Either:

* deploy Terraform to a different account or region, or
* delete the CloudFormation stack first. Its tables and buckets are retained. Then import the buckets so Terraform adopts them rather than failing:
  ```bash
  terraform import 'aws_s3_bucket.this["imports"]'     <bucket_prefix>-imports-<account>-<region>
  terraform import 'aws_s3_bucket.this["attachments"]' <bucket_prefix>-mail-attachments-<account>-<region>
  terraform import 'aws_s3_bucket.this["inbound"]'     <bucket_prefix>-mail-inbound-<account>-<region>
  ```
  The CloudFormation tables had generated names, so the Terraform tables start empty. Copy across any lists, variables or TOTP secrets you need.

## Files

| File | Contents |
|---|---|
| `versions.tf` | Terraform and provider versions, provider config, optional S3 backend |
| `variables.tf` | Inputs, with validation |
| `main.tf` | Shared names, hosted-zone checks, generated SMS key |
| `domain.tf` | ACM certificates and DNS validation, custom domains, A records, base-path mappings |
| `storage.tf` | DynamoDB tables, S3 buckets (encryption, lifecycle, CORS, SES policy, notification) |
| `ses.tf` | SES identity, DKIM/MX/SPF/DMARC records, receipt rule set and rule |
| `iam.tf` | One role per function family, with the same statements as `template.yaml` |
| `lambda.tf` | Code package, the 13 functions, invoke permissions |
| `api.tf` | The v1 and root REST APIs: routes, integrations, gateway responses, deployments, stages, throttling |
| `outputs.tf` | Same outputs as the stack, plus the rule set and the SMS key |
| `tests/check_parity.py` | Cross-checks this configuration against `../template.yaml` |

## Keeping it in step with template.yaml

`tests/check_parity.py` compares the two definitions field by field: every function's runtime, handler, memory, timeout, role and environment; every IAM statement; every table key and TTL; every API route and invoke permission; the bucket lifecycle and CORS rules; the gateway responses and the outputs. It also runs as part of the main test suite (`tests/test_terraform_parity.py`). If you change one definition, the check fails until the other matches.

```bash
pip install python-hcl2 pyyaml
python terraform/tests/check_parity.py
```
