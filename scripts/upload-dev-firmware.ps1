<#
.SYNOPSIS
    Builds and/or uploads ESP32 firmware to the Hannah OTA update server.

.DESCRIPTION
    Reads HANNAH_UPDATE_TOKEN and HANNAH_UPDATE_BASE_URL from environment or
    a .env file in the repo root. Never store credentials in this script or git.

    Required env vars:
        HANNAH_UPDATE_TOKEN       Bearer token for the update server
        HANNAH_UPDATE_BASE_URL    Base URL, e.g. https://hannah-update.example.com

.EXAMPLE
    # Build + upload to dev channel
    .\scripts\upload-dev-firmware.ps1

.EXAMPLE
    # Skip build, upload existing binary to a custom channel
    .\scripts\upload-dev-firmware.ps1 -NoBuild -Channel beta

.EXAMPLE
    # Build the Rev5 config instead of the default Rev4
    .\scripts\upload-dev-firmware.ps1 -Rev rev5
#>

param(
    [string]$Channel = "satellite-esp-dev",
    [string]$Rev = "rev4",
    [switch]$NoBuild,
    [switch]$List,
    [string]$Delete = "",
    [string]$Version = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path $PSScriptRoot -Parent
$BinPath  = Join-Path $RepoRoot "satellite-esp\build\hannah_satellite.bin"

# Load .env if present
$EnvFile = Join-Path $RepoRoot ".env"
if (Test-Path $EnvFile) {
    Get-Content $EnvFile | ForEach-Object {
        if ($_ -match '^\s*([^#=]+?)\s*=\s*(.+?)\s*$') {
            $name = $Matches[1]; $val = $Matches[2] -replace '^[''"]|[''"]$', ''
            if (-not [Environment]::GetEnvironmentVariable($name)) {
                [Environment]::SetEnvironmentVariable($name, $val, "Process")
            }
        }
    }
}

# Validate required env vars
$Token   = $env:HANNAH_UPDATE_TOKEN
$BaseUrl = $env:HANNAH_UPDATE_BASE_URL
if (-not $Token)   { Write-Error "HANNAH_UPDATE_TOKEN is not set.";    exit 1 }
if (-not $BaseUrl) { Write-Error "HANNAH_UPDATE_BASE_URL is not set."; exit 1 }

# List
if ($List) {
    $ListUrl = "${BaseUrl}/releases?channel=${Channel}"
    Write-Host "Firmware list (channel: $Channel):" -ForegroundColor Cyan
    $Response = Invoke-WebRequest -Uri $ListUrl -Headers @{ Authorization = "Bearer $Token" } -UseBasicParsing
    $Json = $Response.Content | ConvertFrom-Json
    $Versions = $Json.version
    $Hashes   = $Json.sha256
    $Sizes    = $Json.size
    for ($i = 0; $i -lt $Versions.Count; $i++) {
        Write-Host ""
        Write-Host "  v: $($Versions[$i])" -ForegroundColor White
        Write-Host "  sha256: $($Hashes[$i])"
        Write-Host "  size: $([math]::Round([int]$Sizes[$i] / 1024)) KB"
    }
    Write-Host ""
    exit 0
}

# Delete
if ($Delete) {
    $DeleteUrl = "${BaseUrl}/releases/${Delete}?channel=${Channel}"
    Write-Host "Deleting $Delete from channel '$Channel'..." -ForegroundColor Yellow
    $Response = Invoke-WebRequest -Method Delete -Uri $DeleteUrl -Headers @{ Authorization = "Bearer $Token" } -UseBasicParsing
    Write-Host "Done. Server responded: $($Response.StatusCode)" -ForegroundColor Green
    exit 0
}

# Build
if (-not $NoBuild) {
    Write-Host "Activating ESP-IDF..." -ForegroundColor Cyan
    & "$env:UserProfile\esp\v6.0\esp-idf\export.ps1"

    $Sdkconfig = Join-Path $RepoRoot "satellite-esp\sdkconfig"
    if (Test-Path $Sdkconfig) {
        Remove-Item $Sdkconfig -Force
        Write-Host "sdkconfig deleted - rebuilding from defaults." -ForegroundColor Cyan
    }

    $RevSdkconfig = Join-Path $RepoRoot "satellite-esp\sdkconfig.defaults.$Rev"
    if (-not (Test-Path $RevSdkconfig)) {
        Write-Error "No sdkconfig.defaults.$Rev found under satellite-esp/."; exit 1
    }

    Write-Host "Building firmware ($Rev config)..." -ForegroundColor Cyan
    Push-Location (Join-Path $RepoRoot "satellite-esp")
    try {
        idf.py -DSDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.defaults.$Rev;sdkconfig.ci" build
        if ($LASTEXITCODE -ne 0) { Write-Error "idf.py build failed."; exit 1 }
    } finally {
        Pop-Location
    }
}

# Abort if working tree is dirty — dirty builds break OTA version matching
$DirtyFiles = git -C $RepoRoot status --porcelain 2>$null
if ($DirtyFiles) {
    if ($NoBuild) {
        Write-Warning "Working tree is dirty. If the binary was built before committing, it contains a -dirty version string that will cause an OTA update loop. Rebuild without -NoBuild to be safe."
    } else {
        Write-Error "Working tree has uncommitted changes - binary version will be tagged -dirty. Commit or stash first."; exit 1
    }
}

# Determine version
if (-not $Version) {
    $Version = git -C $RepoRoot describe --tags --always --dirty 2>$null
    if (-not $Version) { $Version = "dev" }
}

# Upload
if (-not (Test-Path $BinPath)) {
    Write-Error "Binary not found: $BinPath"
    exit 1
}

$UploadUrl = "${BaseUrl}/releases/${Version}?channel=${Channel}"
Write-Host "Uploading $Version to channel '$Channel'..." -ForegroundColor Cyan
Write-Host "  URL: $UploadUrl"

$Response = Invoke-WebRequest `
    -Method Post `
    -Uri $UploadUrl `
    -Headers @{ Authorization = "Bearer $Token"; "Content-Type" = "application/octet-stream" } `
    -InFile $BinPath `
    -UseBasicParsing

Write-Host "Done. Server responded: $($Response.StatusCode) $($Response.StatusDescription)" -ForegroundColor Green
