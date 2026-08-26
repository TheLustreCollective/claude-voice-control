from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
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
    notes: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    pid: int | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    last_message_at: float | None = None
    turn_started_at: float | None = None
    active_seconds: float = 0.0
    model: str | None = None
    last_error: str | None = None


class Manager:
    def __init__(self, state_dir: Path, cctty: str) -> None:
        self.state_dir = state_dir.expanduser().resolve()
        self.cctty = cctty
        self.registry_path = self.state_dir / "sessions.json"
        self.logs_dir = self.state_dir / "logs"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict[str, Session]:
        if not self.registry_path.exists():
            return {}
        raw = json.loads(self.registry_path.read_text())
        sessions = {}
        for name, value in raw.items():
            value.setdefault("notes", "")
            value.setdefault("metadata", {})
            value.setdefault("last_message_at", None)
            value.setdefault("turn_started_at", None)
            value.setdefault("active_seconds", 0.0)
            value.setdefault("model", None)
            sessions[name] = Session(**value)
        return sessions

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
        self._refresh_observed_fields(session)
        if session.state == "running" and not self._pid_alive(session.pid):
            result = _last_result(Path(session.log_path))
            if result and not result.get("is_error", False):
                session.state = "idle"
            else:
                session.state = "failed"
                session.last_error = (result or {}).get("result", "cctty exited without a result")
            session.pid = None
            now = time.time()
            if session.turn_started_at:
                session.active_seconds += now - session.turn_started_at
            session.turn_started_at = None
            session.updated_at = now
        return session

    @staticmethod
    def _refresh_observed_fields(session: Session) -> None:
        observed = _recent_observed_fields(Path(session.log_path))
        if observed["last_message_at"]:
            session.last_message_at = observed["last_message_at"]
        if observed["model"]:
            session.model = observed["model"]

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

    def run_turn(
        self,
        name: str,
        prompt: str,
        cwd: Path | None = None,
        notes: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Session:
        sessions = self.sessions()
        session = sessions.get(name)
        if session and session.state == "running":
            raise ValueError(f"session '{name}' already has a running turn")
        if session is None:
            _validate_name(name)
            if cwd is None:
                cwd = Path.cwd()
            cwd = cwd.resolve()
            session = Session(
                name=name,
                session_id=str(uuid.uuid4()),
                cwd=str(cwd),
                log_path=str(self.logs_dir / f"{name}.jsonl"),
                state="idle",
                notes=notes or "",
                metadata=metadata or {},
                created_at=time.time(),
                updated_at=time.time(),
            )
        elif cwd is not None and cwd.resolve() != Path(session.cwd):
            raise ValueError("a resumed session must use its original working directory")

        log = Path(session.log_path)
        log.parent.mkdir(parents=True, exist_ok=True)
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
        session.metadata = {
            **(session.metadata or {}),
            "startup_command": [*command[:-1], "<prompt>"],
        }
        with log.open("ab") as output:
            _write_manager_event(output, "turn_starting", session)
            process = subprocess.Popen(
                command,
                cwd=session.cwd,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            _write_manager_event(output, "turn_started", session, pid=process.pid)
        session.pid = process.pid
        session.state = "running"
        session.last_error = None
        session.turn_started_at = time.time()
        session.updated_at = session.turn_started_at
        sessions[name] = session
        self._save(sessions)
        return session

    def update(
        self,
        name: str,
        notes: str | None = None,
        metadata: dict[str, Any] | None = None,
        merge_metadata: dict[str, Any] | None = None,
    ) -> Session:
        sessions = self.sessions()
        session = sessions.get(name)
        if not session:
            raise ValueError(f"unknown session '{name}'")
        if notes is not None:
            session.notes = notes
        if metadata is not None:
            session.metadata = metadata
        if merge_metadata is not None:
            session.metadata = {**(session.metadata or {}), **merge_metadata}
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
            with Path(session.log_path).open("ab") as output:
                _write_manager_event(output, "turn_stop_requested", session, pid=session.pid)
            if session.turn_started_at:
                session.active_seconds += time.time() - session.turn_started_at
        session.pid = None
        session.state = "stopped"
        session.turn_started_at = None
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


def _write_manager_event(output: Any, event_type: str, session: Session, **extra: Any) -> None:
    event = {
        "type": f"manager_{event_type}",
        "timestamp": time.time(),
        "name": session.name,
        "session_id": session.session_id,
        **extra,
    }
    output.write((json.dumps(event) + "\n").encode())
    output.flush()


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


def _recent_observed_fields(log_path: Path) -> dict[str, Any]:
    observed: dict[str, Any] = {"last_message_at": None, "model": None}
    if not log_path.exists():
        return observed
    for line in reversed(_tail_lines(log_path)):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        timestamp = _event_epoch(event.get("timestamp"))
        if observed["last_message_at"] is None and event.get("type") in {"assistant", "user"}:
            observed["last_message_at"] = timestamp
        if observed["model"] is None:
            message = event.get("message")
            if isinstance(message, dict) and isinstance(message.get("model"), str):
                observed["model"] = message["model"]
        if observed["last_message_at"] is not None and observed["model"] is not None:
            break
    return observed


def _event_epoch(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _tail_lines(path: Path, limit: int = 200, max_bytes: int = 256 * 1024) -> list[str]:
    """Read a bounded tail so status stays cheap for long-running sessions."""
    with path.open("rb") as handle:
        size = handle.seek(0, os.SEEK_END)
        handle.seek(max(0, size - max_bytes))
        chunk = handle.read().decode(errors="replace")
    return chunk.splitlines()[-limit:]


def _validate_name(name: str) -> None:
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError("session name must be a non-empty simple label")


def _print_session(session: Session, include_summary: bool = False) -> None:
    data = asdict(session)
    if include_summary:
        data["summary"] = _tail_summary(Path(session.log_path))
    print(json.dumps(data, indent=2))


def _format_table(sessions: list[Session]) -> str:
    columns = ["Name", "State", "Model", "Started", "Last message", "Active", "Directory", "Notes"]
    rows = []
    now = time.time()
    for session in sessions:
        active = session.active_seconds + ((now - session.turn_started_at) if session.turn_started_at else 0)
        rows.append([
            session.name,
            session.state,
            session.model or "—",
            _format_time(session.created_at),
            _format_time(session.last_message_at),
            _format_duration(active),
            session.cwd,
            session.notes or "—",
        ])
    widths = [len(column) for column in columns]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    render = lambda row: " | ".join(value.ljust(widths[index]) for index, value in enumerate(row))
    rule = "-+-".join("-" * width for width in widths)
    return "\n".join([render(columns), rule, *(render(row) for row in rows)]) if rows else "No sessions."


def _format_time(value: float | None) -> str:
    return datetime.fromtimestamp(value).astimezone().strftime("%Y-%m-%d %H:%M") if value else "—"


def _format_duration(seconds: float) -> str:
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--cctty", default=os.environ.get("CVC_CCTTY", "cctty"))
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start", help="start a named session in the background")
    start.add_argument("name")
    start.add_argument("--cwd", type=Path, required=True)
    start.add_argument("--prompt", required=True)
    start.add_argument("--notes", default="", help="free-form durable context for this session")
    start.add_argument("--metadata", type=_json_object, default={}, help="JSON object stored with the session")
    send = sub.add_parser("send", help="send a follow-up prompt to an idle session")
    send.add_argument("name")
    send.add_argument("--prompt", required=True)
    listing = sub.add_parser("list", help="list all named sessions")
    listing.add_argument("--format", choices=("table", "json"), default="table")
    status = sub.add_parser("status", help="show compact recent status")
    status.add_argument("name")
    stop = sub.add_parser("stop", help="stop the active turn")
    stop.add_argument("name")
    update = sub.add_parser("update", help="update session notes or metadata")
    update.add_argument("name")
    update.add_argument("--notes", help="replace notes (pass an empty value to clear)")
    update.add_argument("--metadata", type=_json_object, help="replace metadata with a JSON object")
    update.add_argument("--merge-metadata", type=_json_object, help="merge a JSON object into metadata")
    logs = sub.add_parser("events", help="print a bounded recent event tail")
    logs.add_argument("name")
    logs.add_argument("--lines", type=int, default=40)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manager = Manager(args.state_dir, args.cctty)
    try:
        if args.command == "start":
            _print_session(manager.run_turn(args.name, args.prompt, args.cwd, args.notes, args.metadata))
        elif args.command == "send":
            _print_session(manager.run_turn(args.name, args.prompt))
        elif args.command == "list":
            sessions = list(manager.sessions().values())
            if args.format == "json":
                print(json.dumps([asdict(session) for session in sessions], indent=2))
            else:
                print(_format_table(sessions))
        elif args.command == "status":
            session = manager.sessions().get(args.name)
            if not session:
                raise ValueError(f"unknown session '{args.name}'")
            _print_session(session, include_summary=True)
        elif args.command == "stop":
            _print_session(manager.stop(args.name))
        elif args.command == "update":
            _print_session(manager.update(args.name, args.notes, args.metadata, args.merge_metadata))
        elif args.command == "events":
            session = manager.sessions().get(args.name)
            if not session:
                raise ValueError(f"unknown session '{args.name}'")
            for line in _tail_lines(Path(session.log_path), limit=args.lines):
                print(line)
    except (OSError, ValueError) as exc:
        print(f"claude-voice-control: {exc}", file=sys.stderr)
        return 2
    return 0


def _json_object(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"invalid JSON: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("metadata must be a JSON object")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
