<#
.SYNOPSIS
    Local-only evidence for LowLlama distributed inference. Reads logs on THIS laptop; never touches other laptops.

.DESCRIPTION
    -Mode Master          (run on the master after llama-server loaded the model)
        Parses the master's llama-server log and reports, per device, the assigned layers and the MODEL-WEIGHT
        buffer size. An RPC device passes only with >0 layers AND a nonzero model buffer, on a Tailscale host that
        is not this laptop and not loopback. KV-cache or compute buffers alone do not count.
        Which log:  -MasterLog <path>   a specific file (manual run: .runtime\logs\master-remote.log, the default)
                    -ApiLog             the newest .runtime\logs\api-launcher-*.log (a /start run); its name is printed
        A log last written more than -MaxLogAgeMinutes ago is refused as STALE unless -AllowOldLog is given.

    -Mode HelperSnapshot  (run on each helper: -Label before, after warmup and BEFORE the chat; -Label after, AFTER it)
        Counts [graph_compute]/[graph_recompute] lines in this helper's ggml-rpc-server log (the RPC server must run
        with GGML_RPC_DEBUG=1) and records the log size, a hash of its first 4 KB and the running ggml-rpc-server
        process, so a restarted server or rewritten log is detected. Snapshots are kept per -Session; taking a new
        "before" deletes an older "after" of the same session.

    -Mode HelperCompare   (run on each helper after the "after" snapshot)
        PASS only if the graph-call count increased inside one short window on the same log and the same server
        process. Restart, rewritten/shrunk log, out-of-order snapshots or a window longer than -MaxWindowMinutes
        give AMBIGUOUS (redo both snapshots around ONE new chat). Zero increase gives FAIL. Compared snapshots are
        renamed *.used.json so they can never be reused. Prints one line to send to the team, for example:
            SHAFIN HELPER COMPUTE PASS : session=run1 delta=36 window=2026-11-08T15:01:02.0000000Z/2026-11-08T15:01:40.0000000Z
        Exit codes: 0 PASS, 1 FAIL or NOT RUN, 2 AMBIGUOUS.

    -Mode SamePrompt      (run anywhere, usually the master, after both helpers sent their HelperCompare lines)
        -HelperResult takes the pasted HELPER COMPUTE lines; -ChatSentAt is the UTC time the ONE chat was sent
        (Test-Api-Flow.ps1 prints it; for a manual chat run (Get-Date).ToUniversalTime().ToString('o') just before
        sending). PASS only if at least -ExpectHelpers distinct helpers all report PASS for the same session and the
        chat time falls inside every helper's before/after window (allowing -MaxClockSkewSeconds of clock drift).
        This is a timing correlation from local logs, not a cryptographic attestation.

    Each teammate sends the team only the printed summary lines (counts, PASS/FAIL), never raw logs with
    addresses, prompts, tokens or invite codes. There is no remote collection in this script, by design.

.EXAMPLE
    .\scripts\Collect-Inference-Evidence.ps1 -Mode Master -ExpectRpc 2
.EXAMPLE
    .\scripts\Collect-Inference-Evidence.ps1 -Mode Master -ApiLog -ExpectRpc 2 -Redact
.EXAMPLE
    .\scripts\Collect-Inference-Evidence.ps1 -Mode HelperSnapshot -Label before -Session run1 -HelperName Shafin
    # ...master sends ONE new chat request...
    .\scripts\Collect-Inference-Evidence.ps1 -Mode HelperSnapshot -Label after -Session run1 -HelperName Shafin
    .\scripts\Collect-Inference-Evidence.ps1 -Mode HelperCompare -Session run1 -HelperName Shafin
.EXAMPLE
    .\scripts\Collect-Inference-Evidence.ps1 -Mode SamePrompt -ChatSentAt 2026-11-08T15:01:20Z -HelperResult 'RODEL HELPER COMPUTE PASS : ...','SHAFIN HELPER COMPUTE PASS : ...'
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Master', 'HelperSnapshot', 'HelperCompare', 'SamePrompt')]
    [string]$Mode,
    [string]$MasterLog = '',                                    # empty: master-remote.log, or the newest api-launcher log with -ApiLog
    [switch]$ApiLog,
    [ValidateRange(1, 1440)]
    [int]$MaxLogAgeMinutes = 120,
    [switch]$AllowOldLog,
    [string]$HelperLog = '.runtime\logs\helper-rpc.log',
    [ValidateSet('before', 'after')]
    [string]$Label = 'before',
    [string]$Session = 'default',
    [string]$HelperName = '',                                   # printed in the result line; defaults to LOWLLAMA_NODE_NAME or the computer name
    [ValidateRange(1, 120)]
    [int]$MaxWindowMinutes = 10,
    [ValidateRange(0, 16)]
    [int]$ExpectRpc = 1,                                        # how many RPC helpers must hold weights (1 or 2 for the demo)
    [string[]]$HelperResult = @(),
    [string]$ChatSentAt = '',
    [ValidateRange(1, 16)]
    [int]$ExpectHelpers = 2,
    [ValidateRange(0, 600)]
    [int]$MaxClockSkewSeconds = 5,
    [switch]$Redact
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$logRoot = Join-Path $projectRoot '.runtime\logs'
$evidenceDir = Join-Path $logRoot 'evidence'
$invariant = [Globalization.CultureInfo]::InvariantCulture

function Resolve-Log([string]$path) {
    $full = if ([System.IO.Path]::IsPathRooted($path)) { $path } else { Join-Path $projectRoot $path }
    if (-not (Test-Path -LiteralPath $full -PathType Leaf)) { throw "Log not found: $full" }
    return (Resolve-Path -LiteralPath $full).Path
}

function Show-Endpoint([string]$endpoint) {
    if (-not $Redact -or -not $endpoint) { return $endpoint }
    return ($endpoint -replace '^(\d+)\.\d+\.\d+\.(\d+)', '$1.*.*.$2')
}

function Get-BaseDevice([string]$token) {
    if ($token -match '^(RPC\d+|CUDA\d+|Vulkan\d+|CPU)') { return $Matches[1] }
    return $token
}

function Get-UtcNow { return (Get-Date).ToUniversalTime().ToString('o', $invariant) }

function ConvertTo-Time([string]$text) {
    return [DateTimeOffset]::Parse($text, $invariant, [Globalization.DateTimeStyles]::AssumeUniversal)
}

function Get-SafeName([string]$text, [string]$fallback) {
    $clean = ($text -replace '[^A-Za-z0-9_-]', '-').Trim('-')
    if (-not $clean) { return $fallback }
    return $clean
}

function Get-OwnTailscaleIps {
    $tailscale = 'C:\Program Files\Tailscale\tailscale.exe'
    if (-not (Test-Path -LiteralPath $tailscale)) { return @() }
    try { return @(& $tailscale ip 2>$null | ForEach-Object { $_.Trim() } | Where-Object { $_ }) } catch { return @() }
}

if ($Mode -eq 'Master') {
    if ($MasterLog) {
        $log = Resolve-Log $MasterLog
    } elseif ($ApiLog) {
        $candidates = @(Get-ChildItem -LiteralPath $logRoot -Filter 'api-launcher-*.log' -File -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending)
        if (-not $candidates.Count) { Write-Output 'MASTER ALLOCATION NOT RUN : no .runtime\logs\api-launcher-*.log yet. Start the model with POST /start first.'; exit 1 }
        $log = $candidates[0].FullName
        $recent = @($candidates | Where-Object { $_.LastWriteTime -gt (Get-Date).AddMinutes(-$MaxLogAgeMinutes) }).Count
        if ($recent -gt 1) { Write-Output "Note: $recent API launch logs were written in the last $MaxLogAgeMinutes minutes; using the newest. Pass -MasterLog to choose another." }
    } else {
        $log = Resolve-Log '.runtime\logs\master-remote.log'
    }
    $info = Get-Item -LiteralPath $log
    $ageMinutes = [math]::Round(((Get-Date) - $info.LastWriteTime).TotalMinutes, 1)
    Write-Output "Master log: $($info.Name) (last written $($info.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss')), $ageMinutes min ago)"
    if ($ageMinutes -gt $MaxLogAgeMinutes -and -not $AllowOldLog) {
        Write-Output "MASTER ALLOCATION FAIL : STALE log, last written $ageMinutes min ago (limit $MaxLogAgeMinutes). Use the log of THIS run, or -AllowOldLog to inspect an old one (never as new evidence)."
        exit 1
    }
    $ownIps = Get-OwnTailscaleIps
    $layers = @{}                                               # layer index -> device (last load wins)
    $modelMiB = @{}                                             # device -> model-weight MiB
    $endpoints = @{}                                            # RPCn -> host:port
    $loads = 0
    foreach ($line in (Get-Content -LiteralPath $log)) {        # Get-Content detects the UTF-16 BOM that Tee-Object writes in PowerShell 5.1
        if ($line -match 'llama_model_loader: loaded meta data') { $layers = @{}; $modelMiB = @{}; $loads++ }   # a new model load starts
        if ($line -match 'layer\s+(\d+)\s+assigned to device\s+([^\s,]+)') {
            $layers[[int]$Matches[1]] = Get-BaseDevice $Matches[2]
        }
        if ($line -match ':\s+(\S+)\s+model buffer size\s*=\s*([\d.]+)\s*MiB') {
            $token = $Matches[1]; $mib = [double]::Parse($Matches[2], $invariant)
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
    if ($loads -gt 1) { Write-Output "Note: this log contains $loads model loads; only the last one is used." }
    $devices = @($layers.Values) + @($modelMiB.Keys) + @($endpoints.Keys) | Sort-Object -Unique   # an RPC device with no weights still shows up, as FAIL
    $rows = foreach ($device in $devices) {
        $mine = @($layers.Keys | Where-Object { $layers[$_] -eq $device } | Sort-Object)
        $range = if ($mine.Count) { "$($mine[0])-$($mine[-1])" } else { '-' }
        $mib = if ($modelMiB.ContainsKey($device)) { $modelMiB[$device] } else { 0 }
        $endpoint = if ($endpoints.ContainsKey($device)) { $endpoints[$device] } else { '' }
        $hostPart = ($endpoint -replace ':\d+$', '').Trim('[', ']')
        $result = 'INFO'
        if ($device -like 'RPC*') {
            if ($mine.Count -gt 0 -and $mib -gt 0) { $result = 'PASS' } else { $result = 'FAIL' }
            if (-not $endpoint) { $result = 'FAIL' }                                          # weights on an RPC device we cannot place on a host
            elseif ($endpoint -match '^(127\.|\[::1\]|localhost)') { $result = 'LOOPBACK' }   # same laptop: diagnostic only, not a physical helper
            elseif ($ownIps -contains $hostPart) { $result = 'SELF' }                         # the master's own address is not a separate helper
        }
        [pscustomobject]@{ Device = $device; Endpoint = (Show-Endpoint $endpoint); Host = $hostPart; Layers = $mine.Count; LayerRange = $range; ModelMiB = $mib; Result = $result }
    }
    if (-not $rows) { Write-Output 'MASTER ALLOCATION FAIL : no layer assignments or model buffers found. Was llama-server run with -lv 5 and its output saved to this log?'; exit 1 }
    $rows | Select-Object Device, Endpoint, Layers, LayerRange, ModelMiB, Result | Format-Table -AutoSize | Out-String -Width 200 | Write-Output
    $rpcPass = @($rows | Where-Object { $_.Result -eq 'PASS' })
    $hosts = @($rpcPass | ForEach-Object { $_.Host } | Sort-Object -Unique)
    $verdict = 'FAIL'
    if ($rpcPass.Count -ge $ExpectRpc -and $hosts.Count -ge $ExpectRpc) { $verdict = 'PASS' }
    $summary = "MASTER ALLOCATION $verdict : $($rpcPass.Count) RPC device(s) hold model weights on $($hosts.Count) distinct host(s); expected $ExpectRpc. " +
               (($rows | ForEach-Object { "$($_.Device)=$($_.Layers) layers/$($_.ModelMiB) MiB" }) -join ', ')
    Write-Output $summary
    Write-Output 'Allocation alone is not participation: each helper must also show NEW graph_compute lines for a fresh request.'
    New-Item -ItemType Directory -Force -Path $evidenceDir | Out-Null
    $saved = Join-Path $evidenceDir ("master-allocation-$(Get-Date -Format 'yyyyMMdd-HHmmss').txt")
    ("Log: $($info.Name)`r`n" + ($rows | Select-Object Device, Endpoint, Layers, LayerRange, ModelMiB, Result | Format-Table -AutoSize | Out-String -Width 200) + $summary) |
        Set-Content -LiteralPath $saved -Encoding UTF8
    Write-Output "Saved to $saved"
    if ($verdict -eq 'PASS') { exit 0 } else { exit 1 }
}

$sessionName = Get-SafeName $Session 'default'
$nameSource = if ($HelperName) { $HelperName } elseif ($env:LOWLLAMA_NODE_NAME) { $env:LOWLLAMA_NODE_NAME } else { $env:COMPUTERNAME }
$displayName = (Get-SafeName $nameSource 'THIS').ToUpperInvariant()
$beforeFile = Join-Path $evidenceDir "helper-$sessionName-before.json"
$afterFile = Join-Path $evidenceDir "helper-$sessionName-after.json"

if ($Mode -eq 'HelperSnapshot') {
    $log = Resolve-Log $HelperLog
    $graph = @(Select-String -LiteralPath $log -Pattern '\[graph_compute\]|\[graph_recompute\]').Count
    $alloc = @(Select-String -LiteralPath $log -Pattern 'alloc_buffer').Count
    $headHash = ''
    $headBytes = 4096
    if ($Label -eq 'after' -and (Test-Path -LiteralPath $beforeFile)) {
        $previous = Get-Content -LiteralPath $beforeFile -Raw | ConvertFrom-Json
        if ($previous.log_head_bytes -ne $null) { $headBytes = [int]$previous.log_head_bytes }   # hash the same prefix the "before" hashed
    }
    $stream = [System.IO.File]::Open($log, 'Open', 'Read', 'ReadWrite')   # the RPC server keeps writing; share the file
    try {
        $buffer = New-Object byte[] ([math]::Max($headBytes, 1))
        $read = 0
        while ($read -lt $headBytes) {
            $n = $stream.Read($buffer, $read, $headBytes - $read)
            if ($n -le 0) { break }
            $read += $n
        }
        $sha = [System.Security.Cryptography.SHA256]::Create()
        $headHash = [BitConverter]::ToString($sha.ComputeHash($buffer, 0, $read)) -replace '-', ''
    } finally { $stream.Close() }
    $servers = @(Get-Process -Name 'ggml-rpc-server' -ErrorAction SilentlyContinue)
    $serverPid = $null; $serverStart = $null
    if ($servers.Count -eq 1) {
        $serverPid = $servers[0].Id
        try { $serverStart = $servers[0].StartTime.ToUniversalTime().ToString('o', $invariant) } catch { $serverStart = $null }
    }
    $snapshot = [ordered]@{
        label = $Label
        session = $sessionName
        helper = $displayName
        taken_at = Get-UtcNow
        computer = $env:COMPUTERNAME                            # stays in the ignored local evidence folder
        log_path = $log
        log_bytes = (Get-Item -LiteralPath $log).Length
        log_head_bytes = $read
        log_head_sha256 = $headHash
        rpc_server_count = $servers.Count
        rpc_server_pid = $serverPid
        rpc_server_started = $serverStart
        graph_calls = $graph
        alloc_buffer_lines = $alloc
    }
    New-Item -ItemType Directory -Force -Path $evidenceDir | Out-Null
    if ($Label -eq 'before' -and (Test-Path -LiteralPath $afterFile)) { Remove-Item -LiteralPath $afterFile }   # an old "after" must never pair with this "before"
    $target = if ($Label -eq 'before') { $beforeFile } else { $afterFile }
    $snapshot | ConvertTo-Json | Set-Content -LiteralPath $target -Encoding UTF8
    Write-Output "HELPER SNAPSHOT $Label : session=$sessionName graph_calls=$graph alloc_buffer_lines=$alloc rpc_servers_running=$($servers.Count)"
    if ($graph -eq 0 -and $alloc -eq 0) { Write-Output 'No RPC debug lines at all. Start ggml-rpc-server with $env:GGML_RPC_DEBUG=''1'' and save its output to this log.' }
    if ($servers.Count -ne 1) { Write-Output "Note: $($servers.Count) ggml-rpc-server processes are running; a restart between snapshots cannot be ruled out by process ID." }
    exit 0
}

if ($Mode -eq 'HelperCompare') {
    if (-not (Test-Path -LiteralPath $beforeFile) -or -not (Test-Path -LiteralPath $afterFile)) {
        Write-Output "$displayName HELPER COMPUTE NOT RUN : session=$sessionName needs both -Label before and -Label after snapshots (old ones are used up after a compare)."
        exit 1
    }
    $before = Get-Content -LiteralPath $beforeFile -Raw | ConvertFrom-Json
    $after = Get-Content -LiteralPath $afterFile -Raw | ConvertFrom-Json
    $delta = [int]$after.graph_calls - [int]$before.graph_calls
    $beforeAt = ConvertTo-Time ([string]$before.taken_at)
    $afterAt = ConvertTo-Time ([string]$after.taken_at)
    $window = ($afterAt - $beforeAt).TotalMinutes
    $problems = New-Object System.Collections.Generic.List[string]
    if ([string]$before.log_path -ne [string]$after.log_path) { $problems.Add('the snapshots read different log files') }
    if ([int64]$after.log_bytes -lt [int64]$before.log_bytes) { $problems.Add('the log shrank (RPC server restarted or log rewritten)') }
    elseif ([string]$before.log_head_sha256 -ne [string]$after.log_head_sha256) { $problems.Add('the start of the log changed (log rewritten)') }
    if ($before.rpc_server_pid -and $after.rpc_server_pid -and ([string]$before.rpc_server_pid -ne [string]$after.rpc_server_pid -or [string]$before.rpc_server_started -ne [string]$after.rpc_server_started)) {
        $problems.Add('ggml-rpc-server restarted between snapshots')
    }
    if ($afterAt -le $beforeAt) { $problems.Add('the after snapshot is not newer than the before snapshot') }
    elseif ($window -gt $MaxWindowMinutes) { $problems.Add("the window is $([math]::Round($window, 1)) min (limit $MaxWindowMinutes); other requests may be inside it") }
    if ($problems.Count) { $verdict = 'AMBIGUOUS'; $note = ($problems -join '; ') + '. Redo both snapshots around ONE new chat.' }
    elseif ($delta -gt 0) { $verdict = 'PASS'; $note = 'this helper computed during the window' }
    else { $verdict = 'FAIL'; $note = 'no new graph computation during the window' }
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    Move-Item -LiteralPath $beforeFile -Destination ($beforeFile -replace '\.json$', ".$stamp.used.json")   # used up: can never be compared again
    Move-Item -LiteralPath $afterFile -Destination ($afterFile -replace '\.json$', ".$stamp.used.json")
    $windowText = $beforeAt.UtcDateTime.ToString('o', $invariant) + '/' + $afterAt.UtcDateTime.ToString('o', $invariant)
    Write-Output "$displayName HELPER COMPUTE $verdict : session=$sessionName delta=$delta window=$windowText graph_calls_before=$($before.graph_calls) after=$($after.graph_calls) ($note)"
    if ($verdict -eq 'PASS') { exit 0 } elseif ($verdict -eq 'AMBIGUOUS') { exit 2 } else { exit 1 }
}

if ($Mode -eq 'SamePrompt') {
    if (-not $ChatSentAt) { Write-Output 'SAME PROMPT PROOF NOT RUN : -ChatSentAt is required (the UTC time the one chat was sent).'; exit 1 }
    try { $chatAt = ConvertTo-Time $ChatSentAt } catch { Write-Output "SAME PROMPT PROOF FAIL : -ChatSentAt '$ChatSentAt' is not a date/time."; exit 1 }
    $lines = @($HelperResult | ForEach-Object { $_ -split "`r?`n" } | Where-Object { $_ -match 'HELPER COMPUTE' })
    $parsed = New-Object System.Collections.Generic.List[object]
    $problems = New-Object System.Collections.Generic.List[string]
    $pattern = '^\s*(?<name>[A-Z0-9_-]+) HELPER COMPUTE (?<verdict>PASS|FAIL|AMBIGUOUS|NOT RUN) : session=(?<session>\S+)(?: delta=(?<delta>-?\d+) window=(?<from>[^/\s]+)/(?<to>\S+))?'
    foreach ($line in $lines) {
        if ($line -notmatch $pattern) { $problems.Add("unreadable result line: $($line.Trim())"); continue }
        $item = [pscustomobject]@{ Name = $Matches['name']; Verdict = $Matches['verdict']; Session = $Matches['session']; From = $Matches['from']; To = $Matches['to'] }
        $parsed.Add($item)
    }
    $names = @($parsed | ForEach-Object { $_.Name } | Sort-Object -Unique)
    $sessions = @($parsed | ForEach-Object { $_.Session } | Sort-Object -Unique)
    if ($names.Count -lt $ExpectHelpers) { $problems.Add("$($names.Count) distinct helper(s) reported; expected $ExpectHelpers") }
    if ($names.Count -ne $parsed.Count) { $problems.Add('the same helper name appears more than once') }
    if ($sessions.Count -gt 1) { $problems.Add("different sessions: $($sessions -join ', ')") }
    foreach ($item in $parsed) {
        if ($item.Verdict -ne 'PASS') { $problems.Add("$($item.Name) reported $($item.Verdict)"); continue }
        try { $from = ConvertTo-Time $item.From; $to = ConvertTo-Time $item.To } catch { $problems.Add("$($item.Name) window is unreadable"); continue }
        if ($chatAt -lt $from.AddSeconds(-$MaxClockSkewSeconds) -or $chatAt -gt $to.AddSeconds($MaxClockSkewSeconds)) {
            $problems.Add("$($item.Name) window $($item.From)/$($item.To) does not contain the chat time")
        }
    }
    $chatText = $chatAt.UtcDateTime.ToString('o', $invariant)
    if ($problems.Count) {
        Write-Output "SAME PROMPT PROOF FAIL : $($problems -join '; ')"
        exit 1
    }
    Write-Output "SAME PROMPT PROOF PASS : $($names -join ', ') each computed during one window containing the chat sent at $chatText (session=$($sessions[0]), clock skew allowance $MaxClockSkewSeconds s). Timing correlation from local logs, not an attestation."
    exit 0
}
