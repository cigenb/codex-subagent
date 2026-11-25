# Codex Subagent (`codex-sub`)

Use the `codex-sub_codex` MCP server (`codex_spawn`, `codex_review`, `codex_wait`, `codex_status`, `codex_steer`, `codex_interrupt`) to delegate independent work to Codex (default `gpt-6.2-sol`, effort `high`, standard tier) in parallel in the background.

## When to Delegate

Delegate only in these cases; otherwise do the work yourself.

- The user asks for Codex.
- The work splits into independent parts that can run in parallel with your own work or with each other; pass `worktree=true` when parallel tasks edit the same git repository.
- You want a second opinion on changes: run a read-only defect review with `codex_review`.

## Calling Conventions

- Pass the active workspace's absolute path as `cwd` on `codex_spawn` (unless continuing an existing `thread_id`) and `codex_review` — the MCP server process does not run in the workspace directory.
- Pass your `Conversation ID` as `conversation_id` on `codex_spawn` / `codex_review` (required for hook auto-delivery; omitted `conversation_id` will not auto-deliver) and on `codex_wait` / `codex_status` when omitting `ids`, because all Antigravity conversations share one MCP server process.
- Codex cannot see this conversation: include the background, relevant file paths and constraints, plus the goal, scope (which files or directories it may change), verification criteria, and a concise return format (`file:line`, no large code dumps). For `thread_id` follow-ups, send only incremental instructions.
- Only set `model` (`gpt-6.2-sol` / `gpt-6.2-astra` / `gpt-6.1-terra`), `effort`, or `fast` when the user explicitly requests them.
- Codex runs with `sandbox=danger-full-access` and `approvalPolicy=never` and directly edits files and runs commands.

## Results and Waiting

- Completed task results are delivered automatically by `PreInvocation` / `Stop` hooks.
- When idle and waiting for results, call `codex_wait` (pass explicit `ids` or `conversation_id`; each wait defaults to 120s and caps at 150s to stay under Antigravity's 3-minute MCP deadline). On timeout, tasks keep running; call `codex_wait` again or inspect via `codex_status`.
- Show `codex_review` findings to the user before applying changes.
- Verify critical changes yourself; a Codex task reporting completion is not automatic sign-off.
