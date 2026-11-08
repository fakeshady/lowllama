[CmdletBinding()]
param(
    [string]$CoordinatorUrl = "http://127.0.0.1:8000",
    [string]$AdminToken = $env:LOWLLAMA_ADMIN_TOKEN,
    [string]$ModelId = "qwen2.5-0.5b-instruct-q4_k_m",
    [string[]]$NodeIds = @(),
    [string]$Prompt = "What is the capital of France?"
)

$ErrorActionPreference = 'Stop'

# Normalize URL: remove trailing slash
$baseUrl = $CoordinatorUrl.TrimEnd('/')

if (-not $AdminToken) {
    Write-Error "Admin token must be provided via -AdminToken or LOWLLAMA_ADMIN_TOKEN environment variable."
    exit 1
}

$headers = @{
    "Authorization" = "Bearer $AdminToken"
    "Content-Type" = "application/json"
}

Write-Host "--- Phase 2 API Flow Test ---" -ForegroundColor Cyan
Write-Host "Coordinator URL: $baseUrl"

# 1. Check Nodes
Write-Host "[1/5] Checking registered nodes..."
try {
    $nodes = Invoke-RestMethod "$baseUrl/nodes" -Headers $headers -Method Get
    Write-Host "Found $($nodes.Count) nodes."
} catch {
    Write-Error "Failed to fetch nodes: $_"
    exit 1
}

# 2. Start Model
if ($NodeIds.Count -eq 0) {
    Write-Warning "No Node IDs provided. Attempting to use only online and rpc_ready nodes."
    $NodeIds = $nodes | Where-Object { $_.status -eq 'online' -and $_.rpc_ready -eq $true } | Select-Object -ExpandProperty node_id
    if ($NodeIds.Count -eq 0) {
        Write-Error "No online and rpc_ready nodes available to start model."
        exit 1
    }
    Write-Host "Auto-selected nodes: $($NodeIds -join ', ')"
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
    $err = $_.Exception.Response
    if ($null -ne $err) {
        $reader = New-Object System.IO.StreamReader($err.GetResponseStream())
        $body = $reader.ReadToEnd()
        Write-Error "Failed to start model: $body"
    } else {
        Write-Error "Failed to start model: $_"
    }
    exit 1
}

$startedSuccessfully = $true
try {
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
            $startedSuccessfully = $false # Trigger stop
            break
        } else {
            Start-Sleep -Seconds 2
        }
    }

    if (-not $ready -and $startedSuccessfully) {
        Write-Error "Model failed to reach 'ready' state within timeout."
        $startedSuccessfully = $false # Trigger stop
    }

    if ($ready) {
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
            if ($chatResult.choices -and $chatResult.choices[0].message.content) {
                Write-Host "`nResponse from model:" -ForegroundColor Green
                Write-Host $chatResult.choices[0].message.content
            } else {
                Write-Error "Chat request returned empty or invalid response."
            }
        } catch {
            Write-Error "Chat request failed: $_"
        }
    }
} finally {
    # 5. Stop (Always attempt if we requested start, unless we got 409 or similar where it didn't start)
    # Actually, if the coordinator says it is starting/ready, we should stop it.
    if ($startedSuccessfully) {
        Write-Host "`n[5/5] Stopping model..."
        try {
            $stopResult = Invoke-RestMethod "$baseUrl/stop" -Headers $headers -Method Post
            Write-Host "Stopped. Status: $($stopResult.status)"
            
            # Verify status is stopped
            $finalStatus = Invoke-RestMethod "$baseUrl/status" -Headers $headers -Method Get
            Write-Host "Final Status: $($finalStatus.status)"
        } catch {
            Write-Error "Failed to stop model: $_"
        }
    }
}

if (-not $ready) { exit 1 }
Write-Host "`n--- API Flow Test Complete ---" -ForegroundColor Cyan
