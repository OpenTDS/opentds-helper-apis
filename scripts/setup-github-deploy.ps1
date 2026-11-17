# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Test Data Services Pty. Ltd.
<#
.SYNOPSIS
  One-off setup so GitHub Actions can deploy this repository into your AWS account.

.DESCRIPTION
  Run this once, from your own machine, with AWS credentials for the TARGET
  account (an admin or anyone allowed to create IAM roles).

  1. Works out your GitHub repository (owner/repo) from `git remote get-url origin`.
  2. Detects whether the account already has GitHub's OpenID Connect identity
     provider, and deploys infra/github-deploy-role.yaml as a small stack.
     That creates (if needed) the provider and an IAM role that only GitHub
     Actions jobs from YOUR repository, running in the chosen environment,
     can assume. No AWS access keys are created or stored anywhere.
  3. With the GitHub CLI (gh) signed in, it also creates the environment and
     sets the repository variables the deploy workflow reads. Without gh it
     prints the values to set by hand.

  Afterwards: Actions tab -> "Deploy to AWS" -> Run workflow.

.EXAMPLE
  .\scripts\setup-github-deploy.ps1 -Domain example.com
.EXAMPLE
  .\scripts\setup-github-deploy.ps1 -Domain example.com -Repository me/opentds-helper-apis -Region us-east-1
#>
[CmdletBinding()]
param(
    [string]$Domain,
    [string]$Repository,
    [string]$Environment = 'aws-deploy',
    [string]$Region = 'ap-southeast-2',
    [string]$StackName = 'testhelper-v1',
    [string]$RoleStackName = 'testhelper-v1-github-deploy',
    [switch]$SkipGitHub
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

function Invoke-Native {
    # Runs a native command (first argument) and returns stdout; throws on a
    # non-zero exit. Plain $args, so --options pass straight through.
    $ErrorActionPreference = 'Continue'
    $exe = $args[0]
    $rest = @($args | Select-Object -Skip 1)
    $out = & $exe @rest 2>&1
    if ($LASTEXITCODE -ne 0) { throw "$exe $($rest -join ' ') failed:`n$($out -join "`n")" }
    return (($out | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] }) -join "`n")
}

# ------------------------------------------------------------ inputs
if (-not (Get-Command aws -ErrorAction SilentlyContinue)) {
    throw 'The AWS CLI is not installed or not on PATH (Windows: winget install Amazon.AWSCLI).'
}
if (-not $Repository) {
    try {
        $url = Invoke-Native git remote get-url origin
        if ($url -match 'github\.com[:/]([^/]+)/(.+?)(\.git)?$') { $Repository = "$($Matches[1])/$($Matches[2])" }
    } catch { }
}
if (-not $Repository) { $Repository = Read-Host 'GitHub repository (owner/repo)' }
if ($Repository -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$') { throw "'$Repository' is not owner/repo." }
if (-not $Domain) { $Domain = Read-Host 'Domain the API will use (e.g. example.com)' }
$Domain = $Domain.Trim().TrimEnd('.').ToLowerInvariant()

$account = Invoke-Native aws sts get-caller-identity --query Account --output text
Write-Host "`nAccount     $account"
Write-Host "Region      $Region"
Write-Host "Repository  $Repository (environment '$Environment')"
Write-Host "API stack   $StackName for $Domain"

# ------------------------------------------------------------ IAM
$providers = Invoke-Native aws iam list-open-id-connect-providers --query 'OpenIDConnectProviderList[].Arn' --output text
$haveProvider = [bool]($providers -split '\s+' | Where-Object { $_ -like '*:oidc-provider/token.actions.githubusercontent.com' })
# A provider this setup stack created earlier must stay managed by it.
$ownProvider = $false
& { $ErrorActionPreference = 'Continue'
    aws cloudformation describe-stack-resource --stack-name $RoleStackName --logical-resource-id GitHubOidcProvider --region $Region 2>&1 | Out-Null }
if ($LASTEXITCODE -eq 0) { $ownProvider = $true }
$createProvider = if ($ownProvider -or -not $haveProvider) { 'true' } else { 'false' }
Write-Host ("GitHub OIDC provider: {0}" -f $(if ($createProvider -eq 'true') { 'created/managed by this stack' } else { 'already exists - reusing it' }))

Write-Host "`n== Deploying $RoleStackName" -ForegroundColor Cyan
& aws cloudformation deploy --template-file infra/github-deploy-role.yaml --stack-name $RoleStackName `
    --region $Region --capabilities CAPABILITY_IAM --no-fail-on-empty-changeset `
    --parameter-overrides "GitHubRepository=$Repository" "GitHubEnvironment=$Environment" `
    "StackName=$StackName" "CreateOidcProvider=$createProvider"
if ($LASTEXITCODE -ne 0) { throw "Deploying $RoleStackName failed - see its events in the CloudFormation console." }

$roleArn = Invoke-Native aws cloudformation describe-stacks --stack-name $RoleStackName --region $Region `
    --query "Stacks[0].Outputs[?OutputKey=='RoleArn'].OutputValue" --output text
Write-Host "Role $roleArn" -ForegroundColor Green

# ------------------------------------------------------------ GitHub
$vars = [ordered]@{ AWS_ROLE_ARN = $roleArn; AWS_REGION = $Region; DOMAIN = $Domain; STACK_NAME = $StackName }
$gh = Get-Command gh -ErrorAction SilentlyContinue
if (-not $SkipGitHub -and $gh) {
    Write-Host "`n== Configuring $Repository with gh" -ForegroundColor Cyan
    Invoke-Native gh api --method PUT "repos/$Repository/environments/$Environment" | Out-Null
    Write-Host "Environment '$Environment' ready"
    foreach ($kv in $vars.GetEnumerator()) {
        Invoke-Native gh variable set $kv.Key --body $kv.Value --repo $Repository | Out-Null
        Write-Host ("  variable {0,-13} = {1}" -f $kv.Key, $kv.Value)
    }
    Write-Host @"

Optional: the SMS capture key (leave unset if you don't run capture handsets):
  gh secret set SMS_DEVICE_KEYS --repo $Repository --env $Environment
Recommended: add yourself as a required reviewer on the '$Environment' environment
  (repository Settings -> Environments) so every deploy waits for your approval.
"@
} else {
    Write-Host "`nSet these in GitHub (Settings -> Secrets and variables -> Actions -> Variables):" -ForegroundColor Yellow
    foreach ($kv in $vars.GetEnumerator()) { Write-Host ("  {0,-13} {1}" -f $kv.Key, $kv.Value) }
    Write-Host "and create an environment named '$Environment' (Settings -> Environments)."
}
Write-Host "`nNext: Actions -> 'Deploy to AWS' -> Run workflow (tick 'check only' for a dry run first)." -ForegroundColor Green
