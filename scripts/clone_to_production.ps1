#
# clone_to_production.ps1
# ------------------------
# One-shot script to clone the current dev folder into a sibling
# production folder.
#
# What it does:
#   1. Copies dev -> prod, skipping junk (.git, __pycache__, output, etc.)
#   2. Copies the OneDrive token cache + delta state so prod runs
#      without re-authenticating to Microsoft.
#   3. Sets a port override in prod's .env so prod runs on 5001 and
#      doesn't fight with dev (which keeps using 5000).
#   4. Disables the auto-sync scheduler in DEV so only prod syncs
#      OneDrive automatically (prevents two copies from fighting
#      over the same OneDrive delta link and Vertex data store).
#
# Run from anywhere (uses absolute paths internally):
#     powershell -ExecutionPolicy Bypass -File scripts\clone_to_production.ps1
#
# After this finishes you can run production with:
#     cd C:\Users\Harris\Desktop\ClaudeWork\prod\MadisonAve\scripts
#     python .\simple_web.py
# ------------------------------------------------------------------

$ErrorActionPreference = "Stop"

$DEV  = "C:\Users\Harris\Desktop\ClaudeWork\dev\MadisonAve"
$PROD = "C:\Users\Harris\Desktop\ClaudeWork\prod\MadisonAve"

Write-Host ""
Write-Host "============================================================"
Write-Host " Clone dev -> prod"
Write-Host "============================================================"
Write-Host "  Source : $DEV"
Write-Host "  Target : $PROD"
Write-Host ""

if (-not (Test-Path $DEV)) {
    Write-Error "Dev folder not found at $DEV"
    exit 1
}

if (Test-Path $PROD) {
    Write-Host "  WARNING: $PROD already exists." -ForegroundColor Yellow
    $resp = Read-Host "  Overwrite? (yes/no)"
    if ($resp -ne "yes") {
        Write-Host "  Aborted."
        exit 0
    }
    Write-Host "  Removing existing $PROD..."
    Remove-Item -Recurse -Force $PROD
}

# Make parent of PROD if it doesn't exist
$prodParent = Split-Path -Parent $PROD
if (-not (Test-Path $prodParent)) {
    New-Item -ItemType Directory -Path $prodParent -Force | Out-Null
}

Write-Host "  Copying files (this can take 30-60s for ~10K-file repos)..."
# Use robocopy for speed + clean exclusion. /XD = exclude directories.
$excludeDirs = @(
    ".git",
    "__pycache__",
    "node_modules",
    "output",
    ".pytest_cache",
    ".venv",
    "venv"
)
$excludeFiles = @(
    "*.pyc",
    "*.pyo",
    "*.log",
    "billing_output.txt",
    "test_idx_output.txt",
    "test_moto.txt",
    "test_search_output.txt",
    "diag_106.txt",
    "diagnose_output.txt",
    "probe_output.txt"
)

$rcArgs = @(
    $DEV,
    $PROD,
    "/E",                          # copy subdirs, including empty
    "/COPY:DAT",                   # copy data, attributes, timestamps
    "/R:1", "/W:1",                # 1 retry, 1 sec wait
    "/NFL", "/NDL", "/NJH", "/NP", # quiet output
    "/XD"
)
$rcArgs += $excludeDirs
$rcArgs += "/XF"
$rcArgs += $excludeFiles

& robocopy @rcArgs | Out-Null
# Robocopy success codes are 0-7. Anything >=8 is a real error.
if ($LASTEXITCODE -ge 8) {
    Write-Error "Robocopy failed with code $LASTEXITCODE"
    exit 1
}

Write-Host "  Files copied." -ForegroundColor Green
Write-Host ""

# -- Override port in production .env -------------------------------------
$prodEnv = Join-Path $PROD ".env"
if (Test-Path $prodEnv) {
    Write-Host "  Updating production .env..."
    $envContent = Get-Content $prodEnv -Raw

    # Append production-specific overrides (don't touch existing settings)
    $marker = "# == Production overrides (added by clone_to_production.ps1) =="
    if ($envContent -notmatch [regex]::Escape($marker)) {
        $additions = @"


$marker
# Run on a different port so dev (5000) and prod (5001) can coexist.
PORT=5001

# Sync scheduler is ENABLED in production (default). Set to false to disable.
SYNC_ENABLED=true
SYNC_INTERVAL_HOURS=5
"@
        Add-Content -Path $prodEnv -Value $additions
        Write-Host "  Added port override and sync settings to prod .env" -ForegroundColor Green
    } else {
        Write-Host "  Production overrides already present in .env (skipping)" -ForegroundColor Yellow
    }
} else {
    Write-Host "  WARNING: $prodEnv does not exist. Production will fail to start until you create it." -ForegroundColor Yellow
}

# -- Disable scheduler in DEV so only prod syncs --------------------------
$devEnv = Join-Path $DEV ".env"
if (Test-Path $devEnv) {
    $devContent = Get-Content $devEnv -Raw
    $devMarker = "# == Dev overrides (added by clone_to_production.ps1) =="
    if ($devContent -notmatch [regex]::Escape($devMarker)) {
        $devAdditions = @"


$devMarker
# Disabled here so production (the cloned copy) is the only thing
# auto-syncing OneDrive. Re-enable manually if you want dev to sync too.
SYNC_ENABLED=false
"@
        Add-Content -Path $devEnv -Value $devAdditions
        Write-Host "  Disabled sync scheduler in dev .env" -ForegroundColor Green
    } else {
        Write-Host "  Dev overrides already present (skipping)" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "============================================================"
Write-Host " DONE." -ForegroundColor Green
Write-Host "============================================================"
Write-Host ""
Write-Host "  Production folder created at:"
Write-Host "    $PROD"
Write-Host ""
Write-Host "  To start production:"
Write-Host "    cd $PROD\scripts"
Write-Host "    python .\simple_web.py"
Write-Host ""
Write-Host "  Production will:"
Write-Host "    * Listen on http://localhost:5001/bob"
Write-Host "    * Run OneDrive sync 30s after startup"
Write-Host "    * Re-sync every 5 hours forever"
Write-Host "    * Reload local index automatically after each sync"
Write-Host ""
Write-Host "  Dev (this folder) will:"
Write-Host "    * Continue running on http://localhost:5000/bob"
Write-Host "    * NOT auto-sync (so it doesn't fight with prod)"
Write-Host "    * You can still run sync manually for testing:"
Write-Host "        cd Phase5_oneDrive; python onedrive_sync.py"
Write-Host ""
Write-Host "  Both copies share the same GCS bucket and Vertex data store."
Write-Host ""
