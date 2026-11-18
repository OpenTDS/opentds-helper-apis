# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Test Data Services Pty. Ltd.
<#
.SYNOPSIS
  Checks a deployed Test Helper API end to end and prints a pass/fail table.

.DESCRIPTION
  Run it straight after deploy.ps1. It needs nothing but PowerShell (Windows
  PowerShell 5.1 or PowerShell 7) - no Postman, no Python, no AWS credentials.

  Every service is exercised through its public URLs: discovery documents,
  lists (queues, variables, counters, cleanup register, import), names, lock,
  totp (codes checked against a local RFC 6238 calculation), qr, webhook, perf,
  sms (simulated texts only - nothing is sent), mail (a real email sent to the
  deployment's own domain and collected again, attachments) and voice.

  All test data lives under one namespace per run (printed at the start) and
  is cleared again at the end. Exit code 0 = every test passed, 1 = any failed.

  Costs: one short Amazon Polly synthesis (about 30 characters; repeats are
  served from storage) and two emails to the deployment's own domain. Use
  -Skip voice or -Skip mail to leave those out.

.EXAMPLE
  .\scripts\test-deployment.ps1 -Domain example.com
.EXAMPLE
  .\scripts\test-deployment.ps1
  (no -Domain: reads the API address from the deployed stack's outputs, using the AWS CLI)
.EXAMPLE
  .\scripts\test-deployment.ps1 -Domain example.com -Only lists,mail -ResultFile results.json
#>
[CmdletBinding()]
param(
    [string]$Domain,
    [string]$BaseUrl,
    [string]$ApiHostLabel = 'api',
    [string]$StackName = 'testhelper-v1',
    [string]$Region = 'ap-southeast-2',
    [string[]]$Only,
    [string[]]$Skip,
    [int]$MailWaitSeconds = 60,
    [int]$TimeoutSeconds = 35,
    [string]$ResultFile
)

$ErrorActionPreference = 'Stop'

# -Only / -Skip take a list; 'pwsh -File' passes "a,b" as one string, so split it here.
$AllGroups = 'discovery', 'lists', 'names', 'lock', 'totp', 'qr', 'webhook', 'perf', 'sms', 'mail', 'voice'
$Only = @($Only | ForEach-Object { "$_" -split ',' } | ForEach-Object { $_.Trim().ToLowerInvariant() } | Where-Object { $_ })
$Skip = @($Skip | ForEach-Object { "$_" -split ',' } | ForEach-Object { $_.Trim().ToLowerInvariant() } | Where-Object { $_ })
foreach ($g in $Only + $Skip) {
    if ($AllGroups -notcontains $g) { throw "Unknown group '$g'. Groups: $($AllGroups -join ', ')" }
}

[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
Add-Type -AssemblyName System.Net.Http

# ------------------------------------------------------------------ target
if (-not $BaseUrl) {
    if (-not $Domain) {
        if (Get-Command aws -ErrorAction SilentlyContinue) {
            $found = & { $ErrorActionPreference = 'Continue'
                aws cloudformation describe-stacks --stack-name $StackName --region $Region `
                    --query "Stacks[0].Outputs[?OutputKey=='BaseUrl'].OutputValue" --output text 2>$null }
            if ($LASTEXITCODE -eq 0 -and $found -and "$found" -ne 'None') { $BaseUrl = "$found".Trim() }
        }
        if (-not $BaseUrl) { $Domain = Read-Host 'Domain the API was deployed to (e.g. example.com)' }
    }
    if (-not $BaseUrl) { $BaseUrl = "https://$ApiHostLabel.$($Domain.Trim().TrimEnd('.').ToLowerInvariant())/v1" }
}
$BaseUrl = $BaseUrl.TrimEnd('/')
$RootUrl = $BaseUrl -replace '/v1$', ''

$chars = 'abcdefghijkmnpqrstuvwxyz23456789'
$rand = -join (1..6 | ForEach-Object { $chars[(Get-Random -Maximum $chars.Length)] })
$NS = 'tdt-' + (Get-Date).ToUniversalTime().ToString('yyyyMMddHHmmss') + '-' + $rand

$handler = New-Object System.Net.Http.HttpClientHandler
$handler.AllowAutoRedirect = $false
$http = New-Object System.Net.Http.HttpClient($handler)
$http.Timeout = [TimeSpan]::FromSeconds($TimeoutSeconds)
[void]$http.DefaultRequestHeaders.UserAgent.TryParseAdd('opentds-test-deployment/1.0')

# ------------------------------------------------------------------ HTTP
function Get-Url([string]$Path, $Query) {
    $u = if ($Path -match '^https?://') { $Path } elseif ($Path.StartsWith('/')) { "$RootUrl$Path" } elseif ($Path) { "$BaseUrl/$Path" } else { $BaseUrl }
    if ($Query) {
        $pairs = foreach ($k in $Query.Keys) { "$k=" + [Uri]::EscapeDataString([string]$Query[$k]) }
        $u += $(if ($u.Contains('?')) { '&' } else { '?' }) + ($pairs -join '&')
    }
    return $u
}

function Invoke-Api {
    param([string]$Path, $Query, [string]$Method = 'GET', $Body, [string]$ContentType = 'application/json', $Headers)
    $url = Get-Url $Path $Query
    $req = New-Object System.Net.Http.HttpRequestMessage((New-Object System.Net.Http.HttpMethod($Method)), $url)
    if ($Headers) { foreach ($k in $Headers.Keys) { [void]$req.Headers.TryAddWithoutValidation($k, [string]$Headers[$k]) } }
    if ($null -ne $Body) {
        if ($Body -is [byte[]]) { $req.Content = New-Object System.Net.Http.ByteArrayContent(, $Body) }
        else { $req.Content = New-Object System.Net.Http.StringContent([string]$Body, [Text.Encoding]::UTF8) }
        $req.Content.Headers.ContentType = [System.Net.Http.Headers.MediaTypeHeaderValue]::Parse($ContentType)
    }
    $script:lastCall = "$Method $url"
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $res = $http.SendAsync($req).GetAwaiter().GetResult()
    $bytes = $res.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult()
    $sw.Stop()
    $ctype = if ($res.Content.Headers.ContentType) { $res.Content.Headers.ContentType.MediaType } else { '' }
    $text = [Text.Encoding]::UTF8.GetString($bytes)
    $json = $null
    if ($ctype -match 'json' -and $text) { try { $json = $text | ConvertFrom-Json } catch { $json = $null } }
    $hdr = @{}
    foreach ($h in $res.Headers) { $hdr[$h.Key.ToLowerInvariant()] = ($h.Value -join ', ') }
    foreach ($h in $res.Content.Headers) { $hdr[$h.Key.ToLowerInvariant()] = ($h.Value -join ', ') }
    $script:lastCall = "$Method $url -> $([int]$res.StatusCode)"
    return [pscustomobject]@{ Status = [int]$res.StatusCode; ContentType = $ctype; Text = $text; Json = $json
        Bytes = $bytes; Headers = $hdr; Ms = $sw.ElapsedMilliseconds }
}

# ------------------------------------------------------------------ checks
function Fail([string]$Message) { throw (New-Object System.Exception($Message)) }
function Expect-Status($r, [int]$Status) {
    if ($r.Status -ne $Status) {
        $detail = if ($r.Json -and $r.Json.error) { [string]$r.Json.error } else { ($r.Text -replace '\s+', ' ') }
        if ($detail.Length -gt 160) { $detail = $detail.Substring(0, 160) + '...' }
        Fail "expected HTTP $Status, got $($r.Status): $detail"
    }
}
function Expect-Equal($Actual, $Expected, [string]$What) {
    # Numbers compare as numbers: Windows PowerShell 5.1 reads 110.0 as 110.0, PowerShell 7 as 110.
    $a = 0.0; $e = 0.0
    if ($null -ne $Actual -and $null -ne $Expected -and $Actual -isnot [bool] -and $Expected -isnot [bool] -and
        [double]::TryParse("$Actual", [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture, [ref]$a) -and
        [double]::TryParse("$Expected", [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture, [ref]$e)) {
        if ($a -ne $e) { Fail "$What`: expected $Expected, got $Actual" }
        return
    }
    if ("$Actual" -ne "$Expected") { Fail "$What`: expected '$Expected', got '$Actual'" }
}
# A response field that must exist. Reading a missing 'count' would otherwise give
# PowerShell 7's built-in Count (1) and Windows PowerShell 5.1's $null - a false pass or fail.
function Need($Object, [string]$Name) {
    if ($null -eq $Object -or @($Object.PSObject.Properties.Name) -notcontains $Name) { Fail "the response has no '$Name' field" }
    return $Object.$Name
}
function Expect-True($Condition, [string]$What) { if (-not $Condition) { Fail $What } }
function Get-Json($r, [int]$Status = 200) {
    Expect-Status $r $Status
    if ($null -eq $r.Json) { Fail "expected JSON, got '$($r.ContentType)'" }
    return $r.Json
}

# ------------------------------------------------------------------ runner
$results = New-Object System.Collections.Generic.List[object]
function Test-Group([string]$Name) {
    if ($Only.Count -and $Only -notcontains $Name) { return $false }
    if ($Skip.Count -and $Skip -contains $Name) {
        $results.Add([pscustomobject]@{ Group = $Name; Test = '(skipped)'; Result = 'SKIP'; Ms = 0; Detail = 'skipped with -Skip' })
        Write-Host ''
        Write-Host ("  {0}  (skipped)" -f $Name) -ForegroundColor DarkGray
        return $false
    }
    Write-Host ''
    Write-Host "  $Name" -ForegroundColor Cyan
    return $true
}
function Test-Step([string]$Group, [string]$Name, [scriptblock]$Body) {
    $script:lastCall = ''
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $result = 'PASS'; $detail = ''
    try { & $Body | Out-Null }
    catch {
        $result = 'FAIL'
        $e = $_.Exception
        if ($e -is [System.Management.Automation.MethodInvocationException] -or $e.InnerException) {
            while ($e.InnerException) { $e = $e.InnerException }
            $detail = "request failed: $($e.Message)"
        } else { $detail = $e.Message }
        if ($script:lastCall) { $detail += "  [$($script:lastCall)]" }
    }
    $sw.Stop()
    $results.Add([pscustomobject]@{ Group = $Group; Test = $Name; Result = $result; Ms = $sw.ElapsedMilliseconds; Detail = $detail })
    $colour = if ($result -eq 'PASS') { 'Green' } else { 'Red' }
    Write-Host ("    {0}  {1,-52} {2,6} ms" -f $result, $Name, $sw.ElapsedMilliseconds) -ForegroundColor $colour
    if ($detail) { Write-Host "          $detail" -ForegroundColor DarkYellow }
}

# RFC 6238 TOTP, to check the API's codes independently.
function Get-LocalTotp([string]$Secret, [int]$Digits, [int]$Period, [long]$UnixTime) {
    $alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'
    $bits = 0; $value = 0; $key = New-Object System.Collections.Generic.List[byte]
    foreach ($c in $Secret.ToUpperInvariant().TrimEnd('=').ToCharArray()) {
        $value = (($value -shl 5) -bor $alphabet.IndexOf($c)) -band 0xFFFF; $bits += 5
        if ($bits -ge 8) { $bits -= 8; $key.Add([byte](($value -shr $bits) -band 0xFF)) }
    }
    $counter = [BitConverter]::GetBytes([long][Math]::Floor($UnixTime / $Period))
    if ([BitConverter]::IsLittleEndian) { [Array]::Reverse($counter) }
    $hmac = New-Object System.Security.Cryptography.HMACSHA1(, $key.ToArray())
    $h = $hmac.ComputeHash($counter)
    $o = $h[19] -band 0x0F
    $num = ([long]($h[$o] -band 0x7F) -shl 24) -bor ([long]$h[$o + 1] -shl 16) -bor ([long]$h[$o + 2] -shl 8) -bor [long]$h[$o + 3]
    return ($num % [long][Math]::Pow(10, $Digits)).ToString().PadLeft($Digits, '0')
}
function Get-UnixTime { return [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() }
function Get-ValidTotp([string]$Secret, [int]$Digits, [int]$Period) {
    $now = Get-UnixTime
    return @(-$Period, 0, $Period) | ForEach-Object { Get-LocalTotp $Secret $Digits $Period ($now + $_) }
}

# ------------------------------------------------------------------ start
Write-Host ''
Write-Host 'Test Helper API - deployment check' -ForegroundColor Cyan
Write-Host "API        $BaseUrl"
Write-Host "Namespace  $NS   (all test data is created under this and cleared at the end)"

# A new custom domain can take a minute or two to resolve, and a resolver that
# asked too early caches the 'not found' for a while: retry before giving up.
$deadline = (Get-Date).AddSeconds(120); $index = $null; $lastError = ''
while (-not $index -and (Get-Date) -lt $deadline) {
    try {
        $r = Invoke-Api ''
        if ($r.Status -eq 200 -and $r.Json) { $index = $r.Json; $indexText = $r.Text } else { $lastError = "HTTP $($r.Status)" }
    } catch {
        $e = $_.Exception; while ($e.InnerException) { $e = $e.InnerException }; $lastError = $e.Message
    }
    if (-not $index) { Write-Host "  waiting for $BaseUrl ($lastError)..." -ForegroundColor DarkYellow; Start-Sleep -Seconds 10 }
}
if (-not $index) {
    Write-Host ''
    Write-Host "Cannot reach $BaseUrl : $lastError" -ForegroundColor Red
    Write-Host 'If the deploy has only just finished, DNS may still be settling - try again in a few minutes.'
    Write-Host 'Check the address too: -Domain must be the domain deploy.ps1 was given.'
    exit 1
}
$MailDomain = [string]$index.mailDomain
Write-Host "Mail       *@$MailDomain"

# ================================================================== discovery
if (Test-Group 'discovery') {
    $services = @('lists', 'names', 'lock', 'mail', 'sms', 'webhook', 'totp', 'qr', 'perf', 'voice')
    Test-Step discovery 'index lists every service' {
        foreach ($s in $services) { Expect-True ($indexText.Contains("/v1/$s")) "service '$s' is missing from /v1" }
        Expect-True ($MailDomain -match '\.') 'mailDomain in /v1'
    }
    Test-Step discovery 'each service lists its actions' {
        foreach ($s in $services) {
            $b = Get-Json (Invoke-Api $s)
            Expect-True (@($b.actions).Count -gt 0) "/v1/$s lists no actions"
        }
    }
    # Checked as text: the documents are large, and Windows PowerShell 5.1's JSON parser is slow on them.
    function Test-OpenApi([string]$Path, [string]$Version) {
        $r = Invoke-Api $Path; Expect-Status $r 200
        Expect-True ($r.Text -match ('"openapi"\s*:\s*"' + [regex]::Escape($Version))) "$Path is not OpenAPI $Version"
        $paths = [regex]::Matches($r.Text, '"/[a-z]+/[a-z/]+"\s*:\s*\{').Count
        Expect-True ($paths -gt 40) "$Path lists only $paths paths"
    }
    Test-Step discovery 'openapi.json (3.2) and openapi-3.0.json' {
        Test-OpenApi 'openapi.json' '3.'; Test-OpenApi 'openapi-3.0.json' '3.0.'
    }
    Test-Step discovery 'llms.txt guide (v1 and domain root)' {
        foreach ($p in 'llms.txt', '/llms.txt') {
            $r = Invoke-Api $p; Expect-Status $r 200
            Expect-True ($r.Text.Length -gt 1000 -and $r.Text.Contains('lists/push')) "$p does not look like the guide"
        }
    }
    Test-Step discovery '.well-known api-catalog and openapi' {
        $r = Invoke-Api '/.well-known/api-catalog'; Expect-Status $r 200
        Expect-True ($r.Text.Contains('service-desc')) 'api-catalog has no service-desc'
        Test-OpenApi '/.well-known/openapi.json' '3.'
    }
    Test-Step discovery 'unknown action is a 404 with the error shape' {
        $b = Get-Json (Invoke-Api 'lists/nosuchaction' @{ key = $NS }) 404
        Expect-Equal $b.status 404 'error.status'; Expect-True ($b.error) 'error message'
    }
    Test-Step discovery 'CORS preflight' {
        $r = Invoke-Api 'lists/codes' -Method OPTIONS
        Expect-True ($r.Status -eq 204 -or $r.Status -eq 200) "OPTIONS returned $($r.Status)"
        Expect-Equal $r.Headers['access-control-allow-origin'] '*' 'Access-Control-Allow-Origin'
    }
}

# ================================================================== lists
if (Test-Group 'lists') {
    $k = "$NS-list"
    Test-Step lists 'codes' {
        $b = Get-Json (Invoke-Api 'lists/codes')
        Expect-True ("$($b.rand10)" -cmatch '^[A-Z0-9]{10}$') "rand10 '$($b.rand10)'"
    }
    Test-Step lists 'push three records' {
        foreach ($i in 1..3) {
            $b = Get-Json (Invoke-Api 'lists/push' ([ordered]@{ key = $k; n = "$i"; email = "user$i@example.com" }))
            Expect-Equal $b.index $i 'index'; Expect-Equal (Need $b count) $i 'count'
        }
    }
    Test-Step lists 'list with paging' {
        $b = Get-Json (Invoke-Api 'lists/list' ([ordered]@{ key = $k; start = 2; limit = 1 }))
        Expect-Equal (Need $b count) 3 'count'; Expect-Equal @($b.records).Count 1 'records returned'; Expect-Equal $b.nextStart 3 'nextStart'
    }
    Test-Step lists 'pop in order, then exhausted (200, not an error)' {
        foreach ($i in 1..3) { $b = Get-Json (Invoke-Api 'lists/pop' @{ key = $k }); Expect-Equal $b.record.n "$i" "record $i" }
        $b = Get-Json (Invoke-Api 'lists/pop' @{ key = $k }); Expect-Equal $b.exhausted $true 'exhausted'
    }
    Test-Step lists 'reset rewinds; random and cyclepop' {
        $b = Get-Json (Invoke-Api 'lists/reset' @{ key = $k }); Expect-Equal $b.reset $true 'reset'
        $b = Get-Json (Invoke-Api 'lists/pop' @{ key = $k }); Expect-Equal $b.index 1 'index after reset'
        $b = Get-Json (Invoke-Api 'lists/random' @{ key = $k }); Expect-True ($b.index -ge 1 -and $b.index -le 3) 'random index'
        $b = Get-Json (Invoke-Api 'lists/cyclepop' @{ key = $k }); Expect-True ($b.index -ge 1 -and $b.index -le 3) 'cyclepop index'
    }
    Test-Step lists 'variables: set, get, list, missing is 404' {
        Get-Json (Invoke-Api 'lists/vars/set' ([ordered]@{ key = $k; name = 'orderId'; value = 'ORD-42 & more' })) | Out-Null
        $b = Get-Json (Invoke-Api 'lists/vars/get' ([ordered]@{ key = $k; name = 'orderId' }))
        Expect-Equal $b.value 'ORD-42 & more' 'value'
        $b = Get-Json (Invoke-Api 'lists/vars/list' @{ key = $k }); Expect-Equal (Need $b count) 1 'variables'
        Get-Json (Invoke-Api 'lists/vars/get' ([ordered]@{ key = $k; name = 'missing' })) 404 | Out-Null
    }
    Test-Step lists 'counters: add, subtract, get' {
        Get-Json (Invoke-Api 'lists/counters/add' ([ordered]@{ key = $k; name = 'orders'; increment = 5 })) | Out-Null
        $b = Get-Json (Invoke-Api 'lists/counters/add' ([ordered]@{ key = $k; name = 'orders'; increment = -2 })); Expect-Equal $b.value 3 'value'
        $b = Get-Json (Invoke-Api 'lists/counters/get' ([ordered]@{ key = $k; name = 'orders' })); Expect-Equal $b.value 3 'value'
    }
    Test-Step lists 'cleanup register: push, pop, list' {
        Get-Json (Invoke-Api 'lists/cleanup/push' ([ordered]@{ key = $k; value = 'customer-1001'; note = 'created by test' })) | Out-Null
        $b = Get-Json (Invoke-Api 'lists/cleanup/pop' @{ key = $k }); Expect-Equal $b.value 'customer-1001' 'value'
        $b = Get-Json (Invoke-Api 'lists/cleanup/list' @{ key = $k })
        Expect-Equal (Need $b saved) 1 'saved'; Expect-Equal (Need $b removed) 1 'removed'; Expect-Equal (Need $b pending) 0 'pending'
    }
    Test-Step lists 'import JSON lines (POST) and read back' {
        $ik = "$k-import"
        $body = (1..25 | ForEach-Object { '{"user":"u' + $_ + '","pin":"' + (1000 + $_) + '"}' }) -join "`n"
        $b = Get-Json (Invoke-Api 'lists/import' ([ordered]@{ key = $ik; format = 'json' }) -Method POST -Body $body)
        Expect-Equal $b.state 'done' 'import state'; Expect-Equal $b.loaded 25 'loaded'
        $b = Get-Json (Invoke-Api 'lists/pop' @{ key = $ik }); Expect-Equal $b.record.user 'u1' 'first record'
        Get-Json (Invoke-Api 'lists/clear' @{ key = $ik }) | Out-Null
    }
    Test-Step lists 'bad requests are 400s' {
        Get-Json (Invoke-Api 'lists/push' @{ a = '1' }) 400 | Out-Null
        Get-Json (Invoke-Api 'lists/push' ([ordered]@{ key = '#bad'; a = '1' })) 400 | Out-Null
    }
    Test-Step lists 'clear' {
        $b = Get-Json (Invoke-Api 'lists/clear' @{ key = $k }); Expect-Equal $b.cleared $true 'cleared'
        $b = Get-Json (Invoke-Api 'lists/pop' @{ key = $k }); Expect-Equal $b.exhausted $true 'empty after clear'
    }
}

# ================================================================== names
if (Test-Group 'names') {
    Test-Step names 'regions' {
        $b = Get-Json (Invoke-Api 'names/regions'); Expect-True ((Need $b count) -ge 15) "only $((Need $b count)) regions"
    }
    Test-Step names 'persona for a region' {
        $b = Get-Json (Invoke-Api 'names/get' @{ region = 'AU' })
        Expect-Equal $b.region 'AU' 'region'
        foreach ($f in 'femaleFullName', 'maleFullName', 'femaleLastName') { Expect-True ($b.$f) "$f missing" }
    }
    Test-Step names 'same seed, same persona' {
        $a = Get-Json (Invoke-Api 'names/get' ([ordered]@{ region = 'GB'; seed = $NS }))
        $b = Get-Json (Invoke-Api 'names/get' ([ordered]@{ region = 'gb'; seed = $NS }))
        Expect-Equal $b.femaleFullName $a.femaleFullName 'femaleFullName'
    }
    Test-Step names 'notregion, and an unknown region is a 404' {
        $b = Get-Json (Invoke-Api 'names/get' @{ notregion = 'AU' }); Expect-True ($b.region -ne 'AU') 'excluded region was chosen'
        Get-Json (Invoke-Api 'names/get' @{ region = 'zz' }) 404 | Out-Null
    }
}

# ================================================================== lock
if (Test-Group 'lock') {
    $k = "$NS-lock"
    Test-Step lock 'acquire, contend, release, re-acquire' {
        $a = Get-Json (Invoke-Api 'lock/acquire' @{ key = $k }); Expect-Equal $a.locked $true 'first acquire'
        $b = Get-Json (Invoke-Api 'lock/acquire' ([ordered]@{ key = $k; wait = 0 })); Expect-Equal $b.locked $false 'second acquire while held'
        $c = Get-Json (Invoke-Api 'lock/release' @{ ref = $a.ref }); Expect-Equal $c.released $true 'release'
        $d = Get-Json (Invoke-Api 'lock/acquire' @{ key = $k }); Expect-Equal $d.locked $true 'acquire after release'
        $e = Get-Json (Invoke-Api $d.releaseUrl); Expect-Equal $e.released $true 'release via releaseUrl'
    }
    Test-Step lock 'missing key is a 400' { Get-Json (Invoke-Api 'lock/acquire') 400 | Out-Null }
}

# ================================================================== totp
if (Test-Group 'totp') {
    $secret = 'JBSWY3DPEHPK3PXP'
    Test-Step totp 'stateless code matches RFC 6238' {
        $b = Get-Json (Invoke-Api 'totp/get' @{ secret = $secret }); $valid = Get-ValidTotp $secret 6 30
        Expect-True ($valid -contains $b.code) "code $($b.code) is not a valid TOTP for the secret (local: $($valid -join '/'))"
    }
    Test-Step totp '8 digits, 60 s period' {
        $b = Get-Json (Invoke-Api 'totp/get' ([ordered]@{ secret = $secret; digits = 8; period = 60 })); $valid = Get-ValidTotp $secret 8 60
        Expect-True ($valid -contains $b.code) "code $($b.code) (local: $($valid -join '/'))"
    }
    Test-Step totp 'stored secret: set, get, unknown user is 404' {
        $k = "$NS-totp"
        $b = Get-Json (Invoke-Api 'totp/set' ([ordered]@{ key = $k; user = 'ann@example.com'; secret = $secret })); Expect-Equal $b.saved $true 'saved'
        $b = Get-Json (Invoke-Api 'totp/get' ([ordered]@{ key = $k; user = 'ann@example.com' })); $valid = Get-ValidTotp $secret 6 30
        Expect-True ($valid -contains $b.code) "stored code $($b.code) (local: $($valid -join '/'))"
        Get-Json (Invoke-Api 'totp/get' ([ordered]@{ key = $k; user = 'nobody' })) 404 | Out-Null
    }
}

# ================================================================== qr
if (Test-Group 'qr') {
    $text = "https://example.com/join?code=$NS&x=a+b"
    Test-Step qr 'PNG (JSON)' {
        $b = Get-Json (Invoke-Api 'qr/get' @{ text = $text }); Expect-Equal $b.text $text 'text echoed'
        $png = [Convert]::FromBase64String($b.png)
        Expect-Equal ([BitConverter]::ToString($png, 0, 8)) '89-50-4E-47-0D-0A-1A-0A' 'PNG signature'
        Expect-Equal $png.Length $b.pngBytes 'pngBytes'
    }
    Test-Step qr 'SVG and HTML' {
        $r = Invoke-Api 'qr/get' ([ordered]@{ text = $text; format = 'svg' }); Expect-Status $r 200
        Expect-Equal $r.ContentType 'image/svg+xml' 'content type'; Expect-True ($r.Text.Contains('<svg')) 'svg element'
        $r = Invoke-Api 'qr/get' ([ordered]@{ text = $text; format = 'html' }); Expect-Status $r 200
        Expect-True ($r.Text.Contains('data:image/png;base64,')) 'embedded image'
    }
    Test-Step qr 'missing text is a 400' { Get-Json (Invoke-Api 'qr/get') 400 | Out-Null }
}

# ================================================================== webhook
if (Test-Group 'webhook') {
    $k = "$NS-hook"
    Test-Step webhook 'capture a JSON POST and a forced 503' {
        $b = Get-Json (Invoke-Api 'webhook/receive' ([ordered]@{ key = $k; source = 'test' }) -Method POST `
                -Body '{"event":"order.paid","amount":10.5}' -Headers @{ 'X-Signature' = 'sha256=abc' })
        Expect-Equal $b.received $true 'received'
        $r = Invoke-Api 'webhook/receive' ([ordered]@{ key = $k; status = 503 }) -Method PUT -Body 'retry me' -ContentType 'text/plain'
        Expect-Equal $r.Status 503 'forced status'
    }
    Test-Step webhook 'list shows both, newest first, as sent' {
        $b = Get-Json (Invoke-Api 'webhook/list' @{ key = $k }); Expect-Equal (Need $b count) 2 'events'
        $post = @($b.events) | Where-Object { $_.method -eq 'POST' } | Select-Object -First 1
        Expect-True $post 'the POST event'
        Expect-Equal $post.json.event 'order.paid' 'parsed JSON body'
        Expect-Equal $post.headers.'x-signature' 'sha256=abc' 'custom header'
        Expect-Equal @($b.events)[0].method 'PUT' 'newest first'
    }
    Test-Step webhook 'clear' {
        $b = Get-Json (Invoke-Api 'webhook/clear' @{ key = $k }); Expect-Equal $b.deleted 2 'deleted'
    }
}

# ================================================================== perf
if (Test-Group 'perf') {
    $k = "$NS-perf"
    $values = 1..50 | ForEach-Object { 100 + $_ * 10 }        # 110 .. 600
    Test-Step perf 'add a batch of timings and a failure' {
        $b = Get-Json (Invoke-Api 'perf/add' ([ordered]@{ key = $k; value = ($values -join ','); raw = 'true' }))
        Expect-Equal $b.recorded 50 'recorded'
        Get-Json (Invoke-Api 'perf/add' ([ordered]@{ key = $k; value = '5000'; ok = 'false'; raw = 'true' })) | Out-Null
    }
    Test-Step perf 'statistics' {
        $b = Get-Json (Invoke-Api 'perf/stats' @{ key = $k })
        Expect-Equal (Need $b count) 51 'count'; Expect-Equal $b.errors 1 'errors'
        Expect-Equal $b.min 110 'min'; Expect-Equal $b.max 5000 'max'
        Expect-True ([Math]::Abs($b.p50 - 360) -le 15) "p50 $($b.p50) (expected about 360)"
    }
    Test-Step perf 'assert: pass, and a strict failure is a 412' {
        $b = Get-Json (Invoke-Api 'perf/assert' ([ordered]@{ key = $k; p50lt = 1000 })); Expect-Equal $b.pass $true 'p50 < 1000'
        Get-Json (Invoke-Api 'perf/assert' ([ordered]@{ key = $k; p50lt = 10; strict = 'true' })) 412 | Out-Null
    }
    Test-Step perf 'series, keys and CSV export' {
        $b = Get-Json (Invoke-Api 'perf/series' ([ordered]@{ key = $k; interval = 60 })); Expect-True ($b.buckets -ge 1) 'buckets'
        $b = Get-Json (Invoke-Api 'perf/keys' @{ prefix = $NS })
        $keys = @($b.keys) | ForEach-Object { if ($_ -is [string]) { $_ } else { $_.key } }
        Expect-True ($keys -contains $k) 'key listed by perf/keys'
        $r = Invoke-Api 'perf/export' ([ordered]@{ key = $k; format = 'csv' }); Expect-Status $r 200
        Expect-Equal $r.ContentType 'text/csv' 'content type'
        Expect-True (@($r.Text.Trim() -split "`n").Count -ge 51) 'CSV rows'
    }
    Test-Step perf 'clear' {
        Get-Json (Invoke-Api 'perf/clear' @{ key = $k }) | Out-Null
        $b = Get-Json (Invoke-Api 'perf/stats' @{ key = $k }); Expect-Equal (Need $b count) 0 'count after clear'
    }
}

# ================================================================== sms
if (Test-Group 'sms') {
    # Simulated texts only: a random +614 number that is not a registered handset.
    $number = '+614' + (-join (1..8 | ForEach-Object { Get-Random -Maximum 10 }))
    $since = (Get-UnixTime) - 1
    $script:sent = $null
    Test-Step sms 'nothing for a new number' {
        $b = Get-Json (Invoke-Api 'sms/get' @{ number = $number }); Expect-Equal $b.found $false 'found'
    }
    Test-Step sms 'mocksend with a generated code' {
        $script:sent = Get-Json (Invoke-Api 'sms/mocksend' ([ordered]@{ number = $number; body = 'Your code is {code}'; from = 'TestRun' }))
        Expect-Equal $script:sent.mocked $true 'mocked'
        Expect-True ("$($script:sent.code)" -match '^\d{6}$') "code '$($script:sent.code)'"
    }
    Test-Step sms 'get and list return that text' {
        $b = Get-Json (Invoke-Api 'sms/get' ([ordered]@{ number = $number; since = $since }))
        Expect-Equal $b.found $true 'found'; Expect-Equal $b.body "Your code is $($script:sent.code)" 'body'
        $l = Get-Json (Invoke-Api 'sms/list' ([ordered]@{ number = $number; seconds = 300 })); Expect-Equal (Need $l count) 1 'sms/list count'
    }
    Test-Step sms 'real send to an unregistered number is refused' {
        Get-Json (Invoke-Api 'sms/send' @{ number = $number }) 403 | Out-Null
    }
    Test-Step sms 'receive without the device key is refused' {
        Get-Json (Invoke-Api 'sms/receive' @{ number = $number } -Method POST -Body '{"sender":"x","body":"y"}') 401 | Out-Null
    }
}

# ================================================================== mail
if (Test-Group 'mail') {
    $address = "$NS-inbox@$MailDomain"
    $from = "test-deployment@$MailDomain"
    $code = -join (1..6 | ForEach-Object { Get-Random -Maximum 10 })
    $subject = "Deployment check $NS"
    function Wait-Mail([string]$Address) {
        $start = Get-Date; $end = $start.AddSeconds($MailWaitSeconds)
        do {
            $left = [int][Math]::Ceiling(($end - (Get-Date)).TotalSeconds)
            $b = Get-Json (Invoke-Api 'mail/pop' ([ordered]@{ address = $Address; wait = [Math]::Max(1, [Math]::Min(20, $left)) }))
            if (-not $b.exhausted) { return $b }
        } while ((Get-Date) -lt $end)
        $waited = [int]((Get-Date) - $start).TotalSeconds
        Fail "no email arrived at $Address after $waited s - check the SES receipt rule and the MX record"
    }
    Test-Step mail 'empty inbox pops as exhausted' {
        $b = Get-Json (Invoke-Api 'mail/pop' @{ address = $address }); Expect-Equal $b.exhausted $true 'exhausted'
    }
    Test-Step mail 'send to own domain, then receive it (SES round trip)' {
        $b = Get-Json (Invoke-Api 'mail/send' ([ordered]@{ from = $from; to = $address; subject = $subject; body = "Your code is $code." }))
        Expect-Equal $b.result 'sent' 'send result'
        $m = Wait-Mail $address
        Expect-Equal $m.subject $subject 'subject'; Expect-True ("$($m.body)" -match "code is $code") 'code in the body'
    }
    Test-Step mail 'generated attachment arrives' {
        Get-Json (Invoke-Api 'mail/send' ([ordered]@{ from = $from; to = $address; subject = "$subject (attachment)"; body = 'see attached'; attachmentkb = 20; filename = 'report.pdf' })) | Out-Null
        $m = Wait-Mail $address
        Expect-Equal @($m.attachments).Count 1 'attachments'
        Expect-Equal @($m.attachments)[0].filename 'report.pdf' 'attachment name'
    }
    Test-Step mail 'sending outside the allow list is refused' {
        Get-Json (Invoke-Api 'mail/send' ([ordered]@{ from = $from; to = 'someone@example.com'; subject = 'x'; body = 'x' })) 403 | Out-Null
    }
    Test-Step mail 'attachment store: save, get, find, delete' {
        $ak = "$NS-att"; $content = "Invoice INV-$code total 42.00"
        $b = Get-Json (Invoke-Api 'mail/attachments/save' ([ordered]@{ key = $ak; filename = 'invoice.txt' }) -Method POST `
                -Body ([Text.Encoding]::UTF8.GetBytes($content)) -ContentType 'application/octet-stream')
        Expect-Equal $b.result 'saved' 'saved'
        $g = Get-Json (Invoke-Api 'mail/attachments/get' @{ key = $ak })
        Expect-Equal ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($g.data))) $content 'content'
        $f = Get-Json (Invoke-Api 'mail/attachments/find' ([ordered]@{ key = $ak; text = "INV-$code" }))
        Expect-True ((Need $f count) -ge 1) 'text found in the attachment'
        Get-Json (Invoke-Api 'mail/attachments/delete' @{ key = $ak }) | Out-Null
        Get-Json (Invoke-Api 'mail/attachments/get' @{ key = $ak }) 404 | Out-Null
    }
    Test-Step mail 'clear the inbox' { Get-Json (Invoke-Api 'mail/clear' @{ address = $address }) | Out-Null }
}

# ================================================================== voice
if (Test-Group 'voice') {
    $sayText = 'Deployment check, one two three.'
    $script:voiceRef = $null
    Test-Step voice 'voices and tags' {
        $b = Get-Json (Invoke-Api 'voice/voices'); Expect-True (@($b.voices).Count -ge 1) 'voices'
        $b = Get-Json (Invoke-Api 'voice/tags'); Expect-True (@($b.tags).Count -ge 1) 'tags'
    }
    Test-Step voice 'say, then fetch the audio' {
        $b = Get-Json (Invoke-Api 'voice/say' ([ordered]@{ text = $sayText; url = 'true' }))
        Expect-True ($b.ref) 'ref'; Expect-True ($b.url) 'url'
        $script:voiceRef = $b.ref
        $r = Invoke-Api $b.url; Expect-Status $r 200
        Expect-True ($r.Bytes.Length -gt 500) "the audio is only $($r.Bytes.Length) bytes"
    }
    Test-Step voice 'saying it again is served from storage' {
        $b = Get-Json (Invoke-Api 'voice/say' ([ordered]@{ text = $sayText; url = 'true' }))
        Expect-Equal $b.ref $script:voiceRef 'same ref'; Expect-Equal $b.cached $true 'cached'
        $g = Get-Json (Invoke-Api 'voice/get' ([ordered]@{ ref = $script:voiceRef; url = 'true' })); Expect-True ($g.url) 'voice/get url'
    }
}

# ================================================================== summary
$http.Dispose()
Write-Host ''
$groups = $results | Group-Object Group | Sort-Object { [array]::IndexOf($AllGroups, $_.Name) } | ForEach-Object {
    $g = @($_.Group)
    $nFail = @($g | Where-Object { $_.Result -eq 'FAIL' }).Count
    $nSkip = @($g | Where-Object { $_.Result -eq 'SKIP' }).Count
    [pscustomobject]@{
        Group    = $_.Name
        Result   = $(if ($nFail) { 'FAIL' } elseif ($nSkip) { 'SKIP' } else { 'PASS' })
        Passed   = @($g | Where-Object { $_.Result -eq 'PASS' }).Count
        Failed   = $nFail
        'Time s' = '{0:N1}' -f (($g | Measure-Object Ms -Sum).Sum / 1000)
    }
}
$groups | Format-Table -AutoSize | Out-Host

$failed = @($results | Where-Object { $_.Result -eq 'FAIL' })
$passed = @($results | Where-Object { $_.Result -eq 'PASS' }).Count
if ($failed.Count) {
    Write-Host 'Failures:' -ForegroundColor Red
    $failed | ForEach-Object { Write-Host ("  {0} / {1}: {2}" -f $_.Group, $_.Test, $_.Detail) -ForegroundColor Red }
    Write-Host ''
}
$colour = if ($failed.Count) { 'Red' } else { 'Green' }
Write-Host ("{0} passed, {1} failed - {2}" -f $passed, $failed.Count, $BaseUrl) -ForegroundColor $colour

if ($ResultFile) {
    if ($ResultFile -like '*.csv') { $results | Export-Csv -Path $ResultFile -NoTypeInformation }
    else {
        [pscustomobject]@{ baseUrl = $BaseUrl; namespace = $NS; time = (Get-Date).ToUniversalTime().ToString('o')
            passed = $passed; failed = $failed.Count; results = $results } |
            ConvertTo-Json -Depth 5 | Set-Content -Path $ResultFile -Encoding UTF8
    }
    Write-Host "Results written to $ResultFile"
}
exit $(if ($failed.Count) { 1 } else { 0 })
