# Claude Voice Control

Small, local control plane for several Claude Code sessions.

It launches the real interactive Claude Code terminal through
[cctty](https://github.com/Pyiner/cctty), but consumes cctty's structured
`stream-json` output. That avoids treating a terminal screen as an API.

## What it does today

- starts named sessions in the background;
- stores their stable Claude session IDs and event logs locally;
- reports running, idle, failed, and stopped state without replaying an entire
  terminal transcript;
- sends a follow-up prompt by resuming the named Claude session; and
- stops a running turn safely.

It is deliberately a CLI first. A voice agent can call these commands and
decide which status, question, or approval needs to be surfaced to a person.

## Requirements

- an authenticated `claude` CLI;
- `cctty` on `PATH` (or `CVC_CCTTY=/path/to/cctty`);
- Python 3.11+.

The manager passes `--no-chrome` by default. That prevents browser-integration
onboarding dialogs from blocking headless managed sessions.

## Quick start

```bash
python3 -m claude_voice_control start research \
  --cwd /path/to/project \
  --prompt "Inspect the repository and report the test layout."

python3 -m claude_voice_control list
python3 -m claude_voice_control status research
python3 -m claude_voice_control send research \
  --prompt "Now identify the three most important tests to run."
python3 -m claude_voice_control stop research
```

By default state is private and local under
`~/.local/share/claude-voice-control/`. Use `--state-dir` to override it for
tests or a dedicated deployment.

## Relationship to existing tools

This project is intentionally narrower than session-manager UIs such as
`yapcode`. Its goal is an automation-friendly interface for another agent
that already provides the conversational/voice layer. cctty supplies the
Claude-Code-to-structured-stream bridge; this repository supplies names,
background lifecycle, and compact status.

## Status model

Each named session has one active turn at a time:

- `running` — cctty is producing events;
- `idle` — the most recent turn completed and the session can be resumed;
- `failed` — cctty exited unsuccessfully; and
- `stopped` — the manager terminated the active turn.

Events are append-only JSONL logs. `status` reads only the recent tail and
extracts the most recent assistant text/result, so normal control calls do
not need to load a full Claude conversation.
