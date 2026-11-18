<#
.SYNOPSIS
  Package and deploy the Test Helper API v1 stack.

.DESCRIPTION
  1. Checks the discovery documents are built (tools/build_docs.py --check).
  2. Resolves the Route 53 hosted zone for -Domain.
  3. Inspects SES in the target region and chooses safe defaults:
       - domain identity already exists       -> CreateSesIdentity=false
       - an MX record already exists for it   -> ManageMailDnsRecords=false
       - another receipt rule set is active   -> offers to add this stack's
                                                 rule to it (ExistingReceiptRuleSet)
  4. Packages src/ to a staging bucket (created if missing) and deploys with
     the FULL parameter set - `aws cloudformation deploy` resets any parameter
     that is left out to its template default, so this script always passes
     every one of them.
  5. Prints the outputs and warns if SES is still in the sandbox.

  Settings you leave out keep the deployed stack's current value (allow-lists,
  SMS handsets and region, voice region/voice/engine/budget, throttles,
  retention, DMARC). Pass one explicitly to
  change it - pass '' to clear a list. On a first deploy they take the defaults.

  SMS device keys live in sms-device-keys.txt next to this script (git-ignored,
  one key per line). It is created with a random key on first deploy; build the
  same key into the capture app. -SmsDeviceKeys "k1,k2" replaces the file.

.EXAMPLE
  .\deploy.ps1 -Domain example.com -CheckOnly
  (checks tools, credentials, the hosted zone, name-server delegation and SES,
  reports what a deploy would do, and changes nothing)
.EXAMPLE
  .\deploy.ps1 -Domain example.com
.EXAMPLE
  .\deploy.ps1 -Domain example.com -HostedZoneId Z0123456789ABCDEFGHIJ
.EXAMPLE
  .\deploy.ps1 -Domain example.com -EmailSendAllowedDomains "partner.example.net" -Yes
.EXAMPLE
  .\deploy.ps1 -Domain example.com -SmsSendAllowedNumbers "+61400000001,+61400000002"
.EXAMPLE
  .\deploy.ps1 -Domain example.com -Region us-west-1 -VoiceRegion us-west-2
  (voice/say speaks through Amazon Polly in -VoiceRegion; it must be a region
  that offers the engine - us-west-1 has neither neural nor generative voices)
#>
[CmdletBinding()]
param(
    [string]$Domain,
    [string]$HostedZoneId,
    [string]$StackName = 'testhelper-v1',
    [string]$Region = 'ap-southeast-2',
    [string]$ApiHostLabel = 'api',
    [string]$BucketPrefix = 'testhelper-v1',
    [string]$StagingBucket,
    [string]$ExistingReceiptRuleSet,
    [switch]$NoReceiptRule,
    [switch]$ForceActivateReceiptRuleSet,
    [ValidateSet('none', 'quarantine', 'reject')][string]$DmarcPolicy,
    [string]$EmailSendAllowedTo,
    [string]$EmailSendAllowedDomains,
    [int]$ThrottleRateLimit,
    [int]$ThrottleBurstLimit,
    [int]$WebhookRetentionDays,
    [string]$SmsSendAllowedNumbers,
    [string]$SmsRegion,
    [int]$SmsSendIntervalSeconds,
    [string]$SmsDeviceKeys,
    [string]$VoiceRegion,
    [string]$VoiceName,
    [ValidateSet('standard', 'neural', 'generative')][string]$VoiceEngine,
    [int]$VoiceDailyCharacters,
    [switch]$CheckOnly,
    [switch]$SkipDnsCheck,
    [switch]$Yes
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$PSBoundParameters_ = $PSBoundParameters
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# Windows PowerShell 5.1 turns native-command stderr into a terminating error
# under ErrorActionPreference=Stop, so both helpers relax it locally and rely
# on the exit code instead.
function Invoke-Aws {
    # Runs the AWS CLI and returns its stdout; throws on a non-zero exit.
    $ErrorActionPreference = 'Continue'
    $out = & aws @args 2>&1
    if ($LASTEXITCODE -ne 0) { throw "aws $($args -join ' ') failed:`n$($out -join "`n")" }
    return (($out | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] }) -join "`n")
}

function Test-Aws {
    # Runs the AWS CLI, returns $true on success and swallows the error text.
    $ErrorActionPreference = 'Continue'
    & aws @args 2>&1 | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function Test-StackOwns([string]$LogicalId) {
    return (Test-Aws cloudformation describe-stack-resource --stack-name $StackName `
        --logical-resource-id $LogicalId --region $Region)
}

# The deployed value of a stack parameter, or $null (no stack / no such parameter).
$script:stackParams = $null
function Get-StackParam([string]$Key) {
    if ($null -eq $script:stackParams) {
        $script:stackParams = @{}
        if (Test-Aws cloudformation describe-stacks --stack-name $StackName --region $Region) {
            $json = Invoke-Aws cloudformation describe-stacks --stack-name $StackName --region $Region `
                --query 'Stacks[0].Parameters' --output json
            foreach ($p in ($json | ConvertFrom-Json)) { $script:stackParams[$p.ParameterKey] = $p.ParameterValue }
        }
    }
    if ($script:stackParams.ContainsKey($Key)) { return $script:stackParams[$Key] }
    return $null
}

# Explicit value if the caller passed one, else the deployed value, else the default.
function Resolve-Setting([string]$Name, [string]$Default) {
    if ($PSBoundParameters_.ContainsKey($Name)) { return [string]$PSBoundParameters_[$Name] }
    $current = Get-StackParam $Name
    if ($null -ne $current) { return [string]$current }
    return $Default
}

function Get-RecordCount([string]$Name, [string]$Type) {
    return [int](Invoke-Aws route53 list-resource-record-sets --hosted-zone-id $zoneId `
        --query "ResourceRecordSets[?Name=='$Name' && Type=='$Type'] | length(@)" --output text)
}

# Public NS records for a name, via Resolve-DnsName (Windows) or dig. Empty if
# neither is available or the lookup fails - the caller treats that as unknown.
function Get-PublicNameServers([string]$Name) {
    try {
        if (Get-Command Resolve-DnsName -ErrorAction SilentlyContinue) {
            return @(Resolve-DnsName -Name $Name -Type NS -DnsOnly -ErrorAction Stop |
                Where-Object { $_.QueryType -eq 'NS' -and $_.Name.TrimEnd('.') -eq $Name } |
                ForEach-Object { $_.NameHost.TrimEnd('.').ToLowerInvariant() })
        }
        if (Get-Command dig -ErrorAction SilentlyContinue) {
            $ErrorActionPreference = 'Continue'
            return @(& dig +short NS $Name 2>$null | Where-Object { $_ } |
                ForEach-Object { $_.Trim().TrimEnd('.').ToLowerInvariant() })
        }
    } catch { }
    return @()
}

# No prompts when -Yes is given or when running under CI (GitHub Actions sets both).
$nonInteractive = $Yes -or $env:CI -eq 'true' -or $env:GITHUB_ACTIONS -eq 'true'

# ---------------------------------------------------------------- inputs
if (-not $Domain) {
    if ($nonInteractive) { throw '-Domain is required when running non-interactively.' }
    $Domain = Read-Host 'Domain (e.g. example.com)'
}
$Domain = $Domain.Trim().TrimEnd('.').ToLowerInvariant()
if ($Domain -notmatch '^[a-z0-9][a-z0-9-]*(\.[a-z0-9][a-z0-9-]*)+$') {
    throw "'$Domain' is not a bare domain name."
}
$StackName = $StackName.ToLowerInvariant()

Write-Host "`n== Checking prerequisites" -ForegroundColor Cyan
Write-Host ("PowerShell {0}" -f $PSVersionTable.PSVersion)

if (-not (Get-Command aws -ErrorAction SilentlyContinue)) {
    throw ("The AWS CLI is not installed or not on PATH. Install v2 " +
        "(Windows: winget install Amazon.AWSCLI; others: " +
        "https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html), " +
        "then open a new terminal.")
}
$awsVersion = & { $ErrorActionPreference = 'Continue'; (& aws --version 2>&1) -join ' ' }
if ($awsVersion -match 'aws-cli/2\.') {
    Write-Host ($awsVersion -split ' ')[0]
} else {
    Write-Warning "AWS CLI v2 is recommended; found: $awsVersion"
}

try {
    $identity = Invoke-Aws sts get-caller-identity --query '[Account, Arn]' --output text
} catch {
    throw ("No working AWS credentials for the CLI. Run 'aws configure' (access keys) or " +
        "'aws configure sso' then 'aws sso login', set AWS_PROFILE if you use a named " +
        "profile, and retry.`n$($_.Exception.Message)")
}
$account, $callerArn = $identity -split '\s+'
Write-Host "Account $account, region $Region"
Write-Host "Caller  $callerArn"

# ---------------------------------------------------------------- docs
# A Python 3 that actually runs. Windows ships python.exe / python3.exe
# placeholders (App Execution Aliases) that only point at the Microsoft Store
# and exit with an error, so finding the command is not enough - run it.
function Get-WorkingPython {
    foreach ($name in 'python', 'python3', 'py') {
        foreach ($cmd in @(Get-Command $name -CommandType Application -All -ErrorAction SilentlyContinue)) {
            $ok = & {
                $ErrorActionPreference = 'Continue'
                & $cmd.Source -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' *> $null
                $LASTEXITCODE -eq 0
            }
            if ($ok) { return $cmd.Source }
        }
    }
    return $null
}

$python = Get-WorkingPython
if ($python) {
    & $python tools/build_docs.py --check
    if ($LASTEXITCODE -ne 0) {
        throw "Discovery documents are stale. Run: `"$python`" tools/build_docs.py"
    }
} else {
    Write-Warning ('Python 3 not found (or only the Microsoft Store placeholder) - skipping the ' +
        'discovery-document freshness check. Install Python 3 to enable it; deploying does not need it.')
}
Get-ChildItem -Path src -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force

# ---------------------------------------------------------------- zone
if ($HostedZoneId) {
    # Given explicitly: make sure it is the PUBLIC zone for this domain.
    $zoneId = $HostedZoneId -replace '/hostedzone/', ''
    $zoneInfo = Invoke-Aws route53 get-hosted-zone --id $zoneId `
        --query "[HostedZone.Name, HostedZone.Config.PrivateZone]" --output text
    $zoneName, $isPrivate = $zoneInfo -split '\s+'
    if ($zoneName -ne "$Domain.") { throw "Hosted zone $zoneId is for $zoneName, not $Domain." }
    if ($isPrivate -eq 'True') { throw "Hosted zone $zoneId is private; certificate validation needs the public zone." }
} else {
    $zoneId = Invoke-Aws route53 list-hosted-zones-by-name --dns-name $Domain `
        --query "HostedZones[?Name=='$Domain.' && Config.PrivateZone==``false``] | [0].Id" --output text
    if (-not $zoneId -or $zoneId -eq 'None') {
        throw ("No public Route 53 hosted zone named $Domain in account $account. Create one " +
            "(aws route53 create-hosted-zone --name $Domain --caller-reference (Get-Date -Format o)) " +
            "and point the domain's name servers at it - see docs/deployment.md.")
    }
    $zoneId = $zoneId -replace '/hostedzone/', ''
}
Write-Host "Hosted zone $zoneId"

# ACM validates the certificate through public DNS. If the domain's name servers
# don't point at this zone, the first deploy waits on validation for hours and
# then fails, so check before creating anything.
$zoneNs = @((Invoke-Aws route53 get-hosted-zone --id $zoneId --query 'DelegationSet.NameServers' --output text) -split '\s+' |
    Where-Object { $_ } | ForEach-Object { $_.TrimEnd('.').ToLowerInvariant() })
$publicNs = @(Get-PublicNameServers $Domain)
if ($publicNs.Count -eq 0) {
    Write-Warning ("Could not look up the public name servers for $Domain. Make sure the domain " +
        "is delegated to this zone: $($zoneNs -join ', ')")
} elseif (-not ($publicNs | Where-Object { $zoneNs -contains $_ })) {
    $msg = ("$Domain is delegated to $($publicNs -join ', '), but hosted zone $zoneId uses " +
        "$($zoneNs -join ', '). Certificate validation would never complete. Update the name " +
        "servers at your registrar (see docs/deployment.md), wait for DNS to catch up, and retry.")
    if ($SkipDnsCheck) { Write-Warning $msg } else { throw ($msg + ' (-SkipDnsCheck to override.)') }
} else {
    Write-Host "Name servers point at the hosted zone - OK"
}

# ---------------------------------------------------------------- custom domain
# api.<Domain> can belong to only one stack. Refuse early rather than fail
# half-way through a create.
$apiHost = "$ApiHostLabel.$Domain"
if ((Test-Aws apigateway get-domain-name --domain-name $apiHost --region $Region) -and
    -not (Test-StackOwns 'ApiCustomDomain')) {
    throw ("$apiHost is already an API Gateway custom domain owned by something else " +
        "(another stack?). Choose a different -ApiHostLabel or -Domain.")
}

# The landing page is also served at https://<Domain>/ unless the bare domain
# is already in use (an A record or a custom domain this stack doesn't own).
$serveApex = 'true'
$apexInUse = ((Get-RecordCount "$Domain." 'A') -gt 0) -or
    (Test-Aws apigateway get-domain-name --domain-name $Domain --region $Region)
if ($apexInUse -and -not (Test-StackOwns 'ApexCustomDomain')) {
    $serveApex = 'false'
    Write-Warning "$Domain already has an A record or custom domain - the landing page will be at https://$apiHost/ only."
}

# ---------------------------------------------------------------- SES
# Each check keeps managing what THIS stack created on an earlier deploy -
# passing false for a resource the stack owns would make CloudFormation delete it.
$createIdentity = 'true'
if ((Test-Aws sesv2 get-email-identity --email-identity $Domain --region $Region) -and
    -not (Test-StackOwns 'SesDomainIdentity')) {
    $createIdentity = 'false'
    Write-Host "SES identity $Domain already exists - the stack will not create it."
}

$manageMx = 'true'
$existingMx = (Get-RecordCount "$Domain." 'MX') + (Get-RecordCount "mail.$Domain." 'MX') +
    (Get-RecordCount "mail.$Domain." 'TXT')
if ($existingMx -gt 0 -and -not (Test-StackOwns 'SesMailRecords')) {
    $manageMx = 'false'
    Write-Host "MX / MAIL FROM records for $Domain already exist - the stack will not create them."
}

$manageSpf = 'true'
$existingTxt = (Get-RecordCount "$Domain." 'TXT') + (Get-RecordCount "_dmarc.$Domain." 'TXT')
if ($existingTxt -gt 0 -and -not (Test-StackOwns 'SesSpfDmarcRecords')) {
    $manageSpf = 'false'
    Write-Warning ("TXT records already exist at $Domain or _dmarc.$Domain - the stack will not " +
        "create SPF/DMARC. Make sure the apex TXT includes 'v=spf1 include:amazonses.com ~all'.")
}

$createRule = if ($NoReceiptRule) { 'false' } else { 'true' }
$activate = 'true'
if ($createRule -eq 'true' -and -not $ExistingReceiptRuleSet) {
    $active = ''
    try {
        $active = Invoke-Aws ses describe-active-receipt-rule-set --region $Region `
            --query 'Metadata.Name' --output text
    } catch { $active = '' }
    $ours = "$StackName-inbound"
    if ($active -and $active -ne 'None' -and $active -ne $ours) {
        Write-Host ""
        Write-Host "Receipt rule set '$active' is already ACTIVE in $Region." -ForegroundColor Yellow
        Write-Host "Only one can be active per account and region. Options:"
        Write-Host "  [A] add this stack's rule to '$active' (recommended - nothing else changes)"
        Write-Host "  [R] replace it with this stack's own rule set (breaks mail that relies on '$active')"
        Write-Host "  [N] no inbound rule (mail/pop will not receive anything)"
        $choice = if ($nonInteractive -or $CheckOnly) { 'A' } else { (Read-Host 'Choose A, R or N').ToUpperInvariant() }
        if ($CheckOnly) { Write-Host "(-CheckOnly: a deploy with -Yes would choose A)" }
        switch ($choice) {
            'A' { $ExistingReceiptRuleSet = $active }
            'R' { $ForceActivateReceiptRuleSet = $true }
            'N' { $createRule = 'false' }
            default { throw 'Cancelled.' }
        }
    }
}
if ($ExistingReceiptRuleSet) { $activate = 'false' }

if ($CheckOnly) {
    $prodCheck = ''
    try {
        $prodCheck = Invoke-Aws sesv2 get-account --region $Region --query ProductionAccessEnabled --output text
    } catch { $prodCheck = '' }
    $stackState = 'not deployed yet'
    if (Test-Aws cloudformation describe-stacks --stack-name $StackName --region $Region) {
        $stackState = Invoke-Aws cloudformation describe-stacks --stack-name $StackName --region $Region `
            --query 'Stacks[0].StackStatus' --output text
    }
    Write-Host "`n== Checks passed - a deploy would:" -ForegroundColor Green
    Write-Host ("  {0,-24} {1}" -f 'Stack', "$StackName in $Region ($stackState)")
    Write-Host ("  {0,-24} {1}" -f 'API', "https://$apiHost/v1")
    Write-Host ("  {0,-24} {1}" -f 'Landing page at apex', $serveApex)
    Write-Host ("  {0,-24} {1}" -f 'Create SES identity', $createIdentity)
    Write-Host ("  {0,-24} {1}" -f 'Manage MX / MAIL FROM', $manageMx)
    Write-Host ("  {0,-24} {1}" -f 'Manage SPF / DMARC', $manageSpf)
    $rulePlan = if ($createRule -eq 'false') { 'none' }
        elseif ($ExistingReceiptRuleSet) { "add rule to existing set '$ExistingReceiptRuleSet'" }
        else { "own rule set $StackName-inbound (activated)" }
    Write-Host ("  {0,-24} {1}" -f 'Inbound mail rule', $rulePlan)
    if ($prodCheck -ne 'True') {
        Write-Warning "SES in $Region is in the SANDBOX - request production access after deploying (docs/deployment.md)."
    }
    Write-Host "`nNothing was created or changed. Run again without -CheckOnly to deploy."
    return
}

# ---------------------------------------------------------------- settings
$dmarc         = Resolve-Setting 'DmarcPolicy' 'none'
$mailTo        = Resolve-Setting 'EmailSendAllowedTo' ''
$mailDomains   = Resolve-Setting 'EmailSendAllowedDomains' ''
$throttleRate  = Resolve-Setting 'ThrottleRateLimit' '500'
$throttleBurst = Resolve-Setting 'ThrottleBurstLimit' '1000'
$webhookDays   = Resolve-Setting 'WebhookRetentionDays' '30'
$smsNumbers    = Resolve-Setting 'SmsSendAllowedNumbers' ''
$smsRegion     = Resolve-Setting 'SmsRegion' 'ap-southeast-2'
$smsInterval   = Resolve-Setting 'SmsSendIntervalSeconds' '10'
$voiceRegion   = Resolve-Setting 'VoiceRegion' 'ap-southeast-2'
$voiceName     = Resolve-Setting 'VoiceName' 'Olivia'
$voiceEngine   = Resolve-Setting 'VoiceEngine' 'neural'
$voiceDaily    = Resolve-Setting 'VoiceDailyCharacters' '25000'

# SMS device keys: a NoEcho parameter can't be read back from the stack, so the
# local key file is the source of truth. Created with a random key if missing.
$keyFile = Join-Path $root 'sms-device-keys.txt'
if ($PSBoundParameters.ContainsKey('SmsDeviceKeys')) {
    $deviceKeys = @($SmsDeviceKeys -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    [System.IO.File]::WriteAllLines($keyFile, [string[]]$deviceKeys)
} elseif (Test-Path $keyFile) {
    $deviceKeys = @(Get-Content $keyFile | ForEach-Object { $_.Trim() } | Where-Object { $_ -and -not $_.StartsWith('#') })
} elseif ($env:GITHUB_ACTIONS -eq 'true') {
    # A key generated on a throwaway runner would be lost, and every run would
    # rotate it. The workflow passes the SMS_DEVICE_KEYS secret (empty = off).
    throw 'In CI, pass -SmsDeviceKeys (empty disables sms/receive); a generated key would be lost with the runner.'
} else {
    $alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789'
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $newKey = -join ($bytes | ForEach-Object { $alphabet[$_ % $alphabet.Length] })
    $deviceKeys = @($newKey)
    Set-Content -Path $keyFile -Value $deviceKeys -Encoding ascii
    Write-Host ""
    Write-Host "Created a new SMS device key in $keyFile" -ForegroundColor Yellow
    Write-Host "Build this key into the capture app (X-Device-Key header):  $newKey" -ForegroundColor Yellow
}
foreach ($k in $deviceKeys) {
    if ($k -match '[,\s]') { throw "SMS device keys may not contain commas or spaces: '$k'" }
}
$deviceKeysJoined = $deviceKeys -join ','

# ---------------------------------------------------------------- staging
if (-not $StagingBucket) { $StagingBucket = "$BucketPrefix-cfn-staging-$account-$Region" }
if (-not (Test-Aws s3api head-bucket --bucket $StagingBucket --region $Region)) {
    Write-Host "Creating staging bucket $StagingBucket"
    Invoke-Aws s3api create-bucket --bucket $StagingBucket --region $Region `
        --create-bucket-configuration "LocationConstraint=$Region" | Out-Null
    Invoke-Aws s3api put-public-access-block --bucket $StagingBucket --region $Region `
        --public-access-block-configuration `
        'BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true' | Out-Null
}

# ---------------------------------------------------------------- summary
$params = [ordered]@{
    Domain                      = $Domain
    HostedZoneId                = $zoneId
    ApiHostLabel                = $ApiHostLabel
    BucketPrefix                = $BucketPrefix
    CreateSesIdentity           = $createIdentity
    ManageMailDnsRecords        = $manageMx
    ManageSpfDmarcRecords       = $manageSpf
    CreateReceiptRules          = $createRule
    ExistingReceiptRuleSet      = "$ExistingReceiptRuleSet"
    ActivateReceiptRuleSet      = $activate
    ForceActivateReceiptRuleSet = $(if ($ForceActivateReceiptRuleSet) { 'true' } else { 'false' })
    DmarcPolicy                 = $dmarc
    EmailSendAllowedTo          = $mailTo
    EmailSendAllowedDomains     = $mailDomains
    ThrottleRateLimit           = $throttleRate
    ThrottleBurstLimit          = $throttleBurst
    WebhookRetentionDays        = $webhookDays
    ServeApexSite               = $serveApex
    SmsSendAllowedNumbers       = $smsNumbers
    SmsRegion                   = $smsRegion
    SmsSendIntervalSeconds      = $smsInterval
    SmsDeviceKeys               = $deviceKeysJoined
    VoiceRegion                 = $voiceRegion
    VoiceName                   = $voiceName
    VoiceEngine                 = $voiceEngine
    VoiceDailyCharacters        = $voiceDaily
}

Write-Host "`n== About to deploy stack '$StackName' to $Region" -ForegroundColor Cyan
Write-Host "API:  https://$ApiHostLabel.$Domain/v1"
$params.GetEnumerator() | ForEach-Object {
    $shown = if ($_.Key -eq 'SmsDeviceKeys') { "($($deviceKeys.Count) key(s) from sms-device-keys.txt)" } else { $_.Value }
    Write-Host ("  {0,-28} {1}" -f $_.Key, $shown)
}
if (-not $nonInteractive) {
    if ((Read-Host 'Proceed? (y/N)') -notmatch '^[Yy]') { throw 'Cancelled.' }
}

# ---------------------------------------------------------------- package + deploy
Write-Host "`n== Packaging" -ForegroundColor Cyan
Invoke-Aws cloudformation package --template-file template.yaml `
    --s3-bucket $StagingBucket --s3-prefix $StackName `
    --output-template-file packaged.yaml --region $Region | Out-Null

Write-Host "== Deploying (the certificate and DNS validation take a few minutes on first deploy)" -ForegroundColor Cyan
# Empty values must still be passed (Name=) so they do not revert to defaults.
$overrides = $params.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }
& aws cloudformation deploy --template-file packaged.yaml --stack-name $StackName `
    --capabilities CAPABILITY_IAM --region $Region `
    --s3-bucket $StagingBucket --s3-prefix $StackName `
    --no-fail-on-empty-changeset `
    --parameter-overrides @overrides
if ($LASTEXITCODE -ne 0) { throw 'Deploy failed - see the stack events in the CloudFormation console.' }

# ---------------------------------------------------------------- outputs
Write-Host "`n== Outputs" -ForegroundColor Cyan
Invoke-Aws cloudformation describe-stacks --stack-name $StackName --region $Region `
    --query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' --output table

$prod = ''
try {
    $prod = Invoke-Aws sesv2 get-account --region $Region --query ProductionAccessEnabled --output text
} catch { $prod = '' }
if ($prod -ne 'True') {
    Write-Warning ("SES in $Region is in the SANDBOX: mail/send can only reach verified addresses " +
        "(200/day). Request production access - see README.md.")
}
Write-Host "`nTry it:  https://$ApiHostLabel.$Domain/v1/lists/codes" -ForegroundColor Green
Write-Host "Guide:   https://$ApiHostLabel.$Domain/v1/llms.txt" -ForegroundColor Green
