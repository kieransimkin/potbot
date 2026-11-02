[CmdletBinding()]
param(
    [Parameter()]
    [string] $Python,
    [Parameter()]
    [switch] $FullSuite,
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]] $PytestArgs = @()
)

$ErrorActionPreference = "Stop"
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$projectName = [IO.Path]::GetFileName($repoRoot.TrimEnd([IO.Path]::DirectorySeparatorChar))
$projectPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not $Python) {
    $Python = if (Test-Path -LiteralPath $projectPython) { $projectPython } else { "python" }
}
if (-not $FullSuite -and $PytestArgs.Count -eq 0) {
    throw "Select one or more focused pytest paths, or pass -FullSuite explicitly for release/environment diagnosis."
}

$onWindows = [Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT
$tempParent = if ($onWindows -and $env:LOCALAPPDATA) {
    Join-Path $env:LOCALAPPDATA "$projectName\pytest-tmp"
} else {
    Join-Path $repoRoot ".test-tmp"
}
$runName = "pytest-{0}-{1}" -f (Get-Date -Format "yyyyMMdd-HHmmss"), ([guid]::NewGuid().ToString("N"))
$runRoot = [IO.Path]::GetFullPath((Join-Path $tempParent $runName))
$parentRoot = [IO.Path]::GetFullPath($tempParent)
if (-not $runRoot.StartsWith($parentRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Unsafe pytest temp path: $runRoot"
}

$pytestTemp = Join-Path $runRoot "pytest"
$processTemp = Join-Path $runRoot "process"
$pytestCache = Join-Path $runRoot "cache"
$projectSrc = Join-Path $repoRoot "src"
$previous = @{
    TEMP = $env:TEMP
    TMP = $env:TMP
    TMPDIR = $env:TMPDIR
    PYTHONPATH = $env:PYTHONPATH
}
$exitCode = 1

try {
    New-Item -ItemType Directory -Path $tempParent -Force | Out-Null
    $probe = Join-Path $tempParent ("probe-" + [guid]::NewGuid().ToString("N"))
    $renamedProbe = "$probe-renamed"
    try {
        New-Item -ItemType Directory -Path $probe | Out-Null
        $probeFile = Join-Path $probe "probe.txt"
        [IO.File]::WriteAllText($probeFile, "pytest-temp-probe")
        if ([IO.File]::ReadAllText($probeFile) -ne "pytest-temp-probe") {
            throw "Pytest temp probe readback failed: $probe"
        }
        Move-Item -LiteralPath $probe -Destination $renamedProbe
    }
    finally {
        if (Test-Path -LiteralPath $renamedProbe) {
            Remove-Item -LiteralPath $renamedProbe -Recurse -Force
        }
        if (Test-Path -LiteralPath $probe) {
            Remove-Item -LiteralPath $probe -Recurse -Force
        }
    }

    New-Item -ItemType Directory -Path $processTemp -Force | Out-Null
    $env:TEMP = $processTemp
    $env:TMP = $processTemp
    $env:TMPDIR = $processTemp
    if (Test-Path -LiteralPath $projectSrc) {
        $env:PYTHONPATH = if ($previous.PYTHONPATH) {
            "$projectSrc$([IO.Path]::PathSeparator)$($previous.PYTHONPATH)"
        } else {
            $projectSrc
        }
    }

    Write-Host "pytest temp probe passed: $tempParent"
    Write-Host "pytest process temp: $processTemp"
    Write-Host "pytest basetemp: $pytestTemp"
    Write-Host "pytest cache: $pytestCache"
    Write-Host "pytest interpreter: $Python"
    Push-Location -LiteralPath $repoRoot
    try {
        & $Python -m pytest @PytestArgs "--basetemp=$pytestTemp" "-o" "cache_dir=$pytestCache"
        $exitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
}
finally {
    $env:TEMP = $previous.TEMP
    $env:TMP = $previous.TMP
    $env:TMPDIR = $previous.TMPDIR
    $env:PYTHONPATH = $previous.PYTHONPATH
    if (Test-Path -LiteralPath $runRoot) {
        Remove-Item -LiteralPath $runRoot -Recurse -Force
    }
    if (Test-Path -LiteralPath $runRoot) {
        throw "Pytest temp cleanup failed: $runRoot"
    }
    Write-Host "pytest temp cleanup complete: $runRoot"
}

exit $exitCode
