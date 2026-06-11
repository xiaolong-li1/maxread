$ErrorActionPreference = "Stop"

$InstallDir = if ($env:MAXREAD_INSTALL_DIR) { $env:MAXREAD_INSTALL_DIR } else { "C:\MaxRead" }
$KeysFile = if ($env:MAXREAD_KEYS_FILE) { $env:MAXREAD_KEYS_FILE } else { "C:\MaxReadLocal\maxread.env" }

function Write-MaxReadLog($Message) {
    Write-Host "[maxread-windows] $Message"
}

if (-not (Test-Path $KeysFile)) {
    throw "Env file not found: $KeysFile. Copy deploy\windows\env.windows.example to this path first."
}

if (-not (Test-Path $InstallDir)) {
    New-Item -ItemType Directory -Path $InstallDir | Out-Null
}

if ($PWD.Path -ne $InstallDir) {
    Write-MaxReadLog "Run this script from the MaxRead checkout. Current: $($PWD.Path)"
}

Copy-Item $KeysFile (Join-Path $InstallDir ".env") -Force
New-Item -ItemType Directory -Path (Join-Path $InstallDir "var\maxread") -Force | Out-Null

Set-Location $InstallDir

py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip wheel
.\.venv\Scripts\pip.exe install -e .
.\.venv\Scripts\pip.exe install Pillow

@'
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
Get-Content .env | ForEach-Object {
    if ($_ -match '^\s*#' -or $_ -notmatch '=') { return }
    $parts = $_ -split '=', 2
    [Environment]::SetEnvironmentVariable($parts[0].Trim(), $parts[1].Trim(), 'Process')
}
& .\.venv\Scripts\python.exe -m maxread.cli listen
'@ | Set-Content -Path (Join-Path $InstallDir "run-listener.ps1") -Encoding UTF8

@'
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
Get-Content .env | ForEach-Object {
    if ($_ -match '^\s*#' -or $_ -notmatch '=') { return }
    $parts = $_ -split '=', 2
    [Environment]::SetEnvironmentVariable($parts[0].Trim(), $parts[1].Trim(), 'Process')
}
$hostName = if ($env:MAXREAD_ADMIN_HOST) { $env:MAXREAD_ADMIN_HOST } else { "127.0.0.1" }
$port = if ($env:MAXREAD_ADMIN_PORT) { $env:MAXREAD_ADMIN_PORT } else { "8765" }
& .\.venv\Scripts\python.exe -m maxread.cli admin --host $hostName --port $port
'@ | Set-Content -Path (Join-Path $InstallDir "run-admin.ps1") -Encoding UTF8

Write-MaxReadLog "Installed at $InstallDir"
Write-MaxReadLog "Env copied to $InstallDir\.env"
Write-MaxReadLog "Verify Feishu auth with: lark-cli doctor"
Write-MaxReadLog "Run listener: .\run-listener.ps1"
Write-MaxReadLog "Run admin: .\run-admin.ps1"
