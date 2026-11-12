# Creates a desktop shortcut "qbkb Console" that runs scripts\start-console.bat.
# Run it yourself when you want the shortcut (the console never creates it on its own):
#   powershell -ExecutionPolicy Bypass -File scripts\create-desktop-shortcut.ps1
# ASCII only on purpose: Windows PowerShell 5.1 reads BOM-less scripts in the ANSI code page.
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$target = Join-Path $repo "scripts\start-console.bat"
if (-not (Test-Path $target)) { throw "Not found: $target" }
$desktop = [Environment]::GetFolderPath("Desktop")
$link = Join-Path $desktop "qbkb Console.lnk"
$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($link)
$sc.TargetPath = $target
$sc.WorkingDirectory = $repo
$sc.Description = "qbkb console (local web page)"
$sc.IconLocation = "$env:SystemRoot\System32\shell32.dll,13"
$sc.Save()
Write-Host "Shortcut created: $link"
