# codex-subagent

English | [简体中文](README.zh-CN.md)

Use Codex as a subagent inside Claude Code or Antigravity: the host delegates tasks, Codex works in the background, and finished results come back into the conversation on their own.

Pure Python standard library, no third-party dependencies. Verified on Windows 11.

## Features

- **Async dispatch**: `codex_spawn` returns immediately and the host carries on. Multiple tasks run in parallel inside one long-lived Codex process, without cold starts per task
- **Automatic result delivery**: finished results are delivered on the next action without polling. In Claude Code, `codex_wait` moves to the background after two minutes and wakes the session on completion; in Antigravity, results arrive via `PreInvocation` / `Stop` hooks and each `codex_wait` call caps at 150s (within Antigravity's 3-minute MCP call limit) without stopping running tasks on timeout
- **Mid-flight control**: `codex_steer` sends a follow-up instruction and Codex changes course after its current step; `codex_interrupt` stops a task and keeps what's done; pass `thread_id` to continue a thread
- **Code review**: `codex_review` runs Codex's built-in review mode — does not modify files, defect-focused, with file and line numbers
- **Host & session isolation**: each Claude Code window gets its own Codex process; Antigravity shares one Codex process across conversations and isolates hook delivery by `conversation_id`. Claude Code and Antigravity use separate state directories and can run side by side
- **Independent model settings**: subagents default to `gpt-6.2-sol` / reasoning effort `high` / standard service tier, unaffected by Codex desktop chat settings; change defaults via environment variables or override per task (GPT-6.2 Sol / GPT-6.2 Astra / GPT-6.1 Terra)

## How it works

```
Claude Code / Antigravity ──MCP──> server.py ──JSON-RPC──> codex app-server (long-lived child process)
Claude Code               ──hook──> hook.py
Antigravity               ──hook──> antigravity/hook.py
```

## Requirements

- Windows 11, Python 3.10+ (`python` on PATH), and Claude Code 2.1.212+ or Antigravity
- A logged-in Codex. By default the `codex.exe` bundled with the Codex desktop app is used (under `%LOCALAPPDATA%\OpenAI\Codex\bin\`, updated with the app); the app itself does not need to be running. The npm package `@openai/codex` also works, or point `CODEX_SUB_BIN` at any binary. Both share the login and config in `~/.codex`

## Install

Clone anywhere; `<install-dir>` below stands for that path.

### Claude Code

1. Register the MCP server:

```bash
claude mcp add codex-sub -s user -- python "<install-dir>\server.py"
```

2. Merge the hooks into `~/.claude/settings.json` (escape backslashes as `\\` in JSON):

```json
{
  "hooks": {
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python \"<install-dir>\\hook.py\" UserPromptSubmit", "timeout": 10}]}],
    "PostToolUse":      [{"hooks": [{"type": "command", "command": "python \"<install-dir>\\hook.py\" PostToolUse", "timeout": 10}]}],
    "Stop":             [{"hooks": [{"type": "command", "command": "python \"<install-dir>\\hook.py\" Stop", "timeout": 10}]}]
  }
}
```

3. Optional: add `"mcp__codex-sub"` to `permissions.allow`, otherwise every call asks for confirmation.

Restart your Claude Code session. To try it without touching global config: `claude --plugin-dir "<install-dir>"`.

### Antigravity

Install as a global Antigravity plugin (writes manifest, MCP/hook pointers, and rules to `~/.gemini/config/plugins/codex-sub/`; `server.py` and `antigravity/hook.py` continue to run directly from `<install-dir>`):

```bash
python "<install-dir>\antigravity\install.py"
```

Antigravity runs `server.py --client antigravity` with `PreInvocation` / `Stop` hooks and an isolated state directory (`%LOCALAPPDATA%\codex-sub-agy`).

## Usage

Ask Claude Code or Antigravity directly. It delegates to Codex only when you ask for Codex, when the work splits into parts that can run in parallel, or for a second-opinion review; otherwise it does the work itself:

> Hand the auth module refactor to Codex. Meanwhile fix the API tests yourself, then merge when it's done.

> Have Codex review what I just changed.

> Run two Codex tasks in parallel: one fixes `add`, one fixes `mul`, each in its own worktree.

| Tool | Purpose |
|---|---|
| `codex_spawn` | Dispatch a task. `wait_s` waits a while first, `thread_id` continues a thread, `worktree` runs in a separate branch, `model` / `effort` override per task, `fast` uses the Codex Fast tier for that task |
| `codex_review` | Code review: uncommitted changes / against a base branch / a commit / custom instructions |
| `codex_wait` | Wait for results (default 3000s in Claude Code; default 120s / max 150s in Antigravity); a timeout only returns the current state, the task keeps running |
| `codex_status` | List tasks, or show one task's details and full output |
| `codex_steer` | Send a follow-up instruction to a running task |
| `codex_interrupt` | Interrupt, keeping what has been produced |

- Tasks are ephemeral by default and don't appear in Codex history; pass `persist=true` to resume across host restarts via `thread_id`. Subagents can use the skills and plugins in `~/.codex`
- For fire-and-forget work pass `detach=true`; the host won't wait for it when ending its turn

## Permissions

Subagents run with **the built-in sandbox off** (`sandbox=danger-full-access`) and **interactive tool approvals disabled** (`approvalPolicy=never`) by default: they can edit any file and run any command without stopping to ask, and interactive follow-up questions are automatically declined so background tasks do not pause waiting for input. The reason: Codex's Windows sandbox runs commands under a restricted account that cannot see tools installed in the user profile (Python, pnpm, uv, …), so a sandboxed subagent cannot run tests.

`codex_review` uses the same default: review mode itself does not modify files, but it needs to run `git` and similar commands. There is no per-task sandbox option: `read-only` enables the same restricted sandbox, inside which no command can start, so Codex cannot even read files. The default can be changed with `CODEX_SUB_SANDBOX`.

## Configuration

| Environment variable | Default |
|---|---|
| `CODEX_SUB_BIN` | auto-detected |
| `CODEX_SUB_SANDBOX` | `danger-full-access` |
| `CODEX_SUB_MODEL` / `CODEX_SUB_EFFORT` / `CODEX_SUB_SERVICE_TIER` | `gpt-6.2-sol` / `high` / `default` |
| `CODEX_SUB_STATE_DIR` | `%LOCALAPPDATA%\codex-sub` (Claude mode logs and session state; server and hook must use the same value) |
| `CODEX_SUB_AGY_STATE_DIR` | `%LOCALAPPDATA%\codex-sub-agy` (Antigravity mode logs and session state) |

## Notes

- Relies on some experimental `codex app-server` APIs; Codex updates may change the protocol. Verified with Codex 0.163
- Each Claude Code session keeps one `app-server` process alive (~150 MB); Antigravity shares one across the app. Both exit with the MCP server, and on Windows a `KILL_ON_JOB_CLOSE` Job Object owned by the MCP server process cleans up the child tree if it terminates unexpectedly

Test: `python tests/smoke.py` (calls Codex for real, about 3 minutes).

Uninstall:
- Claude Code: `claude mcp remove codex-sub -s user`, then remove the three hooks from `~/.claude/settings.json`.
- Antigravity: `python "<install-dir>\antigravity\install.py" --uninstall`.

## License

MIT
