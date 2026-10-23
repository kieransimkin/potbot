# Native Windows qualification without installing Python, uv, or model weights.
$ErrorActionPreference = 'Stop'
$ProjectSource = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$TemporaryRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('potbot launcher smoke ' + [guid]::NewGuid().ToString('N'))
$Project = Join-Path $TemporaryRoot 'project with spaces'
$Log = Join-Path $TemporaryRoot 'calls.log'
$Shell = Join-Path $PSHOME 'powershell.exe'
if (-not (Test-Path -LiteralPath $Shell)) { $Shell = Join-Path $PSHOME 'pwsh.exe' }

function Assert-True([bool] $Condition, [string] $Message) {
    if (-not $Condition) { throw $Message }
}

function Quote-Native([AllowEmptyString()][string] $Value) {
    $Quoted = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    return '"' + [regex]::Replace($Quoted, '(\\+)$', '$1$1') + '"'
}

function Invoke-Launcher([string] $Executable, [string] $NativeArguments) {
    $Info = New-Object System.Diagnostics.ProcessStartInfo
    $Info.FileName = $Executable
    $Info.Arguments = $NativeArguments
    $Info.UseShellExecute = $false
    $Info.RedirectStandardInput = $true
    $Info.RedirectStandardOutput = $true
    $Info.RedirectStandardError = $true
    $Process = [System.Diagnostics.Process]::Start($Info)
    try {
        $Process.StandardInput.WriteLine('credential prompt input')
        $Process.StandardInput.Close()
        $Stdout = $Process.StandardOutput.ReadToEnd()
        $Stderr = $Process.StandardError.ReadToEnd()
        $Process.WaitForExit()
        return @{ Code = $Process.ExitCode; Out = $Stdout; Err = $Stderr }
    }
    finally { $Process.Dispose() }
}

function Get-Records([string] $Prefix) {
    $Records = @()
    foreach ($Line in Get-Content -LiteralPath $Log) {
        if ($Line.StartsWith($Prefix + ' ')) {
            $Decoded = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Line.Substring(2)))
            $Records += ,($Decoded.Split([char]0))
        }
    }
    return ,$Records
}

$EnvironmentNames = @('POTBOT_LAUNCH_LOG', 'POTBOT_APPLICATION_CODE', 'POTBOT_FAIL_STAGE', 'POTBOT_FAIL_CODE', 'UV_CACHE_DIR', 'UV_PYTHON_INSTALL_DIR')
$SavedEnvironment = @{}
foreach ($Name in $EnvironmentNames) { $SavedEnvironment[$Name] = [Environment]::GetEnvironmentVariable($Name) }
try {
    New-Item -ItemType Directory -Path (Join-Path $Project '.potbot\runtime\uv') -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $ProjectSource 'start.ps1') -Destination $Project
    Copy-Item -LiteralPath (Join-Path $ProjectSource 'start.bat') -Destination $Project
    $SynClientDirectory = Join-Path $Project 'vendor\syn\packages\syn-client'
    New-Item -ItemType Directory -Path $SynClientDirectory -Force | Out-Null
    [IO.File]::WriteAllText((Join-Path $SynClientDirectory 'pyproject.toml'), '# Complete-distribution fixture')
    $FakeUv = Join-Path $Project '.potbot\runtime\uv\uv.exe'
    Add-Type -OutputAssembly $FakeUv -OutputType ConsoleApplication -TypeDefinition @'
using System;
using System.IO;
using System.Text;
public static class FakeUv {
    static void Record(string prefix, string[] values) {
        string encoded = Convert.ToBase64String(Encoding.UTF8.GetBytes(string.Join("\0", values)));
        File.AppendAllText(Environment.GetEnvironmentVariable("POTBOT_LAUNCH_LOG"), prefix + " " + encoded + "\n");
    }
    public static int Main(string[] args) {
        if (args.Length == 1 && args[0] == "--version") {
            Console.WriteLine("uv 0.12.23 (Windows smoke fixture)"); return 0;
        }
        Record("A", args);
        if (args[0] == Environment.GetEnvironmentVariable("POTBOT_FAIL_STAGE"))
            return int.Parse(Environment.GetEnvironmentVariable("POTBOT_FAIL_CODE"));
        if (args[0] == "run") {
            Record("I", new string[] { Console.In.ReadToEnd() });
            Record("E", new string[] {
                Environment.GetEnvironmentVariable("POTBOT_PROJECT_ROOT"),
                Environment.GetEnvironmentVariable("UV_PROJECT_ENVIRONMENT"),
                Environment.GetEnvironmentVariable("UV_CACHE_DIR"),
                Environment.GetEnvironmentVariable("UV_PYTHON_INSTALL_DIR") });
            Console.WriteLine("shared startup reached");
            return int.Parse(Environment.GetEnvironmentVariable("POTBOT_APPLICATION_CODE"));
        }
        return 0;
    }
}
'@
    $env:POTBOT_LAUNCH_LOG = $Log
    $env:POTBOT_APPLICATION_CODE = '23'
    $env:POTBOT_FAIL_STAGE = $null
    $env:UV_CACHE_DIR = $null
    $env:UV_PYTHON_INSTALL_DIR = $null
    $Original = @('dependencies', 'status', 'two words', '', 'literal"quote', 'C:\model path\', 'literal & value')
    $LauncherArguments = @('-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $Project 'start.ps1')) + $Original
    $Result = Invoke-Launcher $Shell (($LauncherArguments | ForEach-Object { Quote-Native $_ }) -join ' ')
    Assert-True ($Result.Code -eq 23) ('PowerShell exit code was not preserved: ' + $Result.Err)
    $Calls = Get-Records 'A'
    Assert-True ($Calls.Count -eq 3) 'Expected Python install, locked sync, then application startup.'
    Assert-True (($Calls[0] -join '|') -eq 'python|install|3.12|--no-bin|--no-registry') 'Managed Python was not installed.'
    $ExpectedSync = @('sync', '--project', $Project, '--locked', '--all-extras', '--no-dev', '--python', '3.12', '--managed-python')
    Assert-True (($Calls[1] -join [char]0) -ceq ($ExpectedSync -join [char]0)) 'Expected locked all-extras/no-dev synchronization.'
    $ExpectedRun = @('run', '--project', $Project, '--no-sync', 'python', '-m', 'potbot.startup') + $Original
    Assert-True (($Calls[2] -join [char]0) -ceq ($ExpectedRun -join [char]0)) 'PowerShell failed to preserve arguments.'
    $InputRecords = Get-Records 'I'
    Assert-True ($InputRecords[0][0].Trim() -eq 'credential prompt input') 'PowerShell failed to preserve stdin.'
    $EnvironmentRecords = Get-Records 'E'
    $ExpectedEnvironment = @($Project, (Join-Path $Project '.venv'), (Join-Path $Project '.potbot\cache\uv'), (Join-Path $Project '.potbot\runtime\python'))
    Assert-True (($EnvironmentRecords[0] -join [char]0) -ceq ($ExpectedEnvironment -join [char]0)) 'Runtime paths were not project-local.'

    Remove-Item -LiteralPath $Log
    $env:POTBOT_FAIL_STAGE = 'sync'
    $env:POTBOT_FAIL_CODE = '31'
    $Result = Invoke-Launcher $Shell (($LauncherArguments | ForEach-Object { Quote-Native $_ }) -join ' ')
    Assert-True ($Result.Code -eq 31) 'Synchronization error code was not preserved.'
    Assert-True ((Get-Records 'A').Count -eq 2) 'Application started after failed synchronization.'

    Remove-Item -LiteralPath $Log
    $env:POTBOT_FAIL_STAGE = $null
    $BatchCommand = '/d /s /c ""' + (Join-Path $Project 'start.bat') + '" dependencies status "two words" "literal & value""'
    $Result = Invoke-Launcher $env:ComSpec $BatchCommand
    Assert-True ($Result.Code -eq 23) ('BAT exit code was not preserved: ' + $Result.Err)
    $Calls = Get-Records 'A'
    $ExpectedBatch = @('run', '--project', $Project, '--no-sync', 'python', '-m', 'potbot.startup', 'dependencies', 'status', 'two words', 'literal & value')
    Assert-True (($Calls[2] -join [char]0) -ceq ($ExpectedBatch -join [char]0)) 'BAT failed to preserve quoted arguments.'
    Assert-True ((Get-Records 'I')[0][0].Trim() -eq 'credential prompt input') 'BAT failed to preserve stdin.'
    Remove-Item -LiteralPath (Join-Path $SynClientDirectory 'pyproject.toml')
    Remove-Item -LiteralPath $Log
    $Result = Invoke-Launcher $Shell (($LauncherArguments | ForEach-Object { Quote-Native $_ }) -join ' ')
    Assert-True ($Result.Code -eq 1 -and $Result.Err.Contains('Syn submodule is missing')) 'Missing Syn source did not fail clearly.'
    Assert-True (-not (Test-Path -LiteralPath $Log)) 'Runtime installation started without Syn source.'
    Write-Output 'Windows PowerShell/BAT launcher smoke passed: paths, arguments, stdin, dependencies, missing submodule and exit codes.'
}
finally {
    foreach ($Name in $EnvironmentNames) { [Environment]::SetEnvironmentVariable($Name, $SavedEnvironment[$Name]) }
    $ResolvedTemporaryRoot = [IO.Path]::GetFullPath($TemporaryRoot)
    $ExpectedTemporaryParent = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    if (-not $ResolvedTemporaryRoot.StartsWith($ExpectedTemporaryParent, [StringComparison]::OrdinalIgnoreCase) -or
        -not ([IO.Path]::GetFileName($ResolvedTemporaryRoot)).StartsWith('potbot launcher smoke ')) {
        throw 'Refusing cleanup outside the owned launcher smoke directory.'
    }
    Remove-Item -LiteralPath $ResolvedTemporaryRoot -Recurse -Force -ErrorAction SilentlyContinue
}
