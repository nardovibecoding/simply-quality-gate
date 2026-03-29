# Copyright (c) 2026 Nardo (nardovibecoding). AGPL-3.0 — see LICENSE
"""Tests for auto_memory_index.py — stdlib unittest only."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))
import auto_memory_index as ami


class TestCheck(unittest.TestCase):

    def _check(self, tool, file_path):
        return ami.check(tool, {"file_path": file_path}, {})

    def test_write_to_memory_md_triggers(self):
        self.assertTrue(self._check("Write", "/some/memory/new_file.md"))

    def test_memory_index_itself_skipped(self):
        self.assertFalse(self._check("Write", "/some/memory/MEMORY.md"))

    def test_non_md_skipped(self):
        self.assertFalse(self._check("Write", "/some/memory/file.py"))

    def test_edit_tool_skipped(self):
        self.assertFalse(self._check("Edit", "/some/memory/new_file.md"))

    def test_no_memory_in_path_skipped(self):
        self.assertFalse(self._check("Write", "/some/other/new_file.md"))


class TestAction(unittest.TestCase):

    def test_returns_none_when_already_indexed(self):
        with patch("auto_memory_index.INDEX") as mock_index:
            mock_index.exists.return_value = True
            mock_index.read_text.return_value = "- [my_file.md](my_file.md) — desc"
            result = ami.action("Write", {"file_path": "/memory/my_file.md"}, {})
        self.assertIsNone(result)

    def test_returns_warning_when_not_indexed(self):
        with patch("auto_memory_index.INDEX") as mock_index:
            mock_index.exists.return_value = True
            mock_index.read_text.return_value = "- [other.md](other.md) — desc"
            result = ami.action("Write", {"file_path": "/memory/new_note.md"}, {})
        self.assertIsNotNone(result)
        self.assertIn("MEMORY.md", result or "")

    def test_returns_none_when_index_missing(self):
        with patch("auto_memory_index.INDEX") as mock_index:
            mock_index.exists.return_value = False
            result = ami.action("Write", {"file_path": "/memory/new_note.md"}, {})
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
