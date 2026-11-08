[CmdletBinding()]
param(
    [ValidateSet('Master', 'Helper')]
    [string]$Role = 'Master'
)

$ErrorActionPreference = 'Stop'
$runtimeRoot = Join-Path (Split-Path -Parent $PSScriptRoot) '.runtime'
$downloadRoot = Join-Path $runtimeRoot 'downloads'
$release = 'b12110'
$commit = 'cd959bae99ff4bed22bef2604f45241c09bbb8c8'
$releaseUrl = "https://github.com/ggml-org/llama.cpp/releases/download/$release"
New-Item -ItemType Directory -Path $downloadRoot -Force | Out-Null

function Invoke-CheckedNativeCapture {
    param([string]$Executable, [string[]]$Arguments, [string]$Label)

    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $output = & $Executable @Arguments 2>&1 | Out-String
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($exitCode -ne 0) { throw "$Label failed (exit $exitCode)." }
    return $output
}

function Get-VerifiedDownload {
    param(
        [string]$Url,
        [string]$Destination,
        [string]$Sha256,
        [Nullable[long]]$GitHubAssetId
    )

    if (Test-Path -LiteralPath $Destination) {
        if ((Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash -ne $Sha256) {
            throw "Checksum mismatch for existing file: $Destination. Move it aside before retrying."
        }
        Write-Host "Reusing verified download: $Destination"
        return $Destination
    }

    New-Item -ItemType Directory -Path (Split-Path -Parent $Destination) -Force | Out-Null
    $partial = "$Destination.partial"
    & curl.exe --fail --location --retry 2 --retry-all-errors --connect-timeout 15 `
        --max-time 600 --output $partial $Url
    $downloadExit = $LASTEXITCODE
    if ($downloadExit -ne 0 -and $null -ne $GitHubAssetId) {
        Write-Warning "Direct release download failed (curl exit $downloadExit); trying the official GitHub asset API."
        $assetUrl = "https://api.github.com/repos/ggml-org/llama.cpp/releases/assets/$GitHubAssetId"
        & curl.exe --fail --location --retry 2 --retry-all-errors --connect-timeout 15 `
            --max-time 600 --header 'Accept: application/octet-stream' `
            --header 'X-GitHub-Api-Version: 2022-11-28' --output $partial $assetUrl
        $downloadExit = $LASTEXITCODE
    }
    if ($downloadExit -ne 0) {
        throw "Verified download failed (curl exit $downloadExit). Partial file retained at $partial."
    }
    if ((Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash -ne $Sha256) {
        throw "Checksum mismatch: $partial. The download will not be installed."
    }
    Move-Item -LiteralPath $partial -Destination $Destination
    return $Destination
}

function Expand-VerifiedArchive {
    param(
        [string]$Archive,
        [string]$Destination,
        [string]$Sha256,
        [string[]]$RequiredFiles = @()
    )

    $marker = Join-Path $Destination ((Split-Path -Leaf $Archive) + '.sha256')
    if (Test-Path -LiteralPath $marker) {
        if ((Get-Content -LiteralPath $marker -Raw).Trim() -ne $Sha256) {
            throw "Unexpected extraction marker: $marker"
        }
        foreach ($required in $RequiredFiles) {
            if (-not (Test-Path -LiteralPath (Join-Path $Destination $required) -PathType Leaf)) {
                throw "Extracted runtime is incomplete; missing $required in $Destination"
            }
        }
        Write-Host "Reusing extracted runtime: $Destination"
        return
    }

    # Merge archives without overwriting existing files or deleting directories.
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $destinationPrefix = [IO.Path]::GetFullPath($Destination).TrimEnd('\') + '\'
    $zip = [IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        foreach ($entry in $zip.Entries) {
            $target = [IO.Path]::GetFullPath((Join-Path $Destination $entry.FullName))
            if (-not $target.StartsWith($destinationPrefix, [StringComparison]::OrdinalIgnoreCase)) {
                throw "Archive entry escapes the runtime directory: $($entry.FullName)"
            }
            if ([string]::IsNullOrEmpty($entry.Name)) {
                New-Item -ItemType Directory -Path $target -Force | Out-Null
                continue
            }
            New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
            if (Test-Path -LiteralPath $target) {
                $stream = $entry.Open()
                $hasher = [Security.Cryptography.SHA256]::Create()
                try {
                    $entryHash = [BitConverter]::ToString($hasher.ComputeHash($stream)).Replace('-', '')
                } finally {
                    $stream.Dispose()
                    $hasher.Dispose()
                }
                if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -ne $entryHash) {
                    throw "Existing runtime file differs from the archive: $target. Move it aside before retrying."
                }
            } else {
                [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $false)
            }
        }
    } finally {
        $zip.Dispose()
    }
    foreach ($required in $RequiredFiles) {
        if (-not (Test-Path -LiteralPath (Join-Path $Destination $required) -PathType Leaf)) {
            throw "Archive extraction is incomplete; missing $required in $Destination"
        }
    }
    Set-Content -LiteralPath $marker -Value $Sha256 -Encoding ASCII
}

$cpuName = "llama-$release-bin-win-cpu-x64.zip"
$cpuHash = '98901e327c252875accaf2d1bf364e10e868843b0b63468baad1aa945cdc5da2'
$cpuArchive = Get-VerifiedDownload "$releaseUrl/$cpuName" (Join-Path $downloadRoot $cpuName) $cpuHash 709507579
$cpuRoot = Join-Path $runtimeRoot "llama-$release-cpu"
Expand-VerifiedArchive $cpuArchive $cpuRoot $cpuHash @('llama-server.exe', 'ggml-rpc-server.exe')
$cpuVersion = Invoke-CheckedNativeCapture (Join-Path $cpuRoot 'llama-server.exe') @('--version') 'CPU runtime verification'
if ($cpuVersion -notmatch 'build\s+12110\b' -or $cpuVersion -notmatch 'commit\s+cd959bae9\b') {
    throw 'Pinned CPU llama-server verification failed; expected build 12110.'
}

if ($Role -eq 'Master') {
    $cudaRoot = Join-Path $runtimeRoot "llama-$release-cuda"
    $cudaName = "llama-$release-bin-win-cuda-12.4-x64.zip"
    $cudaHash = '1ce755bc3a061889234048f88e33328e17971eaad73c4666cfeb26026b317fee'
    $cudaArchive = Get-VerifiedDownload "$releaseUrl/$cudaName" (Join-Path $downloadRoot $cudaName) $cudaHash 709507719
    Expand-VerifiedArchive $cudaArchive $cudaRoot $cudaHash @('llama-server.exe')

    $cudartName = 'cudart-llama-bin-win-cuda-12.4-x64.zip'
    $cudartHash = '8c79a9b226de4b3cacfd1f83d24f962d0773be79f1e7b75c6af4ded7e32ae1d6'
    $cudartArchive = Get-VerifiedDownload "$releaseUrl/$cudartName" (Join-Path $downloadRoot $cudartName) $cudartHash 709502077
    Expand-VerifiedArchive $cudartArchive $cudaRoot $cudartHash @('cudart64_12.dll', 'cublas64_12.dll', 'cublasLt64_12.dll')

    $modelName = 'qwen2.5-0.5b-instruct-q4_k_m.gguf'
    $modelRevision = '9217f5db79a29953eb74d5343926648285ec7e67'
    $modelUrl = "https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/$modelRevision/$modelName"
    $modelHash = '74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db'
    $null = Get-VerifiedDownload $modelUrl (Join-Path $runtimeRoot "models/$modelName") $modelHash

    $cudaVersion = Invoke-CheckedNativeCapture (Join-Path $cudaRoot 'llama-server.exe') @('--version') 'CUDA runtime verification'
    if ($cudaVersion -notmatch 'build\s+12110\b' -or $cudaVersion -notmatch 'commit\s+cd959bae9\b') {
        throw 'Pinned CUDA llama-server verification failed; expected build 12110.'
    }
    $cudaDevices = Invoke-CheckedNativeCapture (Join-Path $cudaRoot 'llama-server.exe') @('--list-devices') 'CUDA device verification'
    if ($cudaDevices -notmatch '(?m)^\s*CUDA0:') {
        throw 'CUDA runtime verification failed; CUDA0 is unavailable.'
    }
}

Write-Host "$Role runtime prepared: llama.cpp $release ($commit)"
Write-Host "Runtime directory: $runtimeRoot"
