<#
.SYNOPSIS
    Read-only master-side preflight for LowLlama RPC helpers. Prints PASS/FAIL per check.

.DESCRIPTION
    Run on the MASTER from the project root. For each helper Tailscale IP, in the order you give them
    (that order becomes RPC0, RPC1, ...):
      1. the pinned b12110 llama-server binary exists and reports build 12110
      2. the address is a Tailscale address (100.64.0.0/10 or fd7a:115c:a1e0::/48), not this laptop, not repeated
      3. tailscale ping reaches it
      4. TCP port 50052 accepts a connection
      5. llama-server --rpc <all endpoints> --list-devices shows RPC0, RPC1, ... with the matching endpoints
      6. the master's CUDA0 is present (the launcher hardcodes CUDA0; without it /start cannot work)

    It never starts or stops the coordinator, agents, RPC servers or llama-server (only the short
    --version and --list-devices probes), never opens firewall ports, and never prints tokens or invite codes.
    TCP/RPC success is NOT proof of inference; see docs/PHASE2_EVIDENCE_CHECKLIST.md.

.EXAMPLE
    .\scripts\Check-RPC-Workers.ps1 -HelperIp 100.101.102.103
.EXAMPLE
    .\scripts\Check-RPC-Workers.ps1 -HelperIp 100.101.102.103,100.101.102.104 -ExpectHelpers 2 -Redact

    Exit code 0 and "PREFLIGHT PASS" only when every row passed. -SkipTailscalePing marks that row SKIPPED and
    says so in the final line; TCP and --list-devices still decide the result. -ExpectHelpers 2 makes the run
    fail unless exactly two helper addresses were given (gate G4 needs RPC0 AND RPC1).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string[]]$HelperIp,                                         # in RPC order: first = RPC0, second = RPC1
    [ValidateRange(1, 65535)]
    [int]$Port = 50052,
    [string]$ServerPath = '.runtime\llama-b12110-cuda\llama-server.exe',   # pinned master build
    [ValidateRange(1, 120)]
    [int]$ProbeTimeoutSeconds = 20,
    [switch]$SkipTailscalePing,
    [ValidateRange(0, 16)]
    [int]$ExpectHelpers = 0,                                     # 0 = any number; 2 = must be exactly two helpers
    [switch]$Redact                                              # mask middle octets in printed output
)

$ErrorActionPreference = 'Stop'
$HelperIp = @($HelperIp | ForEach-Object { $_ -split '[,\s]+' } | Where-Object { $_ })   # also accept "a,b" passed as one string
$projectRoot = Split-Path -Parent $PSScriptRoot
$tailscale = 'C:\Program Files\Tailscale\tailscale.exe'
$results = New-Object System.Collections.Generic.List[object]

function Show-Address([string]$ip) {
    if (-not $Redact) { return $ip }
    if ($ip -match '^(\d+)\.\d+\.\d+\.(\d+)$') { return "$($Matches[1]).*.*.$($Matches[2])" }
    if ($ip -match ':') { return ($ip -replace '^([0-9a-fA-F]+:[0-9a-fA-F]+:).*(:[0-9a-fA-F]+)$', '$1*$2') }
    return $ip
}

function Add-Result([string]$check, [string]$target, [string]$result, [string]$detail) {
    $results.Add([pscustomobject]@{ Check = $check; Target = $target; Result = $result; Detail = $detail })
}

function Test-TailscaleAddress([System.Net.IPAddress]$address) {
    $bytes = $address.GetAddressBytes()
    if ($address.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork) {
        return ($bytes[0] -eq 100 -and $bytes[1] -ge 64 -and $bytes[1] -le 127)   # 100.64.0.0/10
    }
    return ($bytes[0] -eq 0xfd -and $bytes[1] -eq 0x7a -and $bytes[2] -eq 0x11 -and $bytes[3] -eq 0x5c -and $bytes[4] -eq 0xa1 -and $bytes[5] -eq 0xe0)   # fd7a:115c:a1e0::/48
}

function Invoke-Bounded([string]$file, [string[]]$arguments, [int]$seconds) {   # run a probe with a hard timeout
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $file
    $info.Arguments = ($arguments | ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } }) -join ' '
    $info.UseShellExecute = $false
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.CreateNoWindow = $true
    $process = [System.Diagnostics.Process]::Start($info)
    $stdout = $process.StandardOutput.ReadToEndAsync()
    $stderr = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit($seconds * 1000)) {
        try { $process.Kill() } catch { }
        return [pscustomobject]@{ TimedOut = $true; ExitCode = $null; Output = '' }
    }
    $process.WaitForExit()
    return [pscustomobject]@{ TimedOut = $false; ExitCode = $process.ExitCode; Output = ($stdout.Result + "`n" + $stderr.Result) }
}

# 1. pinned binary
$server = Join-Path $projectRoot $ServerPath
$serverOk = $false
if (-not (Test-Path -LiteralPath $server -PathType Leaf)) {
    Add-Result 'Pinned llama-server' $ServerPath 'FAIL' 'binary not found; install the pinned b12110 build (scripts/Install-Runtime.ps1)'
} else {
    $version = Invoke-Bounded $server @('--version') 15
    if ($version.TimedOut) {
        Add-Result 'Pinned llama-server' $ServerPath 'FAIL' '--version timed out'
    } elseif ($version.Output -match '\bbuild 12110\b') {                 # b12110 prints "version: 0.9.0-dev (build 12110, commit ...)"
        Add-Result 'Pinned llama-server' $ServerPath 'PASS' 'build 12110'
        $serverOk = $true
    } else {
        Add-Result 'Pinned llama-server' $ServerPath 'FAIL' 'binary is not build 12110; do not mix llama.cpp releases'
    }
}

# 2. addresses
if ($ExpectHelpers -gt 0 -and $HelperIp.Count -ne $ExpectHelpers) {
    Add-Result 'Helper count' "$($HelperIp.Count) given" 'FAIL' "expected exactly $ExpectHelpers helper address(es)"
}
$ownIps = @()
if (Test-Path -LiteralPath $tailscale) {
    try { $ownIps = @(& $tailscale ip 2>$null | ForEach-Object { $_.Trim() } | Where-Object { $_ }) } catch { $ownIps = @() }
} else {
    Add-Result 'Tailscale CLI' $tailscale 'FAIL' 'not installed at the default path'
}
$helpers = New-Object System.Collections.Generic.List[object]
$seen = @{}
for ($i = 0; $i -lt $HelperIp.Count; $i++) {
    $raw = $HelperIp[$i].Trim()
    $label = "RPC$i " + (Show-Address $raw)
    $parsed = $null
    if (-not [System.Net.IPAddress]::TryParse($raw, [ref]$parsed)) {
        Add-Result 'Tailscale address' $label 'FAIL' 'not an IP address'
        continue
    }
    $ip = $parsed.ToString()
    if (-not (Test-TailscaleAddress $parsed)) {
        Add-Result 'Tailscale address' $label 'FAIL' 'not a Tailscale address; never expose RPC on a LAN or the internet'
        continue
    }
    if ($ownIps -contains $ip) {
        Add-Result 'Tailscale address' $label 'FAIL' 'this is the master itself, not a separate helper'
        continue
    }
    if ($seen.ContainsKey($ip)) {
        Add-Result 'Tailscale address' $label 'FAIL' 'same helper listed twice'
        continue
    }
    $seen[$ip] = $true
    Add-Result 'Tailscale address' $label 'PASS' 'in the tailnet range'
    $endpoint = if ($ip -match ':') { "[$ip]:$Port" } else { "${ip}:$Port" }
    $helpers.Add([pscustomobject]@{ Index = $helpers.Count; Ip = $ip; Endpoint = $endpoint; Label = $label; Reachable = $false })   # RPC number = position among accepted helpers
}

# 3 + 4. reachability
foreach ($helper in $helpers) {
    if ($SkipTailscalePing) {
        Add-Result 'tailscale ping' $helper.Label 'SKIPPED' 'skipped by -SkipTailscalePing (diagnostic only; TCP and list-devices still decide)'
    } elseif (Test-Path -LiteralPath $tailscale) {
        & $tailscale ping --c 1 --timeout 5s $helper.Ip *> $null
        if ($LASTEXITCODE -eq 0) { Add-Result 'tailscale ping' $helper.Label 'PASS' 'peer answered' }
        else { Add-Result 'tailscale ping' $helper.Label 'FAIL' 'no answer; is the helper on THIS tailnet and awake?' }
    }
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $connect = $client.ConnectAsync($helper.Ip, $Port)
        if ($connect.Wait(3000) -and $client.Connected) {
            $helper.Reachable = $true
            Add-Result "TCP $Port" $helper.Label 'PASS' 'port accepts connections (not yet proof of llama.cpp RPC)'
        } else {
            Add-Result "TCP $Port" $helper.Label 'FAIL' 'no listener reachable: check ggml-rpc-server, the helper firewall rule and the tailnet'
        }
    } catch {
        Add-Result "TCP $Port" $helper.Label 'FAIL' 'connection refused or unreachable'
    } finally {
        $client.Close()
    }
}

# 5 + 6. llama.cpp device discovery, all endpoints together so RPC numbering matches the real launch
$reachable = @($helpers | Where-Object { $_.Reachable })
if (-not $serverOk -or $reachable.Count -eq 0 -or $reachable.Count -ne $helpers.Count -or $helpers.Count -ne $HelperIp.Count) {
    Add-Result 'list-devices' 'all helpers' 'NOT RUN' 'needs the pinned binary, valid addresses and every helper reachable on TCP first'
} else {
    $endpoints = ($helpers | ForEach-Object { $_.Endpoint }) -join ','
    $probe = Invoke-Bounded $server @('--rpc', $endpoints, '--list-devices') $ProbeTimeoutSeconds
    if ($probe.TimedOut) {
        Add-Result 'list-devices' 'all helpers' 'FAIL' "no answer within $ProbeTimeoutSeconds s (version mismatch or a stuck helper)"
    } else {
        $lines = $probe.Output -split "`r?`n"
        foreach ($helper in $helpers) {
            $name = "RPC$($helper.Index)"
            $line = $lines | Where-Object { $_ -match "^\s*$name\s*:" } | Select-Object -First 1
            if (-not $line) {
                Add-Result 'list-devices' $helper.Label 'FAIL' "$name missing: not llama.cpp RPC or a different llama.cpp build"
            } elseif ($line -notmatch ('(^|[\s\[])' + [regex]::Escape($helper.Endpoint) + '(\s|\(|$)')) {   # whole endpoint only: 100.64.0.2 must not match 100.64.0.22
                Add-Result 'list-devices' $helper.Label 'FAIL' "$name does not point at this helper"
            } else {
                $memory = if ($line -match '\(([^)]*)\)') { $Matches[1] } else { 'memory not shown' }
                Add-Result 'list-devices' $helper.Label 'PASS' "$name found ($memory, reported by the helper)"
            }
        }
        if ($probe.ExitCode -ne 0) { Add-Result 'list-devices' 'exit code' 'FAIL' "llama-server exited with $($probe.ExitCode)" }
        $cuda = $lines | Where-Object { $_ -match '^\s*CUDA0\s*:' } | Select-Object -First 1
        if ($cuda) { Add-Result 'Master CUDA0' 'this laptop' 'PASS' ($cuda.Trim() -replace '^CUDA0:\s*', '') }
        else { Add-Result 'Master CUDA0' 'this laptop' 'FAIL' 'no CUDA0: the launcher command (CUDA0,RPC0,...) cannot start here' }
    }
}

$results | Format-Table -AutoSize -Wrap | Out-String -Width 200 | Write-Output
$logDir = Join-Path $projectRoot '.runtime\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$saved = Join-Path $logDir ("rpc-preflight-$(Get-Date -Format 'yyyyMMdd-HHmmss').txt")
$results | Format-Table -AutoSize -Wrap | Out-String -Width 200 | Set-Content -LiteralPath $saved -Encoding UTF8
$failed = @($results | Where-Object { $_.Result -eq 'FAIL' }).Count
$notRun = @($results | Where-Object { $_.Result -eq 'NOT RUN' }).Count
$skipped = @($results | Where-Object { $_.Result -eq 'SKIPPED' }).Count
$skipNote = if ($skipped) { " Tailscale ping SKIPPED by the operator for $skipped helper(s)." } else { '' }
if ($failed -eq 0 -and $notRun -eq 0) { Write-Output "PREFLIGHT PASS (saved to $saved).$skipNote This is NOT proof of inference." ; exit 0 }
Write-Output "PREFLIGHT FAIL: $failed failed, $notRun not run (saved to $saved)."
exit 1
