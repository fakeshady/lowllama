[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8000
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeRoot = Join-Path $projectRoot '.runtime'
$pidRoot = Join-Path $runtimeRoot 'pids'
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$server = Join-Path $runtimeRoot 'llama-b12110-cuda\llama-server.exe'
$model = Join-Path $runtimeRoot 'models\qwen2.5-0.5b-instruct-q4_k_m.gguf'
$modelHash = '74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db'
$tailscale = 'C:\Program Files\Tailscale\tailscale.exe'

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

foreach ($required in @($python, $server, $model, $tailscale)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Required master file is missing: $required"
    }
}
if ([string]::IsNullOrWhiteSpace($env:LOWLLAMA_ADMIN_TOKEN)) {
    throw 'Set LOWLLAMA_ADMIN_TOKEN privately in this terminal before starting the master.'
}

$masterIp = (& $tailscale ip -4 | Select-Object -First 1).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($masterIp)) {
    throw 'Unable to determine the current Tailscale IPv4 address.'
}
$parsedIp = $null
if (-not [Net.IPAddress]::TryParse($masterIp, [ref]$parsedIp) -or -not $masterIp.StartsWith('100.')) {
    throw 'The current Tailscale IPv4 address is invalid.'
}
$env:LOWLLAMA_MASTER_IP = $masterIp

& nvidia-smi.exe *> $null
if ($LASTEXITCODE -ne 0) { throw 'nvidia-smi failed; verify the NVIDIA driver.' }
$version = Invoke-NativeCapture $server @('--version') 'llama-server version check'
if ($version -notmatch 'build\s+12110\b' -or $version -notmatch 'commit\s+cd959bae9\b') {
    throw 'The master llama-server is not pinned build 12110.'
}
$devices = Invoke-NativeCapture $server @('--list-devices') 'llama-server device check'
if ($devices -notmatch '(?m)^\s*CUDA0:') {
    throw 'CUDA0 is unavailable on this master.'
}
if ((Get-FileHash -LiteralPath $model -Algorithm SHA256).Hash -ne $modelHash) {
    throw 'The master GGUF checksum does not match the pinned model.'
}

$listener = [Net.Sockets.TcpListener]::new($parsedIp, $Port)
try { $listener.Start() } catch { throw "TCP $masterIp`:$Port is already in use." } finally { $listener.Stop() }

New-Item -ItemType Directory -Path $pidRoot -Force | Out-Null
$recordPath = Join-Path $pidRoot 'master-coordinator.json'
$process = Start-Process -FilePath $python -ArgumentList @(
    '-m', 'uvicorn', 'coordinator.main:app', '--host', $masterIp, '--port', "$Port"
) -WorkingDirectory $projectRoot -NoNewWindow -PassThru
@{
    role = 'Master'
    component = 'Coordinator'
    pid = $process.Id
    executable = (Resolve-Path -LiteralPath $python).Path
    started_utc = [DateTime]::UtcNow.ToString('o')
} | ConvertTo-Json | Set-Content -LiteralPath $recordPath -Encoding UTF8

Write-Host "LowLlama master started on http://$masterIp`:$Port (PID $($process.Id))."
Write-Host 'The admin token remains in this terminal environment and is not written to disk.'
try {
    $process.WaitForExit()
    exit $process.ExitCode
} finally {
    if (Test-Path -LiteralPath $recordPath) { Remove-Item -LiteralPath $recordPath -Force }
}
