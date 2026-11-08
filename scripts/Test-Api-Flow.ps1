<#
.SYNOPSIS
    End-to-end API check on the master: /nodes -> /start -> poll /status -> /chat -> /stop -> /status=stopped.

.DESCRIPTION
    Run on the machine that can reach the coordinator, with the admin token in the environment:
        $env:LOWLLAMA_ADMIN_TOKEN = Read-Host 'Admin token'
        .\scripts\Test-Api-Flow.ps1 -CoordinatorUrl http://<MASTER_TAILSCALE_IP>:8000 -NodeIds <rpc0-id>,<rpc1-id> -ExpectHelpers 2

    -NodeIds order is the RPC order (first = RPC0). The script always re-reads /nodes and refuses to start if a
    selected ID is missing, offline or not rpc_ready. Without -NodeIds it uses every online + rpc_ready node,
    sorted by node_id, and prints the order.

    Cleanup: once /start has been ACCEPTED (HTTP 200), /stop is always attempted at the end, even after a
    status error, a timeout, a failed chat or Ctrl+C. If /start is refused (for example 409: a model is already
    running), nothing is stopped, so an engine this script did not start is never touched. It never kills
    processes itself.

    -PauseBeforeChat stops after the engine is ready and waits for Enter, so the master can check allocation and
    each helper can take its "before" snapshot after warmup. If the pause is interrupted (Ctrl+C), cleanup still runs.

    Prints "CHAT SENT AT <UTC time>" right before the chat request, for
    Collect-Inference-Evidence.ps1 -Mode SamePrompt -ChatSentAt <that time>.
    Never prints the admin token. Exit code 0 only when the model became ready, answered, and stopped cleanly.
#>
[CmdletBinding()]
param(
    [string]$CoordinatorUrl = 'http://127.0.0.1:8000',
    [string]$AdminToken = $env:LOWLLAMA_ADMIN_TOKEN,            # prefer the environment variable: command lines end up in history
    [string]$ModelId = 'qwen2.5-0.5b-instruct-q4_k_m',
    [string[]]$NodeIds = @(),
    [ValidateRange(0, 16)]
    [int]$ExpectHelpers = 0,                                    # 0 = any; 2 = refuse unless exactly two helpers are selected
    [string]$Prompt = 'What is the capital of France?',
    [ValidateRange(10, 1800)]
    [int]$ReadyTimeoutSeconds = 180,
    [ValidateRange(10, 600)]
    [int]$ChatTimeoutSeconds = 150,
    [switch]$PauseBeforeChat                                    # wait for Enter once ready, so helpers can take "before" snapshots after warmup
)

$ErrorActionPreference = 'Stop'
$baseUrl = $CoordinatorUrl.TrimEnd('/')
$NodeIds = @($NodeIds | ForEach-Object { $_ -split '[,\s]+' } | Where-Object { $_ })   # also accept "a,b" as one string
$invariant = [Globalization.CultureInfo]::InvariantCulture
$failures = New-Object System.Collections.Generic.List[string]

if (-not $AdminToken) { Write-Output 'API FLOW NOT RUN : set $env:LOWLLAMA_ADMIN_TOKEN first (it is never printed).'; exit 1 }
if ($PSBoundParameters.ContainsKey('AdminToken')) { Write-Output 'Note: -AdminToken on the command line can end up in PowerShell history; prefer $env:LOWLLAMA_ADMIN_TOKEN.' }

function Invoke-Api([string]$method, [string]$path, $body = $null, [int]$timeout = 15) {
    $params = @{ Uri = "$baseUrl$path"; Method = $method; Headers = @{ Authorization = "Bearer $AdminToken" }; UseBasicParsing = $true; TimeoutSec = $timeout }
    if ($null -ne $body) {
        $params.ContentType = 'application/json; charset=utf-8'
        $params.Body = [Text.Encoding]::UTF8.GetBytes(($body | ConvertTo-Json -Depth 6 -Compress))   # Depth 6: nested messages survive
    }
    try {
        $response = Invoke-WebRequest @params
        $text = [Text.Encoding]::UTF8.GetString($response.RawContentStream.ToArray())   # FastAPI sends UTF-8 without a charset; decode it ourselves
        $data = if ($text) { $text | ConvertFrom-Json } else { $null }
        return [pscustomobject]@{ Ok = $true; Status = [int]$response.StatusCode; Data = $data; Detail = '' }
    } catch {
        $status = 0
        $resp = $_.Exception.Response
        if ($null -ne $resp) { try { $status = [int]$resp.StatusCode } catch { $status = 0 } }
        $detail = $_.ErrorDetails.Message
        if (-not $detail -and $null -ne $resp) {
            try { $detail = (New-Object System.IO.StreamReader($resp.GetResponseStream())).ReadToEnd() } catch { $detail = '' }
        }
        if ($detail) { try { $parsed = $detail | ConvertFrom-Json; if ($parsed.detail) { $detail = [string]$parsed.detail } } catch { } }
        if (-not $detail) { $detail = $_.Exception.Message }
        return [pscustomobject]@{ Ok = $false; Status = $status; Data = $null; Detail = $detail }
    }
}

function Get-Field($object, [string]$name) {   # property access that cannot collide with PowerShell's own members
    if ($null -eq $object) { return $null }
    $property = $object.PSObject.Properties[$name]
    if ($null -eq $property) { return $null }
    return $property.Value
}

function Get-Nodes {
    $result = Invoke-Api 'GET' '/nodes'
    if (-not $result.Ok) { return $result }
    $result.Data = @($result.Data | Where-Object { $null -ne $_ })   # zero, one or many nodes all become an array
    return $result
}

Write-Output "--- LowLlama API flow test ($baseUrl) ---"

$health = $null
try { $health = Invoke-WebRequest -Uri "$baseUrl/health" -UseBasicParsing -TimeoutSec 10 } catch { $health = $null }
if ($null -eq $health -or [int]$health.StatusCode -ne 200) {
    Write-Output "API FLOW FAIL : $baseUrl/health did not answer. Check the coordinator is running on that address and port, and the master firewall."
    exit 1
}

Write-Output '[1/6] Reading current nodes...'
$listing = Get-Nodes
if (-not $listing.Ok) { Write-Output "API FLOW FAIL : GET /nodes returned $($listing.Status): $($listing.Detail)"; exit 1 }
$nodes = $listing.Data
Write-Output "Registered nodes: $($nodes.Count)"
foreach ($n in $nodes) {
    Write-Output ("  {0}  {1}  status={2} rpc_ready={3}" -f (Get-Field $n 'node_id'), (Get-Field $n 'node_name'), (Get-Field $n 'status'), (Get-Field $n 'rpc_ready'))
}

if ($NodeIds.Count -eq 0) {
    $NodeIds = @($nodes | Where-Object { (Get-Field $_ 'status') -eq 'online' -and (Get-Field $_ 'rpc_ready') -eq $true } |
                 ForEach-Object { [string](Get-Field $_ 'node_id') } | Sort-Object)
    Write-Output "No -NodeIds given: using every online + rpc_ready node, sorted by node_id."
}
$byId = @{}
foreach ($n in $nodes) { $byId[[string](Get-Field $n 'node_id')] = $n }
$problems = New-Object System.Collections.Generic.List[string]
if ($NodeIds.Count -eq 0) { $problems.Add('no online + rpc_ready helper to select') }
if ($ExpectHelpers -gt 0 -and $NodeIds.Count -ne $ExpectHelpers) { $problems.Add("$($NodeIds.Count) helper(s) selected; expected $ExpectHelpers") }
if (@($NodeIds | Sort-Object -Unique).Count -ne $NodeIds.Count) { $problems.Add('the same node ID is selected twice') }
for ($i = 0; $i -lt $NodeIds.Count; $i++) {
    $id = $NodeIds[$i]
    if (-not $byId.ContainsKey($id)) { $problems.Add("$id is not in the current /nodes (stale ID after a coordinator restart?)"); continue }
    $n = $byId[$id]
    if ((Get-Field $n 'status') -ne 'online') { $problems.Add("$id is $(Get-Field $n 'status')") }
    if ((Get-Field $n 'rpc_ready') -ne $true) { $problems.Add("$id is not rpc_ready") }
    Write-Output ("  RPC{0} = {1} ({2})" -f $i, $id, (Get-Field $n 'node_name'))
}
if ($problems.Count) { Write-Output "API FLOW FAIL : not starting: $($problems -join '; ')"; exit 1 }

Write-Output "[2/6] POST /start model=$ModelId ..."
$start = Invoke-Api 'POST' '/start' @{ model_id = $ModelId; node_ids = [string[]]$NodeIds } 300
if (-not $start.Ok) {
    if ($start.Status -eq 409) { Write-Output "API FLOW FAIL : /start refused (409, busy): $($start.Detail). A model this script did not start is running; it was left alone." }
    else { Write-Output "API FLOW FAIL : /start refused ($($start.Status)): $($start.Detail)" }
    exit 1
}
$cleanupRequired = $true                                        # from here on, /stop is always attempted
$ready = $false; $answered = $false; $stopped = $false
try {
    Write-Output "Start accepted: status=$(Get-Field $start.Data 'status') workers=$(@(Get-Field $start.Data 'worker_ids') -join ',')"
    Write-Output "[3/6] Polling /status (up to $ReadyTimeoutSeconds s)..."
    $deadline = (Get-Date).AddSeconds($ReadyTimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $state = Invoke-Api 'GET' '/status'
        if (-not $state.Ok) { $failures.Add("GET /status returned $($state.Status): $($state.Detail)"); break }
        $value = Get-Field $state.Data 'status'
        if ($value -eq 'ready') { $ready = $true; break }
        if ($value -eq 'error') { $failures.Add("engine error: $(Get-Field $state.Data 'error')"); break }
        if ($value -eq 'stopped') { $failures.Add('engine stopped by itself before it was ready'); break }
        Start-Sleep -Seconds 2
    }
    if (-not $ready -and -not $failures.Count) { $failures.Add("not ready within $ReadyTimeoutSeconds s") }
    if ($ready) {
        Write-Output 'Engine is ready.'
        if ($PauseBeforeChat) {
            Write-Output 'PAUSED: run -Mode Master -ApiLog now, and ask each helper to take its -Label before snapshot. Send no other chats.'
            [void](Read-Host 'Press Enter to send the ONE chat request')
        }
        Write-Output '[4/6] POST /chat (non-streaming)...'
        $sentAt = (Get-Date).ToUniversalTime().ToString('o', $invariant)
        Write-Output "CHAT SENT AT $sentAt"
        $chat = Invoke-Api 'POST' '/chat' @{ messages = @(@{ role = 'user'; content = $Prompt }); max_tokens = 128; temperature = 0.7; stream = $false } $ChatTimeoutSeconds
        if (-not $chat.Ok) {
            $failures.Add("/chat returned $($chat.Status): $($chat.Detail)")
        } else {
            $choices = @(Get-Field $chat.Data 'choices')
            $message = if ($choices.Count) { Get-Field $choices[0] 'message' } else { $null }
            $content = [string](Get-Field $message 'content')
            if ($content.Trim()) {
                $answered = $true
                Write-Output 'Answer:'
                Write-Output $content
            } else {
                $failures.Add('/chat answered but the response had no message content')
            }
        }
    }
} finally {
    if ($cleanupRequired) {
        Write-Output '[5/6] POST /stop (cleanup of the engine this script started)...'
        $stop = Invoke-Api 'POST' '/stop' $null 60
        if (-not $stop.Ok) { $failures.Add("/stop returned $($stop.Status): $($stop.Detail)") }
        Write-Output '[6/6] Checking /status...'
        $final = Invoke-Api 'GET' '/status'
        if ($final.Ok -and (Get-Field $final.Data 'status') -eq 'stopped') { $stopped = $true; Write-Output 'Final status: stopped' }
        else { $failures.Add("final status is not stopped ($(if ($final.Ok) { Get-Field $final.Data 'status' } else { "HTTP $($final.Status)" }))") }
    }
}

if ($ready -and $answered -and $stopped -and -not $failures.Count) {
    Write-Output 'API FLOW PASS : start -> ready -> chat answered -> stop -> stopped. (Not proof of remote compute; collect helper evidence around the CHAT SENT AT time.)'
    exit 0
}
Write-Output "API FLOW FAIL : $($failures -join '; ')"
exit 1
