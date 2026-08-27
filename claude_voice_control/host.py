"""Long-lived cctty host for one managed Claude Code session."""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path


def _socket_path(state_dir: Path, name: str) -> Path:
    return state_dir / "hosts" / f"{name}.sock"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--cctty", required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    state_dir = args.state_dir.expanduser().resolve()
    registry = json.loads((state_dir / "sessions.json").read_text())
    session = registry[args.name]
    sock_path = _socket_path(state_dir, args.name)
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    if sock_path.exists():
        sock_path.unlink()

    command = [args.cctty, "--print", "--input-format", "stream-json", "--output-format", "stream-json", "--no-chrome"]
    command.extend(["--resume" if args.resume else "--session-id", session["session_id"]])
    log = Path(session["log_path"])
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as output:
        stdin_lock = threading.Lock()
        output_lock = threading.Lock()
        auto_count = 0
        process = subprocess.Popen(
            command,
            cwd=session["cwd"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        assert process.stdin is not None
        assert process.stdout is not None

        def write_prompt(prompt: str) -> None:
            payload = {
                "type": "user",
                "message": {"role": "user", "content": [{"type": "text", "text": prompt}]},
            }
            with stdin_lock:
                process.stdin.write((json.dumps(payload) + "\n").encode())
                process.stdin.flush()

        def relay_output() -> None:
            nonlocal auto_count
            for raw_line in process.stdout:
                with output_lock:
                    output.write(raw_line)
                    output.flush()
                try:
                    event = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") != "result" or not isinstance(event.get("result"), str):
                    continue
                latest = json.loads((state_dir / "sessions.json").read_text()).get(args.name, {})
                metadata = latest.get("metadata", {})
                result = event["result"]
                enabled = bool(metadata.get("auto_continue", False))
                limit = max(0, int(metadata.get("auto_continue_limit", 10)))
                if enabled and "WORKER_STATUS: IN_PROGRESS" in result.upper() and auto_count < limit:
                    auto_count += 1
                    manager_event = {
                        "type": "manager_auto_continue_queued",
                        "timestamp": time.time(),
                        "name": args.name,
                        "session_id": latest.get("session_id"),
                        "auto_continue_count": auto_count,
                        "auto_continue_limit": limit,
                    }
                    with output_lock:
                        output.write((json.dumps(manager_event) + "\n").encode())
                        output.flush()
                    write_prompt(
                        "Continue the assigned task now from the current context. Perform the next safe concrete "
                        "steps rather than stopping at a plan. End with WORKER_STATUS: COMPLETE, IN_PROGRESS, "
                        "NEEDS_INPUT, or BLOCKED."
                    )

        relay = threading.Thread(target=relay_output, name=f"{args.name}-output", daemon=True)
        relay.start()
        # A resumed cctty session replays its metadata before it can reliably
        # consume streamed input. Avoid exposing the control socket during
        # that restoration window, or the first prompt can remain queued.
        if args.resume:
            time.sleep(1.0)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(sock_path))
        os.chmod(sock_path, 0o600)
        listener.listen()
        listener.settimeout(0.5)
        stopping = False

        def stop_host(_signum: int, _frame: object) -> None:
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, stop_host)
        signal.signal(signal.SIGINT, stop_host)
        try:
            while not stopping and process.poll() is None:
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    continue
                with connection:
                    try:
                        request = json.loads(connection.recv(1024 * 1024).decode())
                        if request.get("type") == "interrupt":
                            os.killpg(process.pid, signal.SIGINT)
                        elif request.get("type") == "prompt" and isinstance(request.get("prompt"), str):
                            write_prompt(request["prompt"])
                        else:
                            raise ValueError("expected a prompt or interrupt request")
                        connection.sendall(b'{"ok":true}\n')
                    except (ValueError, json.JSONDecodeError, BrokenPipeError) as exc:
                        connection.sendall(json.dumps({"ok": False, "error": str(exc)}).encode() + b"\n")
        finally:
            listener.close()
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=10)
            if sock_path.exists():
                sock_path.unlink()
    return process.returncode or 0


if __name__ == "__main__":
    raise SystemExit(main())
