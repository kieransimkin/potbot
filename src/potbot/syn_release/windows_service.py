"""User-owned Windows lifetime task for the native WSL operator service."""

from __future__ import annotations

import json
import re
from pathlib import Path

from potbot.domain.errors import StateMismatchError

from .host import WslOperatorHost, run_host

INSTALL = r"""
param([Parameter(Mandatory=$true)][string]$WslExecutable,
      [Parameter(Mandatory=$true)][string]$InstallationId,
      [Parameter(Mandatory=$true)][string]$WaitScript)
$ErrorActionPreference = 'Stop'
if ($InstallationId -notmatch '^[0-9a-f]{32}$') { throw 'Invalid managed host identity' }
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$taskName = 'Potbot-SynOperator'
$powershell = Join-Path $PSHOME 'powershell.exe'
$arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -File "' + $WaitScript + '" -WslExecutable "' + $WslExecutable + '"'
$description = 'Potbot managed Syn operator; host ' + $InstallationId + '; no automatic restart after faults.'
$existing = Get-ScheduledTask -TaskName $taskName -TaskPath '\' -ErrorAction SilentlyContinue
if ($existing) {
    if ($existing.Description -cne $description -or @($existing.Actions).Count -ne 1 -or
        $existing.Actions[0].Execute -cne $powershell -or $existing.Actions[0].Arguments -cne $arguments -or
        $existing.Principal.LogonType -ne 'Interactive' -or $existing.Principal.RunLevel -ne 'Limited' -or
        $existing.Settings.ExecutionTimeLimit -ne 'PT0S' -or $existing.Settings.RestartCount -ne 0) {
        throw 'Existing task differs from this managed host'
    }
    $principalId = $existing.Principal.UserId
    if ($principalId -notmatch '^S-1-') {
        $principalId = ([Security.Principal.NTAccount]$principalId).Translate([Security.Principal.SecurityIdentifier]).Value
    }
    if ($principalId -cne $sid) { throw 'Existing task belongs to another principal' }
} else {
    $action = New-ScheduledTaskAction -Execute $powershell -Argument $arguments
    $principal = New-ScheduledTaskPrincipal -UserId $sid -LogonType Interactive -RunLevel Limited
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $sid
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $taskName -TaskPath '\' -Action $action -Principal $principal -Trigger $trigger -Settings $settings -Description $description | Out-Null
}
Start-ScheduledTask -TaskName $taskName -TaskPath '\'
@{profile='potbot.syn-wsl-lifetime/1';task=$taskName;state='start_requested';automatic_fault_restart=$false} | ConvertTo-Json -Compress
"""

WAIT = r"""
param([Parameter(Mandatory=$true)][string]$WslExecutable)
$ErrorActionPreference = 'Stop'
$arguments = @('--distribution','Potbot-SynOperator','--user','synoperator','--exec',
               '/usr/bin/systemctl','--user','--wait','start','potbot-syn-operator.service')
$process = Start-Process -FilePath $WslExecutable -ArgumentList $arguments -WindowStyle Hidden -PassThru -Wait
exit $process.ExitCode
"""


def install_windows_service(manager: WslOperatorHost) -> dict[str, object]:
    record = json.loads(manager.receipt.read_text())
    identity = record.get("installation_id", "")
    if not re.fullmatch(r"[0-9a-f]{32}", identity):
        raise StateMismatchError("Managed WSL host lacks its retained identity")
    script = manager.root / "operator-user-task.ps1"
    wait_script = manager.root / "operator-lifetime.ps1"
    for target, source in ((script, INSTALL), (wait_script, WAIT)):
        raw = source.encode("utf-8")
        if target.exists():
            if target.is_symlink() or target.read_bytes() != raw:
                raise StateMismatchError("Existing Windows lifetime helper differs")
        else:
            with target.open("xb") as out:
                out.write(raw)
    powershell = str(Path(manager.wsl).parent / "WindowsPowerShell/v1.0/powershell.exe")
    result = json.loads(
        run_host(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(script),
                "-WslExecutable",
                manager.wsl,
                "-InstallationId",
                identity,
                "-WaitScript",
                str(wait_script),
            ]
        )
    )
    if (
        result.get("profile") != "potbot.syn-wsl-lifetime/1"
        or result.get("task") != "Potbot-SynOperator"
    ):
        raise StateMismatchError("Windows lifetime task did not return its exact receipt")
    return dict(result)
