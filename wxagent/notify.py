"""
Windows desktop notification for the daily headline.

Dependency-free: shells out to PowerShell and uses the WinRT toast API, with a
tray-balloon fallback for hosts where the toast AppID is unavailable. If both
fail the headline still reaches stdout and the bulletin file, so a broken
notifier never costs you the forecast.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from . import config as C

# A registered AUMID is required to raise a toast. PowerShell's own shortcut id
# is present on a default Windows 10/11 install.
_AUMID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

_PS_TEMPLATE = r"""
$ErrorActionPreference = 'Stop'
$title = @'
{title}
'@
$body = @'
{body}
'@
$launch = @'
{launch}
'@

function Show-Toast {{
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
    [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType=WindowsRuntime] | Out-Null

    $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
    $escTitle  = [System.Security.SecurityElement]::Escape($title)
    $escBody   = [System.Security.SecurityElement]::Escape($body)
    $escLaunch = [System.Security.SecurityElement]::Escape($launch)
    $payload = @"
<toast launch="$escLaunch" activationType="protocol">
  <visual>
    <binding template="ToastGeneric">
      <text>$escTitle</text>
      <text>$escBody</text>
    </binding>
  </visual>
</toast>
"@
    $xml.LoadXml($payload)
    $toast = New-Object Windows.UI.Notifications.ToastNotification $xml
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{aumid}').Show($toast)
}}

function Show-Balloon {{
    Add-Type -AssemblyName System.Windows.Forms
    $icon = New-Object System.Windows.Forms.NotifyIcon
    $icon.Icon = [System.Drawing.SystemIcons]::Information
    $icon.BalloonTipTitle = $title
    $icon.BalloonTipText = $body
    $icon.Visible = $true
    $icon.ShowBalloonTip(20000)
    Start-Sleep -Seconds 12
    $icon.Dispose()
}}

try {{ Show-Toast }} catch {{ try {{ Show-Balloon }} catch {{ Write-Output "notify-failed: $_" }} }}
"""


def notify(title: str, body: str, launch: str = "") -> bool:
    """
    Raise a desktop notification. Returns True if PowerShell exited cleanly.

    `launch` may be a file:// URI so clicking the toast opens the bulletin.
    """
    if not C.NOTIFY_ENABLED:
        return False

    # Toast bodies get truncated by the shell anyway; keep it to the headline.
    body = body.strip()
    if len(body) > 250:
        body = body[:247].rstrip() + "..."

    script = _PS_TEMPLATE.format(
        title=title.replace("'@", "'"),
        body=body.replace("'@", "'"),
        launch=launch.replace("'@", "'"),
        aumid=_AUMID,
    )

    tmp = Path(tempfile.gettempdir()) / "wxagent_notify.ps1"
    tmp.write_text(script, encoding="utf-8")
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-File", str(tmp)],
            capture_output=True, text=True, timeout=60,
        )
        if "notify-failed" in (result.stdout or ""):
            print(f"  ! notification failed: {result.stdout.strip()}")
            return False
        return result.returncode == 0
    except Exception as exc:                      # noqa: BLE001 - never fatal
        print(f"  ! notification failed: {exc}")
        return False
    finally:
        tmp.unlink(missing_ok=True)
