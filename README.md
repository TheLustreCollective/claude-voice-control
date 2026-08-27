# Claude Voice Control

Small, local control plane for several Claude Code sessions.

It launches the real interactive Claude Code terminal through
[cctty](https://github.com/Pyiner/cctty), but consumes cctty's structured
`stream-json` output. That avoids treating a terminal screen as an API.
Prompts also use Claude's streaming JSON input protocol, which makes
resume/restart a real continuation of the named Claude conversation.

## What it does today

- starts named sessions in the background;
- keeps a small local session host and Claude's streaming input pipe alive
  between turns, rather than treating a process exit as the normal boundary;
- associates every session with the working directory where Claude Code runs;
- keeps durable human-readable notes and arbitrary JSON metadata per session;
- stores their stable Claude session IDs and event logs locally;
- reports running, idle, failed, and stopped state without replaying an entire
  terminal transcript;
- tracks worker task state separately from the state of the most recent Claude
  turn, so an "I'll start by reading the instructions" response is not treated
  as task completion;
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

# Move a completed session out of the active table; its logs and context remain.
python3 -m claude_voice_control archive research
python3 -m claude_voice_control list --archived
python3 -m claude_voice_control unarchive research

# Resume the original Claude conversation, optionally with a new instruction.
python3 -m claude_voice_control restart research --prompt "Continue from the last findings."

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

Behind each active named session is a local Unix-socket host process. It owns
one cctty/Claude process, retains its stdin pipe while Claude is idle, and
accepts later prompts through the private socket. Consequently, `send` uses
the same live Claude process when it is healthy. If that host genuinely exits,
the next `send`/`restart` creates a replacement with `--resume` and the stored
Claude session ID.

`Turn` and `Task` are deliberately separate in the session table. A turn is
`idle` once Claude has responded, but the task stays `in_progress` until the
worker explicitly reports `WORKER_STATUS: COMPLETE`, `BLOCKED`, or
`NEEDS_INPUT` in its final response. This lets the supervising ChatGPT/Codex
agent send a follow-up or surface an actionable question instead of mistaking a
brief planning response for completed work.

Sessions are active by default. `archive` moves a non-running session out of
the default `list` view without deleting its registry entry, JSONL log, or
Claude session ID. `list --archived` and `list --all` expose historical
records. `unarchive` returns one to the active set. `restart` resumes the
original Claude conversation after a completed, failed, or stopped turn. It
does not revive an old OS process: it starts a fresh cctty/Claude process with
`--resume <stored-Claude-session-ID>` and Claude's streaming JSON input
protocol, so the new instruction is processed in the restored conversation.

Events are append-only JSONL logs. `status` reads only the recent tail and
extracts the most recent assistant text/result, so normal control calls do
not need to load a full Claude conversation.

## Session identity and context

`start` requires a simple session name and `--cwd`. The name is the durable
control handle; the working directory never changes when the session is
resumed. `--notes` is free-form text for human context, while `--metadata`
stores a JSON object for structured routing details such as an issue key,
owner, or project type. Both are included in `list` and `status` output.

`list` renders a compact table for conversational use. It includes name, turn state,
model, creation time, last message time, cumulative active time, working
directory, and notes. Use `list --format json` when another program needs the
full records. The metadata includes `startup_command`, a redacted command array
with `<prompt>` in place of the original prompt.

After a Claude turn returns, the turn state is `waiting` while the overall task
remains `in_progress`; this is distinct from a completed task and is shown in
`list` and `status`.

## Bootstrap prompt

The local `bootstrap_prompt` is prepended to the first task prompt of each new
named session. Its default directs workers to read Patrick's `AGENTS.md`, do
the requested work rather than stopping after a plan, and use an explicit
`WORKER_STATUS` marker. It is not repeated on `send`, so it behaves like
session setup rather than accumulating instructions on every turn. `config
set-bootstrap` stores a customized version in the private controller state
directory, and a session records `bootstrap_prompt_applied` in its metadata for
auditability.
