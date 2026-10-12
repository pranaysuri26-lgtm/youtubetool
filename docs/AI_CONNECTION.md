# AI connection: how the chat talks to Claude, and why it is allowed

## What the app does

For each chat turn, the engine runs **your installed, unmodified `claude` CLI** in Claude Code's documented
non-interactive mode:

```
claude -p "<your message + retrieved context>" --output-format stream-json --verbose
       --mcp-config <temp file: the workspace MCP server only> --strict-mcp-config
       --allowedTools mcp__shiftws Read Glob Grep
       --disallowedTools Bash Edit Write NotebookEdit WebFetch WebSearch Task Agent
       --append-system-prompt <workspace rules + structured project state>
       --add-dir <project folder> <analysis cache>
       [--resume <session id of this thread>]
```

- **Tools:** Claude can use only the workspace tools (68 typed MCP tools, which enqueue the same jobs as the UI
  buttons) plus read-only file access to the project and analysis folders. Read access is how it views frame images.
- **Skills:** the pinned yt-* skills live in the workspace's own agent folder (`<SHIFTWS_HOME>/agent/.claude/skills`),
  not in `~/.claude/skills`.
- **Memory:** the session id is stored only as supplementary context. Memory lives in the database. If a session
  can't be resumed, the turn continues from the database's structured state.
- **Environment:** the only variables the runner removes are the ones that would attach a child run to a parent
  Claude Code session (relevant only when the app itself is started from inside Claude Code). It never reads
  credential values.

## Why this respects the authentication boundary

- [Authentication](https://code.claude.com/docs/en/authentication): Claude Code signs in through Anthropic's own
  flow. This app never sees or stores those credentials, and never copies tokens.
- [Run Claude Code programmatically](https://code.claude.com/docs/en/headless): `claude -p` is the documented way
  to script Claude Code. Without `--bare` it uses your existing login.
- [Agent SDK overview](https://code.claude.com/docs/en/agent-sdk/overview) and the legal and compliance page:
  third-party developers may not offer claude.ai login in their products, or route requests through Free/Pro/Max
  credentials on behalf of their users. This app does neither:
  - It is your own single-user tool.
  - It embeds no Anthropic SDK login.
  - It spawns the CLI you installed and logged into, as you would from a terminal.

  Usage counts against your plan's normal limits, which assume ordinary individual use.
- If you'd rather not run the CLI from the app, set `ai.provider` to `offline` or `ollama`. You can still use
  Claude Code yourself in a terminal against the workspace MCP server (`shiftws mcp-config` shows the
  project-scoped registration).

## Verified here, with the real CLI (2.1.296, logged in)

`tests/test_claude_provider.py` and `docs/demo/acceptance_run.md` show the provider doing all of the following:

- Creating a project through chat.
- Starting an analysis report when gameplay was granted in chat.
- Viewing actual frame images and recording descriptions with confidence.
- Calling the yt-script hook scorer.
- Recalling earlier findings after a full app restart.
