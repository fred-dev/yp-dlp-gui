# Build a packaged Windows app (.msi installer) in dist\.
#
#   powershell -ExecutionPolicy Bypass -File scripts\build.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\build.ps1 -Format zip    # portable zip instead of MSI
param([string]$Format = "msi")
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "uv is not installed. Install it with:"
    Write-Host '  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"'
    exit 1
}

function Run { & $args[0] $args[1..($args.Length - 1)]; if ($LASTEXITCODE) { exit $LASTEXITCODE } }

Run uv python install          # version comes from .python-version
Run uv sync --group dev

if (Test-Path "build\ytdlp_gui\windows") {
    # Refresh code and re-resolve requirements so the newest yt-dlp ships.
    Run uv run briefcase update windows app --update-requirements --no-input
} else {
    Run uv run briefcase create windows app --no-input
}
Run uv run briefcase build windows app --no-input
Run uv run briefcase package windows app --no-input -p $Format

Write-Host "`nDone. Package(s):"
Get-ChildItem dist
