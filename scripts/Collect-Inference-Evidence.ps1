<#
.SYNOPSIS
    Local-only evidence for LowLlama distributed inference. Reads logs on THIS laptop; never touches other laptops.

.DESCRIPTION
    -Mode Master          (run on the master after llama-server loaded the model)
        Parses the master's llama-server log (-lv 5 output) and reports, per device, the assigned layers and the
        MODEL-WEIGHT buffer size. An RPC device passes only with >0 layers AND a nonzero model buffer.
        KV-cache or compute buffers alone do not count.

    -Mode HelperSnapshot  (run on each helper, with -Label before, then -Label after)
        Counts [graph_compute]/[graph_recompute] and alloc_buffer lines in this helper's ggml-rpc-server log
        (the RPC server must run with GGML_RPC_DEBUG=1). Saves the counts to .runtime\logs\evidence\.

    -Mode HelperCompare   (run on each helper after the "after" snapshot)
        PASS only if the graph-call count increased between "before" and "after", i.e. during the new request.

    Each teammate sends the team only the printed summary lines (counts, PASS/FAIL), never raw logs with
    addresses, prompts, tokens or invite codes. There is no remote collection in this script, by design.

.EXAMPLE
    .\scripts\Collect-Inference-Evidence.ps1 -Mode Master -ExpectRpc 2
.EXAMPLE
    .\scripts\Collect-Inference-Evidence.ps1 -Mode HelperSnapshot -Label before
    # ...master sends ONE new chat request...
    .\scripts\Collect-Inference-Evidence.ps1 -Mode HelperSnapshot -Label after
    .\scripts\Collect-Inference-Evidence.ps1 -Mode HelperCompare
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Master', 'HelperSnapshot', 'HelperCompare')]
    [string]$Mode,
    [string]$MasterLog = '.runtime\logs\master-remote.log',
    [string]$HelperLog = '.runtime\logs\helper-rpc.log',
    [ValidateSet('before', 'after')]
    [string]$Label = 'before',
    [ValidateRange(0, 16)]
    [int]$ExpectRpc = 1,                                        # how many RPC helpers must hold weights (1 or 2 for the demo)
    [switch]$Redact
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$evidenceDir = Join-Path $projectRoot '.runtime\logs\evidence'

function Resolve-Log([string]$path) {
    $full = if ([System.IO.Path]::IsPathRooted($path)) { $path } else { Join-Path $projectRoot $path }
    if (-not (Test-Path -LiteralPath $full -PathType Leaf)) { throw "Log not found: $full" }
    return $full
}

function Show-Endpoint([string]$endpoint) {
    if (-not $Redact -or -not $endpoint) { return $endpoint }
    return ($endpoint -replace '^(\d+)\.\d+\.\d+\.(\d+)', '$1.*.*.$2')
}

function Get-BaseDevice([string]$token) {
    if ($token -match '^(RPC\d+|CUDA\d+|Vulkan\d+|CPU)') { return $Matches[1] }
    return $token
}

if ($Mode -eq 'Master') {
    $log = Resolve-Log $MasterLog
    $layers = @{}                                               # layer index -> device (last load wins)
    $modelMiB = @{}                                             # device -> model-weight MiB
    $endpoints = @{}                                            # RPCn -> host:port
    foreach ($line in (Get-Content -LiteralPath $log)) {        # Get-Content detects the UTF-16 BOM that Tee-Object writes in PowerShell 5.1
        if ($line -match 'llama_model_loader: loaded meta data') { $layers = @{}; $modelMiB = @{} }   # a new model load starts
        if ($line -match 'layer\s+(\d+)\s+assigned to device\s+([^\s,]+)') {
            $layers[[int]$Matches[1]] = Get-BaseDevice $Matches[2]
        }
        if ($line -match ':\s+(\S+)\s+model buffer size\s*=\s*([\d.]+)\s*MiB') {
            $token = $Matches[1]; $mib = [double]$Matches[2]
            $device = Get-BaseDevice $token
            if ($token -match '^RPC\[([^\]]+)\]') {                  # ggml-rpc may name the buffer RPC[host:port] without an index
                $bufferEndpoint = $Matches[1]
                $owner = @($endpoints.Keys | Where-Object { $endpoints[$_] -eq $bufferEndpoint } | Select-Object -First 1)
                if ($owner.Count) { $device = $owner[0] } else { $device = "RPC?[$bufferEndpoint]" }   # unmapped: shown as FAIL, never guessed
            }
            $modelMiB[$device] = $mib
            if ($device -like 'RPC*' -and $token -match '\[([^\]]+)\]') { $endpoints[$device] = $Matches[1] }
        }
        if ($line -match 'using device (RPC\d+)\s*\(([^)]+)\)') { $endpoints[$Matches[1]] = $Matches[2].Trim() }
    }
    $devices = @($layers.Values) + @($modelMiB.Keys) + @($endpoints.Keys) | Sort-Object -Unique   # an RPC device with no weights still shows up, as FAIL
    $rows = foreach ($device in $devices) {
        $mine = @($layers.Keys | Where-Object { $layers[$_] -eq $device } | Sort-Object)
        $range = if ($mine.Count) { "$($mine[0])-$($mine[-1])" } else { '-' }
        $mib = if ($modelMiB.ContainsKey($device)) { $modelMiB[$device] } else { 0 }
        $endpoint = if ($endpoints.ContainsKey($device)) { $endpoints[$device] } else { '' }
        $result = 'INFO'
        if ($device -like 'RPC*') {
            if ($mine.Count -gt 0 -and $mib -gt 0) { $result = 'PASS' } else { $result = 'FAIL' }
            if ($endpoint -match '^(127\.|\[::1\]|localhost)') { $result = 'LOOPBACK' }   # same laptop: diagnostic only, not a physical helper
        }
        [pscustomobject]@{ Device = $device; Endpoint = (Show-Endpoint $endpoint); Layers = $mine.Count; LayerRange = $range; ModelMiB = $mib; Result = $result }
    }
    if (-not $rows) { Write-Output 'No layer assignments or model buffers found. Was llama-server run with -lv 5 and the output saved with Tee-Object?'; exit 1 }
    $rows | Format-Table -AutoSize | Out-String -Width 200 | Write-Output
    $rpcPass = @($rows | Where-Object { $_.Result -eq 'PASS' })
    $hosts = @($rpcPass | ForEach-Object { ($endpoints[$_.Device] -replace ':\d+$', '') } | Sort-Object -Unique)
    $verdict = 'FAIL'
    if ($rpcPass.Count -ge $ExpectRpc -and $hosts.Count -ge $ExpectRpc) { $verdict = 'PASS' }
    $summary = "MASTER ALLOCATION $verdict : $($rpcPass.Count) RPC device(s) hold model weights on $($hosts.Count) distinct host(s); expected $ExpectRpc. " +
               (($rows | ForEach-Object { "$($_.Device)=$($_.Layers) layers/$($_.ModelMiB) MiB" }) -join ', ')
    Write-Output $summary
    Write-Output 'Allocation alone is not participation: each helper must also show NEW graph_compute lines for a fresh request.'
    New-Item -ItemType Directory -Force -Path $evidenceDir | Out-Null
    $saved = Join-Path $evidenceDir ("master-allocation-$(Get-Date -Format 'yyyyMMdd-HHmmss').txt")
    (($rows | Format-Table -AutoSize | Out-String -Width 200) + $summary) | Set-Content -LiteralPath $saved -Encoding UTF8
    Write-Output "Saved to $saved"
    if ($verdict -eq 'PASS') { exit 0 } else { exit 1 }
}

if ($Mode -eq 'HelperSnapshot') {
    $log = Resolve-Log $HelperLog
    $graph = @(Select-String -LiteralPath $log -Pattern '\[graph_compute\]|\[graph_recompute\]').Count
    $alloc = @(Select-String -LiteralPath $log -Pattern 'alloc_buffer').Count
    $snapshot = [ordered]@{
        label = $Label
        taken_at = (Get-Date).ToString('o')
        computer = $env:COMPUTERNAME                            # stays in the ignored local evidence folder
        log_bytes = (Get-Item -LiteralPath $log).Length
        graph_calls = $graph
        alloc_buffer_lines = $alloc
    }
    New-Item -ItemType Directory -Force -Path $evidenceDir | Out-Null
    $snapshot | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $evidenceDir "helper-$Label.json") -Encoding UTF8
    Write-Output "HELPER SNAPSHOT $Label : graph_calls=$graph alloc_buffer_lines=$alloc"
    if ($graph -eq 0 -and $alloc -eq 0) { Write-Output 'No RPC debug lines at all. Start ggml-rpc-server with $env:GGML_RPC_DEBUG=''1'' and Tee-Object into this log.' }
    exit 0
}

if ($Mode -eq 'HelperCompare') {
    $beforeFile = Join-Path $evidenceDir 'helper-before.json'
    $afterFile = Join-Path $evidenceDir 'helper-after.json'
    if (-not (Test-Path $beforeFile) -or -not (Test-Path $afterFile)) { Write-Output 'HELPER COMPUTE NOT RUN : take both -Label before and -Label after snapshots first.'; exit 1 }
    $before = Get-Content -LiteralPath $beforeFile -Raw | ConvertFrom-Json
    $after = Get-Content -LiteralPath $afterFile -Raw | ConvertFrom-Json
    $delta = [int]$after.graph_calls - [int]$before.graph_calls
    $verdict = 'FAIL'
    $note = 'no new graph computation during the request'
    if ([int64]$after.log_bytes -lt [int64]$before.log_bytes) { $note = 'log shrank between snapshots (RPC server restarted?); redo both snapshots' }
    elseif ([datetime]$after.taken_at -le [datetime]$before.taken_at) { $note = 'the after snapshot is not newer than the before snapshot' }
    elseif ($delta -gt 0) { $verdict = 'PASS'; $note = 'this helper computed during the new request' }
    Write-Output "HELPER COMPUTE $verdict : graph_calls before=$($before.graph_calls) after=$($after.graph_calls) delta=$delta ($note)"
    if ($verdict -eq 'PASS') { exit 0 } else { exit 1 }
}
