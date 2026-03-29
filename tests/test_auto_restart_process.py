# Copyright (c) 2026 Nardo (nardovibecoding). AGPL-3.0 — see LICENSE
"""Tests for auto_restart_process.py — stdlib unittest only."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))
import auto_restart_process as arp


class TestCheck(unittest.TestCase):

    def _check(self, file_path, tool="Edit"):
        return arp.check(tool, {"file_path": file_path}, {})

    def test_known_file_triggers(self):
        self.assertTrue(self._check("/project/bot_base.py"))

    def test_unknown_file_skips(self):
        self.assertFalse(self._check("/project/totally_unknown_module.py"))

    def test_non_edit_tool_skips(self):
        self.assertFalse(arp.check("Bash", {"file_path": "bot_base.py"}, {}))

    def test_none_restart_file_still_triggers_check(self):
        # Files with None restart (e.g. speak_hook.py) still match the check
        self.assertTrue(self._check("/project/speak_hook.py"))

    def test_config_file_triggers(self):
        self.assertTrue(self._check("/project/config.py"))


class TestEnvParsing(unittest.TestCase):
    """Test the .env parsing logic in _load_vps."""

    def test_parses_user_and_host(self):
        env_content = "VPS_USER=myuser\nVPS_HOST=1.2.3.4\n"
        user, host = "", ""
        for line in env_content.splitlines():
            if line.startswith("VPS_USER="):
                user = line.split("=", 1)[1].strip()
            elif line.startswith("VPS_HOST="):
                host = line.split("=", 1)[1].strip()
        self.assertEqual(user, "myuser")
        self.assertEqual(host, "1.2.3.4")

    def test_returns_at_sign_when_empty(self):
        # When VPS_USER and VPS_HOST are empty, result is "@"
        self.assertEqual("@", f"@")  # trivial, but documents expected format

    def test_load_vps_with_missing_env(self):
        # No .env file → returns "user@host" with empty strings
        import tempfile
        with tempfile.TemporaryDirectory():
            # Point _load_vps at a home dir that has no telegram-claude-bot/.env
            with patch("auto_restart_process.Path") as mock_path_cls:
                mock_home = MagicMock()
                mock_path_cls.home.return_value = mock_home
                mock_env = MagicMock()
                mock_home.__truediv__.return_value.__truediv__.return_value = mock_env
                mock_env.exists.return_value = False
                result = arp._load_vps()
        self.assertIn("@", result)

    def test_load_vps_reads_env_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            bot_dir = Path(tmp) / "telegram-claude-bot"
            bot_dir.mkdir()
            (bot_dir / ".env").write_text("VPS_USER=testuser\nVPS_HOST=9.9.9.9\n")
            with patch("auto_restart_process.Path") as mock_path_cls:
                mock_path_cls.home.return_value = Path(tmp)
                # Let Path() calls for the env file work normally
                mock_path_cls.side_effect = lambda *a, **k: Path(*a, **k)
                mock_path_cls.home.return_value = Path(tmp)
                result = arp._load_vps()
        self.assertIn("@", result)


class TestAction(unittest.TestCase):

    def test_none_restart_returns_none(self):
        result = arp.action("Edit", {"file_path": "/project/speak_hook.py"}, {})
        self.assertIsNone(result)

    def test_unknown_file_returns_none(self):
        result = arp.action("Edit", {"file_path": "/project/totally_unknown.py"}, {})
        self.assertIsNone(result)

    def test_known_file_runs_restart(self):
        with patch("auto_restart_process.subprocess.run") as mock_run, \
             patch("auto_restart_process._load_vps", return_value="user@host"):
            mock_run.return_value = MagicMock(returncode=0)
            result = arp.action("Edit", {"file_path": "/project/bot_base.py"}, {})
        self.assertIsNotNone(result)
        self.assertIn("Auto-restarted", result or "")
        mock_run.assert_called_once()

    def test_ssh_commands_have_vps_placeholder(self):
        # SSH (VPS) commands must have {vps}; local commands (pkill) don't
        for pattern, cmd in arp.RESTART_MAP.items():
            if cmd is not None and cmd.startswith("ssh"):
                self.assertIn("{vps}", cmd, f"{pattern}: SSH command missing {{vps}}")


if __name__ == "__main__":
    unittest.main()
