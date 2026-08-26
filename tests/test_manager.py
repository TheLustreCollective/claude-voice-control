import tempfile
import unittest
from pathlib import Path

from claude_voice_control.cli import Manager, _tail_lines


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


if __name__ == "__main__":
    unittest.main()
