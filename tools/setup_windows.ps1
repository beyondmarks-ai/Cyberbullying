param(
    [switch]$CheckOnly,
    [switch]$Configure,
    [switch]$LocalOnly,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))

function Refresh-ToolPath {
    # Preserve this shell's custom paths as well as newly installed user tools.
    $env:Path = (@($env:Path, [Environment]::GetEnvironmentVariable('Path', 'User'),
        [Environment]::GetEnvironmentVariable('Path', 'Machine'),
        (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links')) -join ';')
}

function Invoke-Checked([string]$Program, [string[]]$Arguments) {
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue' # Native stderr is not itself an exit failure.
        & $Program @Arguments
        $result = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previousPreference }
    if ($result -ne 0) { throw "A required command failed (exit $result). Check the message above, then run start-dashboard.cmd again." }
}

function Find-CompatiblePython {
    $candidates = @((Join-Path $projectRoot '.venv\Scripts\python.exe'))
    foreach ($name in @('py.exe', 'python.exe', 'python3.exe')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) { $candidates += $command.Source }
    }
    foreach ($version in @('313', '312', '311', '314')) {
        $candidates += Join-Path $env:LOCALAPPDATA "Programs\Python\Python$version\python.exe"
    }
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $candidate)) { continue }
        if ($candidate -like '*\WindowsApps\python*.exe') { continue } # Do not launch Store aliases.
        $prefixes = @('')
        if ([IO.Path]::GetFileName($candidate) -eq 'py.exe') { $prefixes = @('-3.13', '-3.12', '-3.11', '-3.14') }
        foreach ($prefix in $prefixes) {
            $arguments = @('-c', 'import sys; print(sys.executable) if (3,11) <= sys.version_info[:2] < (3,15) else sys.exit(1)')
            if ($prefix) { $arguments = @($prefix) + $arguments }
            $previousPreference = $ErrorActionPreference
            try {
                $ErrorActionPreference = 'Continue'
                $found = & $candidate @arguments 2>$null
                $result = $LASTEXITCODE
            } finally { $ErrorActionPreference = $previousPreference }
            if ($result -eq 0 -and $found -and (Test-Path -LiteralPath ([string]$found))) { return [string]$found }
        }
    }
    return $null
}

function Install-WingetTool([string]$Id, [switch]$PerUser) {
    if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
        throw 'Windows App Installer (winget) is missing. Install/update App Installer from Microsoft Store, then double-click start-dashboard.cmd again. Offline/manual instructions are in README.md.'
    }
    Write-Host "Installing $Id using Windows Package Manager. Review any Windows permission prompts."
    $arguments = @('install', '--id', $Id, '--exact', '--source', 'winget',
        '--accept-source-agreements', '--accept-package-agreements', '--disable-interactivity')
    if ($PerUser) { $arguments += @('--scope', 'user') }
    Invoke-Checked 'winget.exe' $arguments
    Refresh-ToolPath
}

function Test-DashboardRunning {
    $client = New-Object Net.Sockets.TcpClient
    try {
        $attempt = $client.BeginConnect('127.0.0.1', 8765, $null, $null)
        if (-not $attempt.AsyncWaitHandle.WaitOne(1000)) { return $false }
        $client.EndConnect($attempt)
        return $true
    } catch { return $false }
    finally { $client.Dispose() }
}

function Test-LocalConfiguration([string]$Python, [string]$ConfigTool) {
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $Python $ConfigTool --check | Out-Host
        return $LASTEXITCODE -eq 0
    } finally { $ErrorActionPreference = $previousPreference }
}

function Start-Setup {
    Set-Location -LiteralPath $projectRoot
    Write-Host 'Instagram Safety Monitor - Windows setup and launch'
    Write-Host 'First run needs internet. Missing tools are installed through winget; cloud accounts and keys are not created.'
    if (-not $CheckOnly -and (Test-DashboardRunning)) {
        Write-Host 'Port 8765 is already in use. No packages or settings were changed.'
        Write-Host 'If this is your dashboard, open http://127.0.0.1:8765. To update/configure it, stop its launcher first.'
        return
    }
    Refresh-ToolPath
    $python = Find-CompatiblePython
    if (-not $python) {
        if ($CheckOnly) { throw 'Python 3.11-3.14 is missing.' }
        Install-WingetTool 'Python.Python.3.13' -PerUser
        $python = Find-CompatiblePython
        if (-not $python) { throw 'Python was not found after installation. Close this window and double-click start-dashboard.cmd again.' }
    }
    foreach ($tool in @('ffmpeg.exe', 'ffprobe.exe')) {
        if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
            if ($CheckOnly) { throw "$tool is missing. Run start-dashboard.cmd to install media tools." }
            Install-WingetTool 'Gyan.FFmpeg'
            break
        }
    }
    foreach ($tool in @('ffmpeg.exe', 'ffprobe.exe')) {
        if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { throw "$tool is not on PATH. Reopen this launcher after installation, or follow the manual setup guide." }
        Invoke-Checked $tool @('-version') | Select-Object -First 1 | Out-Host
    }
    $venv = Join-Path $projectRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venv)) {
        if ($CheckOnly) { throw 'Project environment is missing. Double-click start-dashboard.cmd to create it.' }
        Write-Host 'Creating the project Python environment...'
        Invoke-Checked $python @('-m', 'venv', (Join-Path $projectRoot '.venv'))
    }
    if (-not $CheckOnly) {
        # pip is idempotent: checks exact requirements and installs only missing/changed packages.
        Write-Host 'Checking/installing Python packages...'
        Invoke-Checked $venv @('-m', 'pip', 'install', '--disable-pip-version-check', '-r', (Join-Path $projectRoot 'requirements.txt'))
    }
    Invoke-Checked $venv @('-m', 'pip', 'check')
    Invoke-Checked $venv @('-c', 'import httpx, PIL, azure.storage.blob')
    Write-Host 'Python dependencies: OK'
    $configTool = Join-Path $PSScriptRoot 'setup_config.py'
    if ($CheckOnly) {
        Invoke-Checked $venv @($configTool, '--check')
        Write-Host 'Local prerequisites and configuration fields: OK. Credentials, cloud permissions and Meta callbacks still require a live test.'
        return
    }
    $needsConfig = -not (Test-LocalConfiguration $venv $configTool)
    if ($Configure -or $needsConfig) { Invoke-Checked $venv @($configTool) }
    $arguments = @((Join-Path $PSScriptRoot 'launch_dashboard.py'))
    if ($LocalOnly) { $arguments += '--local-only' }
    if ($NoBrowser) { $arguments += '--no-browser' }
    Invoke-Checked $venv $arguments
}

if ($MyInvocation.InvocationName -ne '.') {
    try { Start-Setup }
    catch { Write-Host "Setup stopped: $($_.Exception.Message)" -ForegroundColor Red; exit 1 }
}
