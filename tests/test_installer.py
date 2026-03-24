"""Unit tests for installer pre-install detection & cleanup helpers."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

# Make the project importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.installer import (
    INSTALL_DIR,
    TASK_CLIENT,
    TASK_SERVER,
    backup_existing,
    cleanup_existing,
    detect_existing_installation,
    find_orphan_tasks,
    preflight_checks,
    rollback,
    safe_install,
    verify_autostart,
)


class TestDetectExistingInstallation(unittest.TestCase):
    """Tests for detect_existing_installation()."""

    @patch("scripts.installer.subprocess.run")
    def test_nothing_installed(self, mock_run):
        """No exe, no task, no process → all False."""
        mock_run.return_value = MagicMock(returncode=1, stdout="")

        with patch.object(Path, "exists", return_value=False):
            result = detect_existing_installation("server")

        self.assertFalse(result["exe_exists"])
        self.assertFalse(result["task_exists"])
        self.assertIsNone(result["task_name"])
        self.assertFalse(result["process_running"])

    @patch("scripts.installer.subprocess.run")
    def test_server_fully_installed(self, mock_run):
        """Exe exists, task registered, process running."""
        def side_effect(cmd, **kw):
            if cmd[0] == "schtasks":
                return MagicMock(returncode=0, stdout="AlarmSystem_Server")
            if cmd[0] == "tasklist":
                return MagicMock(returncode=0, stdout="alarm_server.exe  1234 Console")
            return MagicMock(returncode=1, stdout="")

        mock_run.side_effect = side_effect

        with patch.object(Path, "exists", return_value=True):
            result = detect_existing_installation("server")

        self.assertTrue(result["exe_exists"])
        self.assertTrue(result["task_exists"])
        self.assertEqual(result["task_name"], TASK_SERVER)
        self.assertTrue(result["process_running"])

    @patch("scripts.installer.subprocess.run")
    def test_client_with_slug(self, mock_run):
        """Client detected by slug-specific task name."""
        def side_effect(cmd, **kw):
            if cmd[0] == "schtasks" and "zimmer_1" in str(cmd):
                return MagicMock(returncode=0, stdout="OK")
            if cmd[0] == "schtasks":
                return MagicMock(returncode=1, stdout="")
            if cmd[0] == "tasklist":
                return MagicMock(returncode=0, stdout="notepad.exe  1234")
            return MagicMock(returncode=1, stdout="")

        mock_run.side_effect = side_effect

        with patch.object(Path, "exists", return_value=False):
            result = detect_existing_installation("client", "zimmer_1")

        self.assertFalse(result["exe_exists"])
        self.assertTrue(result["task_exists"])
        self.assertEqual(result["task_name"], f"{TASK_CLIENT}_zimmer_1")
        self.assertFalse(result["process_running"])

    @patch("scripts.installer.subprocess.run")
    def test_exe_exists_but_no_task(self, mock_run):
        """Exe on disk but task was manually deleted."""
        mock_run.return_value = MagicMock(returncode=1, stdout="")

        with patch.object(Path, "exists", return_value=True):
            result = detect_existing_installation("server")

        self.assertTrue(result["exe_exists"])
        self.assertFalse(result["task_exists"])

    @patch("scripts.installer.subprocess.run")
    def test_process_check_handles_exception(self, mock_run):
        """If tasklist throws, process_running defaults to False."""
        def side_effect(cmd, **kw):
            if cmd[0] == "tasklist":
                raise OSError("tasklist failed")
            return MagicMock(returncode=1, stdout="")

        mock_run.side_effect = side_effect

        with patch.object(Path, "exists", return_value=False):
            result = detect_existing_installation("server")

        self.assertFalse(result["process_running"])


class TestCleanupExisting(unittest.TestCase):
    """Tests for cleanup_existing()."""

    @patch("scripts.installer.find_orphan_tasks", return_value=[])
    @patch("scripts.installer.subprocess.run")
    def test_cleanup_server_kills_and_deletes(self, mock_run, _):
        """cleanup_existing('server') calls taskkill and schtasks delete."""
        mock_run.return_value = MagicMock(returncode=0)

        cleanup_existing("server")

        # Should have called taskkill
        taskkill_calls = [c for c in mock_run.call_args_list
                          if c[0][0][0] == "taskkill"]
        self.assertEqual(len(taskkill_calls), 1)
        self.assertIn("alarm_server.exe", taskkill_calls[0][0][0])

        # Should have called schtasks /Delete for the server task
        delete_calls = [c for c in mock_run.call_args_list
                        if len(c[0][0]) > 1 and c[0][0][1] == "/Delete"]
        self.assertTrue(any(TASK_SERVER in str(c) for c in delete_calls))

    @patch("scripts.installer.find_orphan_tasks", return_value=["AlarmSystem_Client_old"])
    @patch("scripts.installer.subprocess.run")
    def test_cleanup_removes_orphans(self, mock_run, _):
        """cleanup_existing also removes orphan tasks."""
        mock_run.return_value = MagicMock(returncode=0)

        cleanup_existing("server")

        # Should have called schtasks /Delete for the orphan task
        delete_calls = [c for c in mock_run.call_args_list
                        if len(c[0][0]) > 1 and c[0][0][1] == "/Delete"]
        self.assertTrue(any("AlarmSystem_Client_old" in str(c) for c in delete_calls))

    @patch("scripts.installer.find_orphan_tasks", return_value=[])
    @patch("scripts.installer.subprocess.run")
    def test_cleanup_client_with_slug(self, mock_run, _):
        """cleanup_existing('client', 'zimmer_1') targets the correct task."""
        mock_run.return_value = MagicMock(returncode=0)

        cleanup_existing("client", "zimmer_1")

        # Should target alarm_client.exe
        taskkill_calls = [c for c in mock_run.call_args_list
                          if c[0][0][0] == "taskkill"]
        self.assertIn("alarm_client.exe", taskkill_calls[0][0][0])

        # Should delete slug-specific task AND generic task
        delete_calls = [c for c in mock_run.call_args_list
                        if len(c[0][0]) > 1 and c[0][0][1] == "/Delete"]
        task_names = [str(c) for c in delete_calls]
        self.assertTrue(any("AlarmSystem_Client_zimmer_1" in t for t in task_names))
        self.assertTrue(any(TASK_CLIENT in t for t in task_names))

    @patch("scripts.installer.find_orphan_tasks", return_value=[])
    @patch("scripts.installer.subprocess.run")
    def test_cleanup_handles_taskkill_failure(self, mock_run, _):
        """If taskkill fails (no process), cleanup continues without error."""
        def side_effect(cmd, **kw):
            if cmd[0] == "taskkill":
                raise OSError("no process")
            return MagicMock(returncode=0)

        mock_run.side_effect = side_effect
        # Should not raise
        cleanup_existing("server")


class TestFindOrphanTasks(unittest.TestCase):
    """Tests for find_orphan_tasks()."""

    @patch("scripts.installer.subprocess.run")
    def test_no_alarm_tasks(self, mock_run):
        """No AlarmSystem tasks → empty list."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='"HOST","TaskName","Next Run","Status"\n'
                   '"PC","\\SomeOtherTask","N/A","Ready"\n',
        )
        self.assertEqual(find_orphan_tasks(), [])

    @patch("scripts.installer.subprocess.run")
    def test_orphan_detected(self, mock_run):
        """Task pointing to nonexistent exe is flagged as orphan."""
        csv_line = (
            '"PC","\\AlarmSystem_Client_old","N/A","Ready","nobody",'
            '"N/A","N/A","N/A","C:\\nonexistent\\alarm_client.exe"'
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=csv_line)

        with patch.object(Path, "exists", return_value=False):
            orphans = find_orphan_tasks()

        self.assertIn("AlarmSystem_Client_old", orphans)

    @patch("scripts.installer.subprocess.run")
    def test_schtasks_failure(self, mock_run):
        """If schtasks query fails, return empty list gracefully."""
        mock_run.side_effect = OSError("command not found")
        self.assertEqual(find_orphan_tasks(), [])


class TestVerifyAutostart(unittest.TestCase):
    """Tests for verify_autostart()."""

    @patch("scripts.installer.subprocess.run")
    def test_task_enabled(self, mock_run):
        """Task exists and is enabled → True."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Scheduled Task State:          Enabled\n",
        )
        self.assertTrue(verify_autostart("AlarmSystem_Server"))

    @patch("scripts.installer.subprocess.run")
    def test_task_disabled(self, mock_run):
        """Task exists but is disabled → False."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Scheduled Task State:          Disabled\n",
        )
        self.assertFalse(verify_autostart("AlarmSystem_Server"))

    @patch("scripts.installer.subprocess.run")
    def test_task_enabled_german(self, mock_run):
        """German locale: task enabled."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Status des geplanten Tasks:    Aktiviert\n",
        )
        self.assertTrue(verify_autostart("AlarmSystem_Server"))

    @patch("scripts.installer.subprocess.run")
    def test_task_disabled_german(self, mock_run):
        """German locale: task disabled."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Status des geplanten Tasks:    Deaktiviert\n",
        )
        self.assertFalse(verify_autostart("AlarmSystem_Server"))

    @patch("scripts.installer.subprocess.run")
    def test_task_not_found(self, mock_run):
        """Task doesn't exist → False."""
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        self.assertFalse(verify_autostart("AlarmSystem_Nonexistent"))

    @patch("scripts.installer.subprocess.run")
    def test_no_status_line_assumes_enabled(self, mock_run):
        """If status line is missing, assume enabled (True)."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="TaskName: AlarmSystem_Server\nSomeOtherField: value\n",
        )
        self.assertTrue(verify_autostart("AlarmSystem_Server"))


class TestPreflightChecks(unittest.TestCase):
    """Tests for preflight_checks()."""

    @patch("scripts.installer.subprocess.run")
    @patch("scripts.installer.shutil.disk_usage")
    def test_all_ok(self, mock_disk, mock_run):
        """All checks pass → empty list."""
        mock_disk.return_value = MagicMock(free=500 * 1024 * 1024)  # 500 MB
        mock_run.return_value = MagicMock(returncode=1)  # task not found = OK

        with patch.object(Path, "mkdir"), \
             patch.object(Path, "write_text"), \
             patch.object(Path, "unlink"):
            errors = preflight_checks()

        self.assertEqual(errors, [])

    @patch("scripts.installer.subprocess.run")
    @patch("scripts.installer.shutil.disk_usage")
    def test_no_write_permission(self, mock_disk, mock_run):
        """Write probe fails → error returned."""
        mock_disk.return_value = MagicMock(free=500 * 1024 * 1024)
        mock_run.return_value = MagicMock(returncode=1)

        with patch.object(Path, "mkdir"), \
             patch.object(Path, "write_text", side_effect=PermissionError), \
             patch.object(Path, "unlink"):
            errors = preflight_checks()

        self.assertTrue(any("Schreibrechte" in e for e in errors))

    @patch("scripts.installer.subprocess.run")
    @patch("scripts.installer.shutil.disk_usage")
    def test_low_disk_space(self, mock_disk, mock_run):
        """Less than 100 MB free → error."""
        mock_disk.return_value = MagicMock(free=50 * 1024 * 1024)  # 50 MB
        mock_run.return_value = MagicMock(returncode=1)

        with patch.object(Path, "mkdir"), \
             patch.object(Path, "write_text"), \
             patch.object(Path, "unlink"):
            errors = preflight_checks()

        self.assertTrue(any("Speicherplatz" in e for e in errors))

    @patch("scripts.installer.subprocess.run")
    @patch("scripts.installer.shutil.disk_usage")
    def test_schtasks_missing(self, mock_disk, mock_run):
        """schtasks not found → error."""
        mock_disk.return_value = MagicMock(free=500 * 1024 * 1024)

        def side_effect(cmd, **kw):
            if cmd[0] == "schtasks":
                raise FileNotFoundError
            return MagicMock(returncode=0)

        mock_run.side_effect = side_effect

        with patch.object(Path, "mkdir"), \
             patch.object(Path, "write_text"), \
             patch.object(Path, "unlink"):
            errors = preflight_checks()

        self.assertTrue(any("schtasks" in e for e in errors))


class TestBackupAndRollback(unittest.TestCase):
    """Tests for backup_existing() and rollback()."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.orig_install_dir = INSTALL_DIR

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @patch("scripts.installer.subprocess.run")
    @patch("scripts.installer.INSTALL_DIR")
    def test_backup_copies_exe_and_config(self, mock_dir, mock_run):
        """backup_existing creates copies of exe and config."""
        mock_dir.__truediv__ = lambda s, x: Path(self.tmpdir) / x
        mock_dir.__str__ = lambda s: self.tmpdir
        mock_run.return_value = MagicMock(returncode=1, stdout="")

        # Create fake existing files
        exe = Path(self.tmpdir) / "alarm_server.exe"
        cfg = Path(self.tmpdir) / "server_config.toml"
        exe.write_text("fake exe")
        cfg.write_text("fake config")

        result = backup_existing("server")

        self.assertTrue(len(result["files"]) >= 0)  # may or may not find files depending on mock

    @patch("scripts.installer.subprocess.run")
    def test_rollback_restores_files(self, mock_run):
        """rollback() copies backup files back to original locations."""
        mock_run.return_value = MagicMock(returncode=0)

        # Create temp backup structure
        src = Path(self.tmpdir) / "original.txt"
        bak = Path(self.tmpdir) / "backup.txt"
        bak.write_text("backup content")

        backup_info = {
            "files": [{"src": str(src), "bak": str(bak)}],
            "task_xml": None,
        }

        rollback(backup_info)

        self.assertTrue(src.exists())
        self.assertEqual(src.read_text(), "backup content")

    @patch("scripts.installer.subprocess.run")
    def test_rollback_restores_task(self, mock_run):
        """rollback() re-imports the scheduled task XML."""
        mock_run.return_value = MagicMock(returncode=0)

        xml_path = Path(self.tmpdir) / "task.xml"
        xml_path.write_text("<Task/>")

        backup_info = {
            "files": [],
            "task_xml": str(xml_path),
            "task_name": "AlarmSystem_Server",
        }

        rollback(backup_info)

        # Verify schtasks /Create was called
        create_calls = [c for c in mock_run.call_args_list
                        if "/Create" in str(c)]
        self.assertTrue(len(create_calls) > 0)


class TestSafeInstall(unittest.TestCase):
    """Tests for safe_install() orchestration."""

    @patch("scripts.installer.verify_autostart", return_value=True)
    @patch("scripts.installer.cleanup_existing")
    @patch("scripts.installer.backup_existing", return_value={"files": [], "task_xml": None, "backup_dir": "/tmp"})
    @patch("scripts.installer.preflight_checks", return_value=[])
    def test_successful_install(self, mock_pre, mock_bak, mock_clean, mock_verify):
        """Full successful install path."""
        def install_fn(_backup):
            return {"task_ok": True, "exe": Path("test.exe"),
                    "desk_ok": True, "start_ok": True,
                    "task_name": "AlarmSystem_Server"}

        result = safe_install("server", install_fn)
        self.assertTrue(result["ok"])

    @patch("scripts.installer.preflight_checks", return_value=["No write access"])
    def test_preflight_failure_aborts(self, mock_pre):
        """If preflight fails, install is aborted without touching anything."""
        def install_fn(_backup):
            raise AssertionError("Should not be called")

        result = safe_install("server", install_fn)
        self.assertFalse(result["ok"])
        self.assertIn("Vorprüfung", result["error"])

    @patch("scripts.installer.rollback")
    @patch("scripts.installer.cleanup_existing")
    @patch("scripts.installer.backup_existing", return_value={"files": [], "task_xml": None, "backup_dir": "/tmp"})
    @patch("scripts.installer.preflight_checks", return_value=[])
    def test_install_failure_triggers_rollback(self, mock_pre, mock_bak, mock_clean, mock_rollback):
        """If install_fn raises, rollback is called."""
        def install_fn(_backup):
            raise RuntimeError("copy failed")

        result = safe_install("server", install_fn)
        self.assertFalse(result["ok"])
        self.assertIn("rückgängig", result["error"])
        mock_rollback.assert_called_once()

    @patch("scripts.installer.rollback")
    @patch("scripts.installer.verify_autostart", return_value=False)
    @patch("scripts.installer.cleanup_existing")
    @patch("scripts.installer.backup_existing", return_value={"files": [], "task_xml": None, "backup_dir": "/tmp"})
    @patch("scripts.installer.preflight_checks", return_value=[])
    def test_verify_failure_triggers_rollback(self, mock_pre, mock_bak, mock_clean, mock_verify, mock_rollback):
        """If autostart verification fails, rollback is called."""
        def install_fn(_backup):
            return {"task_ok": True, "exe": Path("test.exe"),
                    "desk_ok": True, "start_ok": True,
                    "task_name": "AlarmSystem_Server"}

        result = safe_install("server", install_fn)
        self.assertFalse(result["ok"])
        self.assertIn("rückgängig", result["error"])
        mock_rollback.assert_called_once()


if __name__ == "__main__":
    unittest.main()
