# One-time setup on Windows 10/11, from the project folder in PowerShell:
#   powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1 -Bundle C:\path\project_tailor_data_bundle.zip
# Installs the Python packages and the PDF browser under %USERPROFILE%\project_tailor_data, puts your own data there
# (from a bundle made on your other computer with scripts\bundle_my_data.py, if you give one), then runs the self-check.
# It downloads about 700 MB (packages + Chromium). Nothing personal leaves the machine.
param([string]$Bundle = "")
$ErrorActionPreference = "Stop"

$Project = Split-Path -Parent $PSScriptRoot
$Data = if ($env:TAILOR_DATA) { $env:TAILOR_DATA } else { Join-Path $HOME "project_tailor_data" }
Write-Host "Project: $Project"
Write-Host "Data:    $Data   (your profile lives here: keep it out of OneDrive / Dropbox)"

# Python 3.12 or 3.13 through the Windows launcher ("py"), else whatever "python" is
$PyCmd = $null
foreach ($v in @("3.13", "3.12", "3.11")) {
    try { & py "-$v" -c "import sys" 2>$null; if ($LASTEXITCODE -eq 0) { $PyCmd = @("py", "-$v"); break } } catch {}
}
if (-not $PyCmd) {
    try { & python -c "import sys; assert sys.version_info >= (3, 11)" 2>$null; if ($LASTEXITCODE -eq 0) { $PyCmd = @("python") } } catch {}
}
if (-not $PyCmd) {
    Write-Host "Python 3.12 or 3.13 is needed: https://www.python.org/downloads/windows/ (tick 'Add python.exe to PATH')."
    exit 1
}
Write-Host "Python:  $($PyCmd -join ' ')"

New-Item -ItemType Directory -Force -Path $Data | Out-Null
$free = (Get-PSDrive -Name ($Data.Substring(0, 1))).Free / 1GB
Write-Host ("Free disk space here: {0:N1} GB (this setup needs about 0.6 GB; the local model is extra, about 5 GB for qwen3:8b)" -f $free)

if ($Bundle) {
    if (-not (Test-Path $Bundle)) { Write-Host "No bundle at $Bundle"; exit 1 }
    Write-Host "Unpacking your data from $Bundle ..."
    Expand-Archive -Path $Bundle -DestinationPath $Data -Force
}
if (-not (Test-Path (Join-Path $Data "profile\base_resume.json"))) {
    Write-Host ""
    Write-Host "No profile yet at $Data\profile. Either rerun with -Bundle <zip made by scripts\bundle_my_data.py>,"
    Write-Host "or start one from your resume after this setup:  $Data\venv\Scripts\python.exe scripts\new_profile.py --resume my_resume.pdf"
}

$Venv = Join-Path $Data "venv"
& $PyCmd[0] $PyCmd[1..9] -m venv $Venv
$Py = Join-Path $Venv "Scripts\python.exe"
& $Py -m pip install --no-cache-dir --upgrade pip
& $Py -m pip install --no-cache-dir -r (Join-Path $Project "requirements.txt")

$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $Data "browsers"
# Chromium for the PDFs and for the extension tests (the extension itself runs in your own Chrome)
& $Py -m playwright install chromium

Write-Host ""
& $Py (Join-Path $Project "scripts\check_setup.py")

Write-Host @"

Next:
  1. Local model (optional, the big download): install Ollama from https://ollama.com/download/windows, then ONE of
       ollama pull qwen3:8b            (about 5 GB, best writing)
       ollama pull qwen3:4b-instruct   (about 2.5 GB)
     Without a model everything works except the free-text writing help.
  2. Start the server:      powershell -ExecutionPolicy Bypass -File scripts\start_windows.ps1
     Start it at every login instead:  ... scripts\start_windows.ps1 -Install
  3. In Chrome: chrome://extensions -> Developer mode -> Load unpacked -> $Project\extension
     Then open the extension's Options page and paste the token from $Data\server_token.txt
"@
