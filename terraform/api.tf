# Two REST APIs, both REGIONAL, every route ANY -> Lambda proxy:
#   v1   : /<service> and /<service>/{proxy+} -> one function per service;
#          /mail/pop|reset|clear -> the small mail-inbox function (static
#          resources win over {proxy+}); / and /{proxy+} -> discovery.
#   root : / and /{proxy+} -> discovery (landing page, llms.txt, .well-known).
# Adding an ACTION needs only a code change; the deployments below redeploy
# themselves whenever a route changes (no logical-id bumping as in CFN).

resource "aws_api_gateway_rest_api" "v1" {
  name        = "${var.name_prefix}-v1"
  description = "Test Helper API v1"
  # Bodies of these types reach Lambda byte-exact (base64): CSV imports in
  # Windows-1252/UTF-16, raw attachment uploads, binary webhook payloads.
  binary_media_types       = ["text/csv", "application/octet-stream"]
  minimum_compression_size = "8192"

  endpoint_configuration {
    types = ["REGIONAL"]
  }
}

resource "aws_api_gateway_rest_api" "root" {
  name                     = "${var.name_prefix}-root"
  description              = "Test Helper API - domain root (llms.txt, /.well-known/*)"
  minimum_compression_size = "8192"

  endpoint_configuration {
    types = ["REGIONAL"]
  }
}

locals {
  # /v1/<service> -> function
  services = {
    lists   = "lists"
    names   = "names"
    lock    = "lock"
    mail    = "mail"
    sms     = "sms"
    voice   = "voice"
    webhook = "webhook"
    totp    = "totp"
    qr      = "qr"
    perf    = "perf"
  }
  mail_inbox_actions = ["pop", "reset", "clear"]
}

resource "aws_api_gateway_resource" "v1_proxy" {
  rest_api_id = aws_api_gateway_rest_api.v1.id
  parent_id   = aws_api_gateway_rest_api.v1.root_resource_id
  path_part   = "{proxy+}"
}

resource "aws_api_gateway_resource" "service" {
  for_each    = local.services
  rest_api_id = aws_api_gateway_rest_api.v1.id
  parent_id   = aws_api_gateway_rest_api.v1.root_resource_id
  path_part   = each.key
}

resource "aws_api_gateway_resource" "service_proxy" {
  for_each    = local.services
  rest_api_id = aws_api_gateway_rest_api.v1.id
  parent_id   = aws_api_gateway_resource.service[each.key].id
  path_part   = "{proxy+}"
}

resource "aws_api_gateway_resource" "mail_inbox" {
  for_each    = toset(local.mail_inbox_actions)
  rest_api_id = aws_api_gateway_rest_api.v1.id
  parent_id   = aws_api_gateway_resource.service["mail"].id
  path_part   = each.key
}

resource "aws_api_gateway_resource" "root_proxy" {
  rest_api_id = aws_api_gateway_rest_api.root.id
  parent_id   = aws_api_gateway_rest_api.root.root_resource_id
  path_part   = "{proxy+}"
}

locals {
  # route key => { api, resource id, function }
  routes = merge(
    {
      v1_root    = { api = "v1", resource = aws_api_gateway_rest_api.v1.root_resource_id, fn = "discovery" }
      v1_proxy   = { api = "v1", resource = aws_api_gateway_resource.v1_proxy.id, fn = "discovery" }
      root_root  = { api = "root", resource = aws_api_gateway_rest_api.root.root_resource_id, fn = "discovery" }
      root_proxy = { api = "root", resource = aws_api_gateway_resource.root_proxy.id, fn = "discovery" }
    },
    { for s, fn in local.services : s => { api = "v1", resource = aws_api_gateway_resource.service[s].id, fn = fn } },
    { for s, fn in local.services : "${s}_proxy" => { api = "v1", resource = aws_api_gateway_resource.service_proxy[s].id, fn = fn } },
    { for a in local.mail_inbox_actions : "mail_${a}" => { api = "v1", resource = aws_api_gateway_resource.mail_inbox[a].id, fn = "mail_inbox" } },
  )
  rest_api = {
    v1   = aws_api_gateway_rest_api.v1.id
    root = aws_api_gateway_rest_api.root.id
  }
}

resource "aws_api_gateway_method" "route" {
  for_each      = local.routes
  rest_api_id   = local.rest_api[each.value.api]
  resource_id   = each.value.resource
  http_method   = "ANY"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "route" {
  for_each                = local.routes
  rest_api_id             = local.rest_api[each.value.api]
  resource_id             = each.value.resource
  http_method             = aws_api_gateway_method.route[each.key].http_method
  type                    = "AWS_PROXY"
  integration_http_method = "POST"
  uri                     = aws_lambda_function.this[each.value.fn].invoke_arn
}

# Gateway-generated errors (throttling, timeouts) in the API's error shape, with CORS.
resource "aws_api_gateway_gateway_response" "default" {
  for_each = {
    for p in setproduct(keys(local.rest_api), ["DEFAULT_4XX", "DEFAULT_5XX"]) : "${p[0]}_${p[1]}" => { api = p[0], type = p[1] }
  }
  rest_api_id   = local.rest_api[each.value.api]
  response_type = each.value.type

  response_parameters = {
    "gatewayresponse.header.Access-Control-Allow-Origin"  = "'*'"
    "gatewayresponse.header.Access-Control-Allow-Headers" = "'*'"
  }
  response_templates = {
    "application/json" = "{\"error\": $context.error.messageString, \"gatewayError\": \"$context.error.responseType\"}"
  }
}

# ------------------------------------------------------------------ deployments and stages
resource "aws_api_gateway_deployment" "this" {
  for_each    = local.rest_api
  rest_api_id = each.value

  # Redeploy when any route, integration or gateway response of this API changes.
  triggers = {
    redeployment = sha1(jsonencode({
      routes = [
        for k, r in local.routes : [
          k,
          aws_api_gateway_method.route[k].id,
          aws_api_gateway_integration.route[k].id,
          aws_api_gateway_integration.route[k].uri,
        ] if r.api == each.key
      ]
      responses = [
        for k, g in aws_api_gateway_gateway_response.default : [k, g.id, g.response_templates] if startswith(k, "${each.key}_")
      ]
    }))
  }

  lifecycle {
    create_before_destroy = true
  }

  depends_on = [
    aws_api_gateway_integration.route,
    aws_api_gateway_gateway_response.default,
  ]
}

resource "aws_api_gateway_stage" "v1" {
  rest_api_id   = aws_api_gateway_rest_api.v1.id
  deployment_id = aws_api_gateway_deployment.this["v1"].id
  stage_name    = "live"
}

resource "aws_api_gateway_stage" "root" {
  rest_api_id   = aws_api_gateway_rest_api.root.id
  deployment_id = aws_api_gateway_deployment.this["root"].id
  stage_name    = "live"
}

# Stage throttling: the default for every method of the stage.
resource "aws_api_gateway_method_settings" "throttle" {
  for_each = {
    v1   = aws_api_gateway_stage.v1
    root = aws_api_gateway_stage.root
  }
  rest_api_id = each.value.rest_api_id
  stage_name  = each.value.stage_name
  method_path = "*/*"

  settings {
    throttling_rate_limit  = var.throttle_rate_limit
    throttling_burst_limit = var.throttle_burst_limit
  }
}
