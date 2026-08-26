# Claude Voice Control

Small, local control plane for several Claude Code sessions.

It launches the real interactive Claude Code terminal through
[cctty](https://github.com/Pyiner/cctty), but consumes cctty's structured
`stream-json` output. That avoids treating a terminal screen as an API.

## What it does today

- starts named sessions in the background;
- associates every session with the working directory where Claude Code runs;
- keeps durable human-readable notes and arbitrary JSON metadata per session;
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
  --notes "Investigating the flaky integration suite" \
  --metadata '{"ticket":"TLU-222","priority":"low"}' \
  --prompt "Inspect the repository and report the test layout."

python3 -m claude_voice_control list
python3 -m claude_voice_control status research
python3 -m claude_voice_control send research \
  --prompt "Now identify the three most important tests to run."
python3 -m claude_voice_control stop research

# Add a note or metadata later, without disrupting the session.
python3 -m claude_voice_control update research \
  --merge-metadata '{"owner":"Patrick"}'

# Apply durable instructions to each newly created session.
python3 -m claude_voice_control config set-bootstrap \
  --text "Before starting work, read and follow /Users/patrick/AGENTS.md."
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

## Session identity and context

`start` requires a simple session name and `--cwd`. The name is the durable
control handle; the working directory never changes when the session is
resumed. `--notes` is free-form text for human context, while `--metadata`
stores a JSON object for structured routing details such as an issue key,
owner, or project type. Both are included in `list` and `status` output.

`list` renders a compact table for conversational use. It includes name, state,
model, creation time, last message time, cumulative active time, working
directory, and notes. Use `list --format json` when another program needs the
full records. The metadata includes `startup_command`, a redacted command array
with `<prompt>` in place of the original prompt.

## Bootstrap prompt

The local `bootstrap_prompt` is prepended to the first task prompt of each new
named session. It is not repeated on `send`, so it behaves like session setup
rather than accumulating instructions on every turn. `config set-bootstrap`
stores it in the private controller state directory, and a session records
`bootstrap_prompt_applied` in its metadata for auditability.
