[CmdletBinding()]
param(
    [string]$AdminToken = $env:LOWLLAMA_ADMIN_TOKEN,
    [string]$ModelId = "qwen2.5-0.5b-instruct-q4_k_m",
    [string[]]$NodeIds = @(),
    [string]$Prompt = "What is the capital of France?"
)

$ErrorActionPreference = 'Stop'
$baseUrl = "http://127.0.0.1:8000"

if (-not $AdminToken) {
    Write-Error "Admin token must be provided via -AdminToken or LOWLLAMA_ADMIN_TOKEN environment variable."
    exit 1
}

$headers = @{
    "Authorization" = "Bearer $AdminToken"
    "Content-Type" = "application/json"
}

Write-Host "--- Phase 2 API Flow Test ---" -ForegroundColor Cyan

# 1. Check Nodes
Write-Host "[1/5] Checking registered nodes..."
try {
    $nodes = Invoke-RestMethod "$baseUrl/nodes" -Headers $headers -Method Get
    Write-Host "Found $($nodes.Count) nodes."
    if ($nodes.Count -eq 0 -and $NodeIds.Count -eq 0) {
        Write-Warning "No nodes registered. Registration/Join required for real test."
    }
} catch {
    Write-Error "Failed to fetch nodes: $_"
}

# 2. Start Model
if ($NodeIds.Count -eq 0) {
    Write-Warning "No Node IDs provided. Attempting to use all online nodes."
    $NodeIds = $nodes | Where-Object { $_.status -eq 'online' } | Select-Object -ExpandProperty node_id
    if ($NodeIds.Count -eq 0) {
        Write-Error "No online nodes available to start model."
        exit 1
    }
}

Write-Host "[2/5] Starting model '$ModelId' on nodes: $($NodeIds -join ', ')..."
$startBody = @{
    model_id = $ModelId
    node_ids = $NodeIds
} | ConvertTo-Json
try {
    $startResult = Invoke-RestMethod "$baseUrl/start" -Headers $headers -Method Post -Body $startBody
    Write-Host "Start requested. Status: $($startResult.status)"
} catch {
    Write-Error "Failed to start model: $_"
    exit 1
}

# 3. Poll Status
Write-Host "[3/5] Polling status until ready..."
$ready = $false
$attempts = 0
$maxAttempts = 30
while (-not $ready -and $attempts -lt $maxAttempts) {
    $attempts++
    $statusResult = Invoke-RestMethod "$baseUrl/status" -Headers $headers -Method Get
    Write-Host "Status: $($statusResult.status) (Attempt $attempts/$maxAttempts)"
    if ($statusResult.status -eq 'ready') {
        $ready = $true
    } elseif ($statusResult.status -eq 'error') {
        Write-Error "Model entered error state: $($statusResult.error)"
        exit 1
    } else {
        Start-Sleep -Seconds 2
    }
}

if (-not $ready) {
    Write-Error "Model failed to reach 'ready' state within timeout."
    exit 1
}

# 4. Chat
Write-Host "[4/5] Sending chat request..."
$chatBody = @{
    messages = @(@{ role = "user"; content = $Prompt })
    max_tokens = 128
    temperature = 0.7
    stream = $false
} | ConvertTo-Json
try {
    $chatResult = Invoke-RestMethod "$baseUrl/chat" -Headers $headers -Method Post -Body $chatBody
    Write-Host "`nResponse from model:" -ForegroundColor Green
    Write-Host $chatResult.choices[0].message.content
} catch {
    Write-Error "Chat request failed: $_"
}

# 5. Stop
Write-Host "`n[5/5] Stopping model..."
try {
    $stopResult = Invoke-RestMethod "$baseUrl/stop" -Headers $headers -Method Post
    Write-Host "Stopped. Status: $($stopResult.status)"
} catch {
    Write-Error "Failed to stop model: $_"
}

Write-Host "`n--- API Flow Test Complete ---" -ForegroundColor Cyan
