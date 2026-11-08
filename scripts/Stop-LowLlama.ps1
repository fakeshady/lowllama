[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Master', 'Helper')]
    [string]$Role,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pidRoot = Join-Path $projectRoot '.runtime\pids'
$records = if ($Role -eq 'Master') {
    @('master-coordinator.json')
} else {
    @('helper-agent.json', 'helper-rpc.json')
}

foreach ($name in $records) {
    $recordPath = Join-Path $pidRoot $name
    if (-not (Test-Path -LiteralPath $recordPath -PathType Leaf)) {
        Write-Host "No owned process record: $name"
        continue
    }
    try {
        $record = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
        $process = Get-Process -Id ([int]$record.pid) -ErrorAction Stop
        $actualPath = $process.Path
        $expectedPath = [IO.Path]::GetFullPath([string]$record.executable)
        if ([string]::IsNullOrWhiteSpace($actualPath) -or
            -not [IO.Path]::GetFullPath($actualPath).Equals($expectedPath, [StringComparison]::OrdinalIgnoreCase)) {
            throw "PID $($record.pid) does not match the recorded LowLlama executable; refusing to stop it."
        }
        $arguments = @('/PID', "$($record.pid)", '/T')
        if ($Force) { $arguments += '/F' }
        & taskkill.exe @arguments | Out-Host
        if ($LASTEXITCODE -ne 0) {
            $forceHint = if ($Force) { '' } else { ' Retry with -Force only if Ctrl+C in the owning terminal is unavailable.' }
            throw "taskkill failed for owned PID $($record.pid).$forceHint"
        }
        Write-Host "Stopped owned $($record.component) process PID $($record.pid)."
        Remove-Item -LiteralPath $recordPath -Force -ErrorAction SilentlyContinue
    } catch [ArgumentException] {
        throw
    } catch {
        if ($_.Exception.Message -match 'Cannot find a process') {
            Write-Warning "Recorded process in $name is no longer running; removing stale record."
            Remove-Item -LiteralPath $recordPath -Force
        } else {
            throw
        }
    }
}
