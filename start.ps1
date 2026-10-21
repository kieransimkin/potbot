# Windows PowerShell 5.1 and PowerShell 7 on Windows; no Python/uv prerequisite.
# Examples: .\start.ps1 --help; .\start.ps1 dependencies status
$PotbotOriginalArguments = @($args)
$ErrorActionPreference = 'Stop'
$PotbotRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$PotbotUvVersion = '0.12.23'
$PotbotUvDirectory = Join-Path $PotbotRoot '.potbot\runtime\uv'
$PotbotUv = $null

function Write-PotbotProgress([string] $Message) {
    [Console]::Error.WriteLine('[Potbot startup] ' + $Message)
}

function ConvertTo-PotbotNativeArgument([AllowEmptyString()][string] $Value) {
    # Windows CommandLineToArgvW/C-runtime escaping. This also preserves empty
    # arguments and literal quotes under Windows PowerShell 5.1's legacy mode.
    $Escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $Escaped = [regex]::Replace($Escaped, '(\\+)$', '$1$1')
    return '"' + $Escaped + '"'
}

function Invoke-PotbotProcess([string] $Executable, [string[]] $Arguments) {
    $Info = New-Object System.Diagnostics.ProcessStartInfo
    $Info.FileName = $Executable
    $Info.Arguments = (($Arguments | ForEach-Object { ConvertTo-PotbotNativeArgument $_ }) -join ' ')
    $Info.UseShellExecute = $false
    # Inherit all three console streams: keyring onboarding must retain stdin.
    $Info.RedirectStandardInput = $false
    $Info.RedirectStandardOutput = $false
    $Info.RedirectStandardError = $false
    $Process = [System.Diagnostics.Process]::Start($Info)
    try {
        $Process.WaitForExit()
        return $Process.ExitCode
    }
    finally { $Process.Dispose() }
}

function Test-PotbotUv([string] $Candidate) {
    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { return $false }
    try {
        $Version = & $Candidate --version 2>$null
        return ($LASTEXITCODE -eq 0 -and $Version -match ('^uv ' + [regex]::Escape($PotbotUvVersion) + '( |$)'))
    }
    catch { return $false }
}

try {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw 'Use start.sh on Linux or macOS.'
    }
    Write-PotbotProgress "1/4 Checking uv $PotbotUvVersion"
    $LocalUv = Join-Path $PotbotUvDirectory 'uv.exe'
    $PathUv = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (Test-PotbotUv $LocalUv) { $PotbotUv = $LocalUv }
    elseif ($PathUv -and (Test-PotbotUv $PathUv.Source)) { $PotbotUv = $PathUv.Source }
    else {
        Write-PotbotProgress "Downloading uv $PotbotUvVersion from Astral"
        New-Item -ItemType Directory -Path $PotbotUvDirectory -Force | Out-Null
        $Installer = Join-Path ([System.IO.Path]::GetTempPath()) ('potbot-uv-' + [guid]::NewGuid().ToString('N') + '.ps1')
        $OldUnmanagedInstall = $env:UV_UNMANAGED_INSTALL
        $OldNoModifyPath = $env:UV_NO_MODIFY_PATH
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest -UseBasicParsing -Uri "https://astral.sh/uv/$PotbotUvVersion/install.ps1" -OutFile $Installer
            $env:UV_UNMANAGED_INSTALL = $PotbotUvDirectory
            $env:UV_NO_MODIFY_PATH = '1'
            $Shell = Join-Path $PSHOME 'powershell.exe'
            if (-not (Test-Path -LiteralPath $Shell)) { $Shell = Join-Path $PSHOME 'pwsh.exe' }
            $Code = Invoke-PotbotProcess $Shell @('-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $Installer)
            if ($Code -ne 0) { exit $Code }
        }
        finally {
            Remove-Item -LiteralPath $Installer -Force -ErrorAction SilentlyContinue
            $env:UV_UNMANAGED_INSTALL = $OldUnmanagedInstall
            $env:UV_NO_MODIFY_PATH = $OldNoModifyPath
        }
        $PotbotUv = $LocalUv
        if (-not (Test-PotbotUv $PotbotUv)) { throw 'The pinned uv installation could not be verified.' }
    }

    $env:POTBOT_PROJECT_ROOT = $PotbotRoot
    $env:UV_PROJECT_ENVIRONMENT = Join-Path $PotbotRoot '.venv'
    if (-not $env:UV_CACHE_DIR) { $env:UV_CACHE_DIR = Join-Path $PotbotRoot '.potbot\cache\uv' }
    if (-not $env:UV_PYTHON_INSTALL_DIR) { $env:UV_PYTHON_INSTALL_DIR = Join-Path $PotbotRoot '.potbot\runtime\python' }

    Write-PotbotProgress '2/4 Ensuring managed Python 3.12 (download progress follows when needed)'
    $Code = Invoke-PotbotProcess $PotbotUv @('python', 'install', '3.12', '--no-bin', '--no-registry')
    if ($Code -ne 0) { exit $Code }
    Write-PotbotProgress '3/4 Installing locked Python dependencies and all runtime extras'
    $Code = Invoke-PotbotProcess $PotbotUv @('sync', '--project', $PotbotRoot, '--locked', '--all-extras', '--no-dev', '--python', '3.12', '--managed-python')
    if ($Code -ne 0) { exit $Code }
    Write-PotbotProgress '4/4 Starting Potbot'
    $Code = Invoke-PotbotProcess $PotbotUv (@('run', '--project', $PotbotRoot, '--no-sync', 'python', '-m', 'potbot.startup') + $PotbotOriginalArguments)
    exit $Code
}
catch {
    Write-PotbotProgress 'Startup failed. Check connectivity, available disk space, and access to this project. On Linux/macOS use start.sh.'
    exit 1
}
