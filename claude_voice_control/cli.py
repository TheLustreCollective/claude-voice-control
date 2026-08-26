from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


DEFAULT_STATE_DIR = Path.home() / ".local" / "share" / "claude-voice-control"


@dataclass
class Session:
    name: str
    session_id: str
    cwd: str
    log_path: str
    state: str
    pid: int | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    last_error: str | None = None


class Manager:
    def __init__(self, state_dir: Path, cctty: str) -> None:
        self.state_dir = state_dir.expanduser().resolve()
        self.cctty = cctty
        self.registry_path = state_dir / "sessions.json"
        self.logs_dir = state_dir / "logs"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict[str, Session]:
        if not self.registry_path.exists():
            return {}
        raw = json.loads(self.registry_path.read_text())
        return {name: Session(**value) for name, value in raw.items()}

    def _save(self, sessions: dict[str, Session]) -> None:
        pending = self.registry_path.with_suffix(".tmp")
        pending.write_text(json.dumps({name: asdict(value) for name, value in sessions.items()}, indent=2) + "\n")
        pending.replace(self.registry_path)
        self.registry_path.chmod(0o600)

    @staticmethod
    def _pid_alive(pid: int | None) -> bool:
        if not pid:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _refresh(self, session: Session) -> Session:
        if session.state == "running" and not self._pid_alive(session.pid):
            result = _last_result(Path(session.log_path))
            if result and not result.get("is_error", False):
                session.state = "idle"
            else:
                session.state = "failed"
                session.last_error = (result or {}).get("result", "cctty exited without a result")
            session.pid = None
            session.updated_at = time.time()
        return session

    def sessions(self) -> dict[str, Session]:
        sessions = self._load()
        changed = False
        for name, session in sessions.items():
            before = (session.state, session.pid)
            self._refresh(session)
            changed |= before != (session.state, session.pid)
        if changed:
            self._save(sessions)
        return sessions

    def run_turn(self, name: str, prompt: str, cwd: Path | None = None) -> Session:
        sessions = self.sessions()
        session = sessions.get(name)
        if session and session.state == "running":
            raise ValueError(f"session '{name}' already has a running turn")
        if session is None:
            if cwd is None:
                cwd = Path.cwd()
            cwd = cwd.resolve()
            session = Session(
                name=name,
                session_id=str(uuid.uuid4()),
                cwd=str(cwd),
                log_path=str(self.logs_dir / f"{name}.jsonl"),
                state="idle",
                created_at=time.time(),
                updated_at=time.time(),
            )
        elif cwd is not None and cwd.resolve() != Path(session.cwd):
            raise ValueError("a resumed session must use its original working directory")

        log = Path(session.log_path)
        command = [
            self.cctty,
            "--print",
            "--output-format",
            "stream-json",
            "--no-chrome",
            "--session-id",
            session.session_id,
            prompt,
        ]
        with log.open("ab") as output:
            process = subprocess.Popen(
                command,
                cwd=session.cwd,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        session.pid = process.pid
        session.state = "running"
        session.last_error = None
        session.updated_at = time.time()
        sessions[name] = session
        self._save(sessions)
        return session

    def stop(self, name: str) -> Session:
        sessions = self.sessions()
        session = sessions.get(name)
        if not session:
            raise ValueError(f"unknown session '{name}'")
        if session.state == "running" and session.pid:
            os.killpg(session.pid, signal.SIGTERM)
        session.pid = None
        session.state = "stopped"
        session.updated_at = time.time()
        sessions[name] = session
        self._save(sessions)
        return session


def _last_result(log_path: Path) -> dict[str, Any] | None:
    if not log_path.exists():
        return None
    for line in reversed(_tail_lines(log_path)):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "result":
            return event
    return None


def _tail_summary(log_path: Path) -> str | None:
    if not log_path.exists():
        return None
    for line in reversed(_tail_lines(log_path)):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = event.get("message")
        if event.get("type") == "assistant" and isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, list):
                text = "".join(block.get("text", "") for block in content if isinstance(block, dict))
                if text:
                    return text[-800:]
        if event.get("type") == "result" and isinstance(event.get("result"), str):
            return event["result"][-800:]
    return None


def _tail_lines(path: Path, limit: int = 200, max_bytes: int = 256 * 1024) -> list[str]:
    """Read a bounded tail so status stays cheap for long-running sessions."""
    with path.open("rb") as handle:
        size = handle.seek(0, os.SEEK_END)
        handle.seek(max(0, size - max_bytes))
        chunk = handle.read().decode(errors="replace")
    return chunk.splitlines()[-limit:]


def _print_session(session: Session, include_summary: bool = False) -> None:
    data = asdict(session)
    if include_summary:
        data["summary"] = _tail_summary(Path(session.log_path))
    print(json.dumps(data, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--cctty", default=os.environ.get("CVC_CCTTY", "cctty"))
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start", help="start a named session in the background")
    start.add_argument("name")
    start.add_argument("--cwd", type=Path, required=True)
    start.add_argument("--prompt", required=True)
    send = sub.add_parser("send", help="send a follow-up prompt to an idle session")
    send.add_argument("name")
    send.add_argument("--prompt", required=True)
    sub.add_parser("list", help="list all named sessions")
    status = sub.add_parser("status", help="show compact recent status")
    status.add_argument("name")
    stop = sub.add_parser("stop", help="stop the active turn")
    stop.add_argument("name")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manager = Manager(args.state_dir, args.cctty)
    try:
        if args.command == "start":
            _print_session(manager.run_turn(args.name, args.prompt, args.cwd))
        elif args.command == "send":
            _print_session(manager.run_turn(args.name, args.prompt))
        elif args.command == "list":
            print(json.dumps([asdict(session) for session in manager.sessions().values()], indent=2))
        elif args.command == "status":
            session = manager.sessions().get(args.name)
            if not session:
                raise ValueError(f"unknown session '{args.name}'")
            _print_session(session, include_summary=True)
        elif args.command == "stop":
            _print_session(manager.stop(args.name))
    except (OSError, ValueError) as exc:
        print(f"claude-voice-control: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
