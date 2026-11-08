[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Rpc', 'Agent')]
    [string]$Component,
    [ValidateRange(1, 65535)]
    [int]$RpcPort = 50052,
    [ValidateRange(1, 256)]
    [int]$Threads = 4
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeRoot = Join-Path $projectRoot '.runtime'
$pidRoot = Join-Path $runtimeRoot 'pids'
$tailscale = 'C:\Program Files\Tailscale\tailscale.exe'
$rpc = Join-Path $runtimeRoot 'llama-b12110-cpu\ggml-rpc-server.exe'
$server = Join-Path $runtimeRoot 'llama-b12110-cpu\llama-server.exe'
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'

function Invoke-NativeCapture {
    param([string]$Executable, [string[]]$Arguments, [string]$Label)
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $output = & $Executable @Arguments 2>&1 | Out-String
        $exitCode = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previousPreference }
    if ($exitCode -ne 0) { throw "$Label failed (exit $exitCode)." }
    return $output
}

foreach ($required in @($tailscale, $rpc, $server, $python)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Required helper file is missing: $required"
    }
}
$helperIp = (& $tailscale ip -4 | Select-Object -First 1).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($helperIp) -or -not $helperIp.StartsWith('100.')) {
    throw 'Unable to determine a current Tailscale IPv4 address for this helper.'
}
$version = Invoke-NativeCapture $server @('--version') 'llama-server version check'
if ($version -notmatch 'build\s+12110\b' -or $version -notmatch 'commit\s+cd959bae9\b') {
    throw 'The helper llama.cpp runtime is not pinned build 12110.'
}

New-Item -ItemType Directory -Path $pidRoot -Force | Out-Null
if ($Component -eq 'Rpc') {
    $env:GGML_RPC_DEBUG = '1'
    $recordPath = Join-Path $pidRoot 'helper-rpc.json'
    $process = Start-Process -FilePath $rpc -ArgumentList @(
        '--device', 'CPU', '--host', $helperIp, '--port', "$RpcPort", '--threads', "$Threads"
    ) -WorkingDirectory $projectRoot -NoNewWindow -PassThru
    $expectedExecutable = (Resolve-Path -LiteralPath $rpc).Path
    Write-Host "Helper RPC started at $helperIp`:$RpcPort (PID $($process.Id))."
} else {
    if ([string]::IsNullOrWhiteSpace($env:LOWLLAMA_COORDINATOR_URL)) {
        throw 'Set LOWLLAMA_COORDINATOR_URL to Itmam master Tailscale URL before starting the agent.'
    }
    if ([string]::IsNullOrWhiteSpace($env:LOWLLAMA_NODE_NAME)) {
        throw 'Set LOWLLAMA_NODE_NAME before starting the agent.'
    }
    $env:LOWLLAMA_RPC_HOST = $helperIp
    $env:LOWLLAMA_RPC_PORT = "$RpcPort"
    $recordPath = Join-Path $pidRoot 'helper-agent.json'
    $process = Start-Process -FilePath $python -ArgumentList @('-m', 'agent.agent') `
        -WorkingDirectory $projectRoot -NoNewWindow -PassThru
    $expectedExecutable = (Resolve-Path -LiteralPath $python).Path
    Write-Host "Helper agent started for RPC $helperIp`:$RpcPort (PID $($process.Id))."
    Write-Host 'A fresh LOWLLAMA_INVITE_CODE is required only for enrollment or re-enrollment.'
}
@{
    role = 'Helper'
    component = $Component
    pid = $process.Id
    executable = $expectedExecutable
    started_utc = [DateTime]::UtcNow.ToString('o')
} | ConvertTo-Json | Set-Content -LiteralPath $recordPath -Encoding UTF8

try {
    $process.WaitForExit()
    exit $process.ExitCode
} finally {
    if (Test-Path -LiteralPath $recordPath) { Remove-Item -LiteralPath $recordPath -Force }
}
