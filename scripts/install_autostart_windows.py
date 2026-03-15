"""
install_autostart_windows.py — Register a Windows Task Scheduler job.

Run once with administrator privileges:
    python scripts/install_autostart_windows.py --target path/to/alarm_server.exe --role server
    python scripts/install_autostart_windows.py --target path/to/alarm_client.exe --role client

The task will:
  - Trigger on user logon AND at system startup
  - Run with highest privileges (required for global hotkey capture)
  - Restart automatically if the process exits unexpectedly
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import textwrap
from pathlib import Path

TASK_NAME_SERVER = "AlarmSystem_Server"
TASK_NAME_CLIENT = "AlarmSystem_Client"


def main() -> None:
    parser = argparse.ArgumentParser(description="Register Windows Task Scheduler auto-start")
    parser.add_argument("--target", required=True, help="Absolute path to the executable")
    parser.add_argument("--role", choices=["server", "client"], required=True)
    parser.add_argument("--uninstall", action="store_true", help="Remove the task instead")
    args = parser.parse_args()

    task_name = TASK_NAME_SERVER if args.role == "server" else TASK_NAME_CLIENT
    target = Path(args.target).resolve()

    if args.uninstall:
        _delete_task(task_name)
        return

    if not target.exists():
        print(f"ERROR: Target executable not found: {target}", file=sys.stderr)
        sys.exit(1)

    _create_task(task_name, target, args.role)


def _create_task(task_name: str, target: Path, role: str) -> None:
    """Create a scheduled task using schtasks.exe."""

    # Build the XML task definition for fine-grained control
    xml = _build_task_xml(target, role)
    xml_path = Path(os.environ.get("TEMP", "C:\\Temp")) / f"{task_name}.xml"
    xml_path.write_text(xml, encoding="utf-16")   # schtasks requires UTF-16

    try:
        # Delete existing task silently (ignore error if not found)
        subprocess.run(
            ["schtasks", "/Delete", "/TN", task_name, "/F"],
            capture_output=True,
        )

        result = subprocess.run(
            ["schtasks", "/Create", "/TN", task_name, "/XML", str(xml_path), "/F"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            print(f"Task '{task_name}' created successfully.")
        else:
            print(f"ERROR creating task:\n{result.stderr}", file=sys.stderr)
            sys.exit(1)
    finally:
        try:
            xml_path.unlink()
        except Exception:
            pass


def _delete_task(task_name: str) -> None:
    result = subprocess.run(
        ["schtasks", "/Delete", "/TN", task_name, "/F"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(f"Task '{task_name}' deleted.")
    else:
        print(f"Task not found or error: {result.stderr}", file=sys.stderr)


def _build_task_xml(target: Path, role: str) -> str:
    """Build a Windows Task Scheduler XML definition."""
    exe  = str(target)
    wdir = str(target.parent)
    desc = f"Alarm System {'Server' if role == 'server' else 'Client'} — auto-start"

    return textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-16"?>
        <Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
          <RegistrationInfo>
            <Description>{desc}</Description>
          </RegistrationInfo>
          <Triggers>
            <LogonTrigger>
              <Enabled>true</Enabled>
            </LogonTrigger>
            <BootTrigger>
              <Enabled>true</Enabled>
              <Delay>PT10S</Delay>
            </BootTrigger>
          </Triggers>
          <Principals>
            <Principal id="Author">
              <LogonType>InteractiveToken</LogonType>
              <RunLevel>HighestAvailable</RunLevel>
            </Principal>
          </Principals>
          <Settings>
            <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
            <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
            <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
            <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
            <RestartOnFailure>
              <Interval>PT1M</Interval>
              <Count>999</Count>
            </RestartOnFailure>
            <Enabled>true</Enabled>
          </Settings>
          <Actions Context="Author">
            <Exec>
              <Command>{exe}</Command>
              <WorkingDirectory>{wdir}</WorkingDirectory>
            </Exec>
          </Actions>
        </Task>
    """)


if __name__ == "__main__":
    main()
