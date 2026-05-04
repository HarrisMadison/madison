# setup_and_sync.ps1
# ONE COMMAND TO RUN EVERYTHING:
#   1. Install Python parser dependencies (pdfplumber/docx/xlsx/pptx)
#   2. Enable Document AI OCR (creates processor + writes ID to .env)
#   3. Run the rebuild-only sync (pre-extracts text + OCR + Vertex re-import)
#
# Usage:  cd C:\Users\Harris\Desktop\ClaudeWork\dev\MadisonAve
#         .\Phase5_oneDrive\setup_and_sync.ps1
#
# Run this from any PowerShell window. No admin needed.

$ErrorActionPreference = "Stop"
$RepoRoot = "C:\Users\Harris\Desktop\ClaudeWork\dev\MadisonAve"
Set-Location $RepoRoot

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Madison Ave -- Full Setup + Sync (RAG + OCR bundle)" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# ------------------------------------------------------------
# Step 1: Install Python pre-extraction parsers
# ------------------------------------------------------------
Write-Host "[1/3] Installing Python parser dependencies..." -ForegroundColor Yellow
pip install --quiet --upgrade pdfplumber python-docx openpyxl python-pptx google-cloud-documentai
if ($LASTEXITCODE -ne 0) {
    Write-Host "  FAILED: pip install. Check Python install / proxy / network." -ForegroundColor Red
    exit 1
}
Write-Host "  Done." -ForegroundColor Green
Write-Host ""

# ------------------------------------------------------------
# Step 2: Enable Document AI OCR (only if not already enabled)
# ------------------------------------------------------------
$envContent = Get-Content "$RepoRoot\.env" -Raw
$ocrAlreadyConfigured = $envContent -match 'DOCAI_PROCESSOR_ID="[^"]+"'

if ($ocrAlreadyConfigured) {
    Write-Host "[2/3] OCR processor already configured in .env -- skipping setup." -ForegroundColor Green
} else {
    Write-Host "[2/3] Setting up Document AI OCR..." -ForegroundColor Yellow
    & "$RepoRoot\Phase5_oneDrive\enable_ocr.ps1"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  OCR setup failed. Continuing without OCR (pre-extraction will still run)." -ForegroundColor Yellow
    }
}
Write-Host ""

# ------------------------------------------------------------
# Step 3: Run rebuild-only sync (uses GCS contents, no OneDrive download)
# ------------------------------------------------------------
Write-Host "[3/3] Running rebuild-only sync (pre-extraction + OCR + Vertex re-import)..." -ForegroundColor Yellow
Write-Host "  This walks all 8K+ GCS files, extracts text, and triggers Vertex import." -ForegroundColor Gray
Write-Host "  Expected duration: 30-90 minutes." -ForegroundColor Gray
Write-Host ""

python Phase5_oneDrive\onedrive_sync.py --rebuild-only
$syncExitCode = $LASTEXITCODE

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
if ($syncExitCode -eq 0) {
    Write-Host "  All done. Vertex import is now running on Google's side." -ForegroundColor Green
    Write-Host "  Wait 15-60 minutes for the import to fully complete," -ForegroundColor Green
    Write-Host "  then restart Flask and test queries." -ForegroundColor Green
} else {
    Write-Host "  Sync exited with code $syncExitCode. Check log above." -ForegroundColor Red
}
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""
