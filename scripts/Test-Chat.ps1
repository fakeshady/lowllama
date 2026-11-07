[CmdletBinding()]
param(
    [string]$Prompt = 'What is 2 + 2? Reply in one short sentence.',
    [ValidateRange(1, 65535)]
    [int]$Port = 8080
)

$ErrorActionPreference = 'Stop'
$baseUrl = "http://127.0.0.1:$Port"
$health = Invoke-RestMethod "$baseUrl/health" -TimeoutSec 10
if ($health.status -ne 'ok') { throw 'The model server is not ready.' }
$body = @{
    model = 'lowllama'
    messages = @(@{ role = 'user'; content = $Prompt })
    temperature = 0
    max_tokens = 64
    stream = $false
} | ConvertTo-Json -Depth 5
$result = Invoke-RestMethod "$baseUrl/v1/chat/completions" -Method Post `
    -ContentType 'application/json' -Body ([Text.Encoding]::UTF8.GetBytes($body)) -TimeoutSec 180
if ([string]::IsNullOrWhiteSpace($result.choices[0].message.content)) {
    throw 'The model returned no answer.'
}
$logRoot = Join-Path (Split-Path -Parent $PSScriptRoot) '.runtime/logs'
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
$output = Join-Path $logRoot ("chat-$(Get-Date -Format 'yyyyMMdd-HHmmss-fff').json")
$result | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $output -Encoding UTF8
Write-Output $result.choices[0].message.content
Write-Output "Response saved to $output"
Write-Output 'For helper participation, also check model allocation and RPC compute logs as described in README.md.'
