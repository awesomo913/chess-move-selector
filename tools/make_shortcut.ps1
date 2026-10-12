# Creates the Desktop shortcut "Chess Move Selector".
# Run: powershell.exe -NoProfile -ExecutionPolicy Bypass -File tools\make_shortcut.ps1
$ErrorActionPreference = 'Stop'
# repo root = the folder that contains this script's tools/ directory
$root   = Split-Path -Parent $PSScriptRoot
$target = Join-Path $root '.venv\Scripts\pythonw.exe'
$script = Join-Path $root 'src\app.py'
$icon   = Join-Path $root 'tools\icon.ico'
if (-not (Test-Path $target)) { throw "missing $target" }
if (-not (Test-Path $script)) { throw "missing $script" }

$ws  = New-Object -ComObject WScript.Shell
$lnkPath = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Chess Move Selector.lnk'
$lnk = $ws.CreateShortcut($lnkPath)
$lnk.TargetPath        = $target
$lnk.Arguments         = '"' + $script + '"'
$lnk.WorkingDirectory  = $root
if (Test-Path $icon) { $lnk.IconLocation = "$icon,0" }
$lnk.Description       = 'Live Stockfish move suggestions for chess.com'
$lnk.Save()
Write-Output "shortcut: $lnkPath"
Write-Output ("exists: " + (Test-Path $lnkPath))
