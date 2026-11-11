# Start the Project Tailor server on Windows:
#   powershell -ExecutionPolicy Bypass -File scripts\start_windows.ps1                 (runs here, Ctrl+C to stop)
#   powershell -ExecutionPolicy Bypass -File scripts\start_windows.ps1 -Install        (starts hidden at every login)
#   powershell -ExecutionPolicy Bypass -File scripts\start_windows.ps1 -Uninstall
#   -Model stub    runs without a local model (no free-text writing); -OllamaModel qwen3:4b-instruct for a smaller one
# -Install adds a small launcher to your Startup folder: no administrator rights, nothing outside your user account.
# The server log is %USERPROFILE%\project_tailor_data\logs\server.log
param([switch]$Install, [switch]$Uninstall, [string]$Model = "ollama", [string]$OllamaModel = "qwen3:8b")
$ErrorActionPreference = "Stop"

$Project = Split-Path -Parent $PSScriptRoot
$Data = if ($env:TAILOR_DATA) { $env:TAILOR_DATA } else { Join-Path $HOME "project_tailor_data" }
$Py = Join-Path $Data "venv\Scripts\python.exe"
$Server = Join-Path $Project "scripts\run_server.py"
$Launcher = Join-Path ([Environment]::GetFolderPath("Startup")) "ProjectTailorServer.vbs"
$Log = Join-Path $Data "logs\server.log"

if ($Uninstall) {
    if (Test-Path $Launcher) { Remove-Item $Launcher; Write-Host "Removed: the server no longer starts at login." }
    else { Write-Host "Nothing to remove." }
    exit 0
}
if (-not (Test-Path $Py)) { Write-Host "Run scripts\setup_windows.ps1 first (no Python environment at $Py)."; exit 1 }
$Args = "--model $Model" + $(if ($Model -eq "ollama") { " --ollama-model $OllamaModel" } else { "" })

if ($Install) {
    New-Item -ItemType Directory -Force -Path (Split-Path $Log) | Out-Null
    # a hidden window: cmd runs the server and appends to the log (VBScript's Run with 0 = no console window)
    $cmd = "cmd /c """"$Py"" ""$Server"" $Args >> ""$Log"" 2>&1"""
    Set-Content -Path $Launcher -Encoding ASCII -Value "CreateObject(""WScript.Shell"").Run ""$($cmd.Replace('"', '""'))"", 0, False"
    Start-Process -FilePath "wscript.exe" -ArgumentList "`"$Launcher`""
    Write-Host "Installed: the server starts at every login and is starting now. Log: $Log"
    Write-Host "Check it in a minute at http://127.0.0.1:8765/api/health"
    exit 0
}

& $Py $Server $Args.Split(" ")
