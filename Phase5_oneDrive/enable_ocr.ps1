# enable_ocr.ps1
# One-shot: enables Document AI OCR for the Madison Ave RAG project
# and writes the processor ID to .env so the next sync uses it.
#
# Uses the Document AI REST API directly (works on any gcloud SDK version,
# regardless of whether the documentai CLI component is installed).
#
# Usage:  cd C:\Users\Harris\Desktop\ClaudeWork\dev\MadisonAve
#         .\Phase5_oneDrive\enable_ocr.ps1

$ErrorActionPreference = "Stop"

$ProjectId = "madison-rag-60"
$Location  = "us"
$EnvFile   = "C:\Users\Harris\Desktop\ClaudeWork\dev\MadisonAve\.env"

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "  Document AI OCR Setup for Madison Ave"  -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""

# ------------------------------------------------------------
# Step 1: enable the API
# ------------------------------------------------------------
Write-Host "[1/3] Enabling documentai.googleapis.com..." -ForegroundColor Yellow
gcloud services enable documentai.googleapis.com --project=$ProjectId
if ($LASTEXITCODE -ne 0) {
    Write-Host "  FAILED to enable API. Make sure 'gcloud' is installed and you are authenticated." -ForegroundColor Red
    Write-Host "  Run: gcloud auth login" -ForegroundColor Red
    exit 1
}
Write-Host "  Done." -ForegroundColor Green

# ------------------------------------------------------------
# Get an access token for REST API calls
# ------------------------------------------------------------
$accessToken = (gcloud auth print-access-token).Trim()
if (-not $accessToken) {
    Write-Host "  Could not get access token. Run 'gcloud auth login' first." -ForegroundColor Red
    exit 1
}
$headers = @{ "Authorization" = "Bearer $accessToken" }

$apiBase = "https://$Location-documentai.googleapis.com/v1/projects/$ProjectId/locations/$Location/processors"

# ------------------------------------------------------------
# Step 2: list existing processors (in case one already exists)
# ------------------------------------------------------------
Write-Host ""
Write-Host "[2/3] Checking for existing OCR processor..." -ForegroundColor Yellow

$processorId = ""
try {
    $listResponse = Invoke-RestMethod -Uri $apiBase -Headers $headers -Method GET
    if ($listResponse.processors) {
        foreach ($p in $listResponse.processors) {
            if ($p.type -eq "OCR_PROCESSOR") {
                # name format: projects/{num}/locations/us/processors/{id}
                $processorId = ($p.name -split "/")[-1]
                Write-Host "  Found existing OCR processor: $processorId" -ForegroundColor Green
                break
            }
        }
    }
} catch {
    Write-Host "  Could not list processors (will try to create one). Error: $_" -ForegroundColor Yellow
}

# ------------------------------------------------------------
# Create one if none found
# ------------------------------------------------------------
if (-not $processorId) {
    Write-Host "  No existing OCR processor. Creating new one..." -ForegroundColor Yellow
    $body = @{
        type        = "OCR_PROCESSOR"
        displayName = "madison-ocr"
    } | ConvertTo-Json

    try {
        $createResponse = Invoke-RestMethod -Uri $apiBase -Headers $headers -Method POST `
            -Body $body -ContentType "application/json"
        $processorId = ($createResponse.name -split "/")[-1]
        Write-Host "  Created processor: $processorId" -ForegroundColor Green
    } catch {
        Write-Host "  FAILED to create processor. Error: $_" -ForegroundColor Red
        Write-Host "  Response body:" -ForegroundColor Red
        if ($_.ErrorDetails.Message) {
            Write-Host "    $($_.ErrorDetails.Message)" -ForegroundColor Red
        }
        exit 1
    }
}

Write-Host ""
Write-Host "  Processor ID: $processorId" -ForegroundColor Green

# ------------------------------------------------------------
# Step 3: write to .env
# ------------------------------------------------------------
Write-Host ""
Write-Host "[3/3] Updating .env with DOCAI_PROCESSOR_ID..." -ForegroundColor Yellow

$envContent = Get-Content $EnvFile -Raw
if ($envContent -match 'DOCAI_PROCESSOR_ID="?([^"\r\n]*)"?') {
    # Replace existing line
    $envContent = $envContent -replace 'DOCAI_PROCESSOR_ID="?[^"\r\n]*"?', "DOCAI_PROCESSOR_ID=`"$processorId`""
    Write-Host "  Updated existing DOCAI_PROCESSOR_ID line." -ForegroundColor Green
} else {
    # Append new line
    $envContent = $envContent.TrimEnd() + "`r`n`r`n# Document AI OCR processor (Phase 6)`r`nDOCAI_PROCESSOR_ID=`"$processorId`"`r`n"
    Write-Host "  Added new DOCAI_PROCESSOR_ID line." -ForegroundColor Green
}
Set-Content -Path $EnvFile -Value $envContent -NoNewline

Write-Host ""
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "  OCR setup complete." -ForegroundColor Green
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""
