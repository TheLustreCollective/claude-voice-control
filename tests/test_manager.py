import tempfile
import unittest
from pathlib import Path

from claude_voice_control.cli import Manager, Session, _format_table, _tail_lines


class ManagerTests(unittest.TestCase):
    def test_state_paths_are_absolute(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = Manager(Path(directory) / "relative-state", "cctty")
            self.assertTrue(manager.registry_path.is_absolute())
            self.assertTrue(manager.logs_dir.is_absolute())

    def test_tail_lines_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "events.jsonl"
            log.write_text("".join(f"event-{number}\n" for number in range(500)))
            self.assertEqual(_tail_lines(log, limit=3), ["event-497", "event-498", "event-499"])

    def test_notes_and_metadata_survive_reload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = Manager(Path(directory) / "state", "cctty")
            # Exercise persistence without launching a real subprocess.
            record = manager.sessions()
            self.assertEqual(record, {})
            example = Session("demo", "session-1", "/tmp", "/tmp/demo.jsonl", "idle", notes="Investigate", metadata={"ticket": "TLU-222"})
            manager._save({"demo": example})
            reloaded = manager.sessions()["demo"]
            self.assertEqual(reloaded.notes, "Investigate")
            self.assertEqual(reloaded.metadata, {"ticket": "TLU-222"})
            updated = manager.update("demo", notes="Ready for review", merge_metadata={"owner": "Patrick"})
            self.assertEqual(updated.notes, "Ready for review")
            self.assertEqual(updated.metadata, {"ticket": "TLU-222", "owner": "Patrick"})

    def test_table_includes_session_context(self) -> None:
        table = _format_table([
            Session("demo", "session-1", "/work/demo", "/tmp/demo.jsonl", "idle", notes="Investigate", model="claude-opus-5"),
        ])
        self.assertIn("Last message", table)
        self.assertIn("claude-opus-5", table)
        self.assertIn("/work/demo", table)
        self.assertIn("Investigate", table)

    def test_bootstrap_prompt_is_private_durable_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = Manager(Path(directory) / "state", "cctty")
            manager.save_config({"bootstrap_prompt": "Read AGENTS.md first."})
            self.assertEqual(manager.config()["bootstrap_prompt"], "Read AGENTS.md first.")
            self.assertEqual(manager.config_path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
