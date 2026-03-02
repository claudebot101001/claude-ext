# claude-ext Technical Documentation

An extensible framework built on the Claude Code CLI. Core philosophy: Claude Code is already a complete AI coding agent — this framework does exactly two things: **wrap CLI invocations** and **manage extension lifecycles**. No reinventing the wheel.

## Directory Structure

```
claude-ext/
├── core/                          # Core layer (stable, rarely modified)
│   ├── engine.py                  # Claude Code CLI wrapper + SessionManager entry + services registry
│   ├── events.py                  # Structured event log (JSONL append + query + rotation)
│   ├── extension.py               # Extension base class (interface contract + health_check)
│   ├── bridge.py                  # Unix socket RPC bridge (main process ↔ MCP child process)
│   ├── mcp_base.py                # MCP stdio server base class (JSON-RPC boilerplate)
│   ├── pending.py                 # Async request/response registry (register → wait → resolve)
│   ├── registry.py                # Extension discovery, loading, lifecycle, health check aggregation
│   ├── session.py                 # tmux-backed multi-session management (core module)
│   └── status.py                  # Status queries (auth + usage API)
├── extensions/                    # Extension layer (each subdirectory fully independent)
│   ├── telegram/
│   │   ├── extension.py           # Telegram Bot bridge (multi-session)
│   │   └── requirements.txt       # Extension-specific dependencies
│   ├── cron/
│   │   ├── extension.py           # Scheduled task scheduler + MCP tool registration
│   │   ├── mcp_server.py          # MCP stdio server (Claude-callable cron tools)
│   │   ├── store.py               # Job persistence (JSON + flock)
│   │   └── requirements.txt       # croniter
│   ├── vault/
│   │   ├── extension.py           # Encrypted credential store (bridge + MCP + access control)
│   │   ├── store.py               # VaultStore: Fernet encrypt/decrypt + unified lockfile
│   │   ├── mcp_server.py          # MCP stdio server (vault four tools)
│   │   └── requirements.txt       # cryptography>=42.0
│   ├── memory/
│   │   ├── extension.py           # Cross-session memory (MCP registration + system prompt + seed)
│   │   ├── store.py               # MemoryStore: Markdown I/O + path safety + flock
│   │   └── mcp_server.py          # MCP stdio server (memory five tools)
│   ├── heartbeat/
│   │   ├── extension.py           # Autonomous heartbeat (dual-channel scheduler + three-tier execution + usage-aware)
│   │   ├── store.py               # HeartbeatStore: state + instruction file I/O + flock
│   │   ├── mcp_server.py          # MCP stdio server (six heartbeat tools)
│   │   └── trigger_cli.py         # Standalone CLI: external processes trigger heartbeat via bridge.sock
│   ├── ask_user/
│   │   ├── extension.py           # Interactive question extension (bridge + PendingStore)
│   │   └── mcp_server.py          # MCP stdio server (ask_user tool)
│   ├── subagent/
│   │   ├── extension.py           # Multi-agent orchestration (PM → worker sessions)
│   │   ├── mcp_server.py          # MCP stdio server (10 subagent tools)
│   │   ├── store.py               # SubAgentStore: agent records + flock + prefix ID matching
│   │   └── worktree.py            # Git worktree utilities (create/diff/merge/cleanup)
│   └── session_ask/
│       ├── extension.py           # Cross-session RPC (bridge + PendingStore)
│       └── mcp_server.py          # MCP stdio server (3 session tools)
├── config.yaml                    # Global config (engine params + extension toggles + extension config) (.gitignore)
├── config.yaml.example            # Config template (tracked)
├── main.py                        # Entry point (load config → build engine → init sessions → register extensions → run)
├── requirements.txt               # Global Python dependencies
└── CLAUDE.md                      # This document
```

---

## Architecture Overview

```
┌─────────────┐     ┌──────────────────────────────────────────┐
│  config.yaml │────▶│              main.py                      │
└─────────────┘     │  load config → build engine →             │
                    │  init_sessions → recover → registry.start │
                    └──────────┬─────────────────────────────── ┘
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                 ▼
     ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
     │  Extension A  │ │  Extension B  │ │  Extension C  │
     │  (telegram)   │ │  (cron)       │ │  (future)     │
     │  user bridge  │ │  sched+MCP    │ │               │
     └──────┬───────┘ └──────┬───────┘ └──────┬───────┘
            │ cb_tg          │ cb_cron         │
            ▼                ▼                 ▼
     ┌─────────────────────────────────────────────┐
     │       ClaudeEngine + SessionManager          │
     │                                              │
     │  SessionManager (multi-session, recommended):│
     │    tmux session → run.sh → claude -p         │
     │    file IPC: prompt.txt → stream.jsonl       │
     │    state persistence: state.json             │
     │    decoupled from main process, crash recovery│
     │    delivery_callbacks: [cb_tg, cb_cron, ...]  │
     │    mcp_servers: {"cron": {...}}  → run.sh     │
     │                                              │
     │  engine.ask() (lightweight one-shot, compat): │
     │    direct subprocess → claude -p              │
     └─────────────────────────────────────────────┘
                         │
                         ▼
                   tmux sessions
                   └── claude -p ... --mcp-config mcp_config.json
```

**Data flow**: The primary direction is extensions → engine/session_manager → tmux → CLI. Reverse channel: MCP server inside CLI → bridge.sock (Unix socket RPC) → main process handler → PendingStore/deliver. Extensions never communicate directly; they share service instances via `engine.services`.

---

## Core Layer Reference

### core/session.py — SessionManager (Core Module)

**tmux-backed multi-session manager. Each Claude Code session runs in its own tmux session, fully decoupled from the main process.**

#### Session Data Structure

```python
@dataclass
class Session:
    id: str                    # UUID
    name: str                  # User-visible name
    slot: int                  # Fixed slot number (1-N), reusable after deletion
    user_id: str               # Owning user (generic string identifier, e.g. str(telegram_user_id))
    working_dir: str           # Claude Code working directory
    context: dict              # Extension-defined routing data (e.g. Telegram puts {"chat_id": ...})
    status: SessionStatus      # idle / busy / dead / stopped
    claude_session_id: str     # Claude CLI session UUID (used for --resume)
    tmux_session: str          # tmux session name "cc-{uuid}"
    prompt_count: int          # Number of prompts sent
```

#### DeliveryCallback Signature

```python
# (session_id, result_text, metadata)
DeliveryCallback = Callable[[str, str, dict], Awaitable[None]]
```

Extensions access the session object via `session_manager.sessions[session_id]` and read routing info from `session.context` (e.g. chat_id). Core does not pass any extension-specific routing parameters.

#### create_session Signature

```python
async def create_session(
    self, name: str, user_id: str, working_dir: str,
    context: dict | None = None,
) -> Session
```

The `context` field is for extension-defined data. For example, the Telegram extension passes `{"chat_id": chat_id}`, a Slack extension could pass `{"channel_id": ...}`. Core does not interpret context contents — it simply persists and restores them as-is.

#### File-Based IPC

Each session has files under `~/.claude-ext/sessions/{uuid}/`:

| File | Purpose |
|------|---------|
| `state.json` | Persisted session metadata (atomic write) |
| `prompt.txt` | Current prompt content |
| `claude_cmd.sh` | Inner script: actual claude invocation (with `--output-format stream-json --verbose`) |
| `run.sh` | Outer script: PTY wrapper (`script -qfec` → forces line buffering) |
| `stream.jsonl` | Claude's streaming JSON output (written by `script`, grows line by line) |
| `stderr.log` | Error output |
| `exitcode` | Completion marker (file exists = command finished), content is claude exit code |
| `mcp_config.json` | Optional. MCP server config (with session-specific env vars) |

#### run.sh Dual-File Template

**Problem**: When `claude -p ... > file.jsonl`, Node.js uses block buffering for file stdout, causing the file to remain 0 bytes for long periods.
**Solution**: `script -qfec` creates a PTY → Node.js detects TTY → line buffering → events written line by line.

**claude_cmd.sh** (inner, actual claude invocation):
```bash
#!/bin/bash
unset CLAUDECODE
PROMPT=$(cat "/path/prompt.txt")
cd "/working/dir"
claude -p "$PROMPT" --output-format stream-json --verbose \
  --session-id "uuid" \           # First prompt uses --session-id
  # or --resume "uuid"            # Subsequent prompts use --resume
  --permission-mode bypassPermissions \
  --disallowedTools AskUserQuestion \     # Optional, extensions register via register_disallowed_tool
  --mcp-config "/path/mcp_config.json" \  # Optional
  2>"/path/stderr.log"
```

**run.sh** (outer, PTY wrapper):
```bash
#!/bin/bash
script -qfec "bash /path/claude_cmd.sh" /path/stream.jsonl
echo $? > /path/exitcode
```

Key changes (compared to the old single-file run.sh):
- `--output-format json` → `--output-format stream-json --verbose`
- stdout is no longer redirected to `output.json`; `script` writes to `stream.jsonl`
- `script -f` flushes after each write; `-e` passes the child process exit code
- `stream.jsonl` may have a script header line at the beginning (non-JSON); parser skips it
- `exitcode` remains the completion signal

**On prompt safety**: `PROMPT=$(cat file)` assigns file contents to a variable without shell interpretation. When `"$PROMPT"` is passed as a double-quoted variable to `claude -p`, special characters like `$` and backticks in the content are not recursively interpreted. This is standard bash variable expansion behavior.

#### Core Methods

| Method | Responsibility |
|--------|---------------|
| `create_session()` | Allocate slot + create tmux session + state directory + start queue worker |
| `send_prompt()` | Enqueue prompt into per-session queue, return queue position. Rejects DEAD sessions, auto-resets STOPPED |
| `stop_session()` | Drain queue → mark STOPPED → Ctrl-C → background 5s exitcode write (non-blocking). For IDLE sessions, only drains queue. Returns `(bool, int)` |
| `destroy_session()` | Kill tmux + cancel worker + delete state directory |
| `recover()` | On startup: scan disk state + check tmux liveness, restore/reconnect (results buffered to pending) |
| `shutdown()` | Cancel all workers, **does not kill tmux sessions** (key design point) |
| `add_delivery_callback()` | Register result delivery callback (supports multiple). On first registration, flushes results buffered during recover() |
| `register_mcp_server()` | Register MCP server config (optional `tools` metadata). All subsequent sessions' run.sh auto-includes `--mcp-config` |
| `list_mcp_tools()` | Return all registered MCP servers and their declared tool metadata |
| `add_system_prompt(text, mcp_server=None)` | Append system prompt fragment. Optional `mcp_server` tag enables per-session filtering via `exclude_mcp_servers` |
| `register_env_unset()` | Register env vars to unset in Claude sessions (prevents sensitive info leakage) |
| `register_disallowed_tool()` | Register built-in CC tools to disable (used when extensions provide MCP replacements, passed via `--disallowedTools`) |
| `add_session_customizer()` | Register per-session customization callback. Called before every prompt execution. Returns `SessionOverrides` |

#### Per-Session Customization

Extensions can register **session customizers** via `add_session_customizer()` to customize per-session configuration without modifying `create_session` callers. Customizers are synchronous callbacks called before every prompt execution (in `_generate_run_scripts`), receiving the `Session` object and returning `SessionOverrides` (or `None` to skip).

```python
@dataclass
class SessionOverrides:
    extra_system_prompt: list[str] | None = None       # Appended to global system prompt
    exclude_mcp_servers: set[str] | None = None        # Removed from global MCP registry
    extra_mcp_servers: dict[str, dict] | None = None   # Added (last-wins on key conflict)
    extra_disallowed_tools: list[str] | None = None    # Appended to disallowed list
    extra_env_unset: list[str] | None = None           # Appended to unset list
```

**Semantic rules**:
- **R1**: `exclude_mcp_servers` removes from the global `_mcp_servers` registry and filters tagged system prompts. Never removes from another customizer's `extra_mcp_servers`. Execution order: copy global → apply exclude → merge extras.
- **R2**: When multiple customizers return the same `extra_mcp_servers` key, last-wins by registration order (`dict.update` semantics).
- **R3**: Customizers are called per-prompt, not per-session. They must be fast, synchronous, and side-effect-free (no I/O, no blocking).

**Example** (future roles extension):
```python
def role_customizer(session):
    role = session.context.get("role")
    if role == "analyst":
        return SessionOverrides(
            extra_system_prompt=["You are a data analyst."],
            exclude_mcp_servers={"vault"},  # analysts don't need vault
        )
    return None

self.engine.session_manager.add_session_customizer(role_customizer)
```

When no customizers are registered, `_collect_overrides` returns all-None defaults, maintaining backward compatibility.

#### Slot Mechanism

Each user can have at most N concurrent sessions (default 5, configurable). Each session is assigned the lowest available slot number at creation time. Slot numbers are fixed for the session's lifetime and released for reuse after deletion. Users reference sessions by slot number rather than list index, avoiding confusion after deletions. Auto-naming follows the `session-{slot}` pattern, ensuring slot numbers match name digits (e.g. `#1 session-1`, `#2 session-2`).

#### Queue Mechanism

Each session has its own asyncio.Queue and worker task. Multiple messages sent to the same session are automatically queued and executed sequentially. `/stop` also drains the queue. The worker checks session status after dequeuing a prompt, skipping STOPPED and DEAD sessions.

#### State Machine

```
Created → IDLE → (send_prompt) → BUSY → (completed) → IDLE
                                     → (stop) → STOPPED → (send_prompt) → IDLE
                                     → (tmux died) → DEAD
```

**Status protection**: `_execute_prompt` checks `session.status` after the streaming loop returns; if `stop_session` has already set it to STOPPED, it delivers an `is_stopped` notification rather than silently discarding.

#### Startup Recovery Matrix

| state.json status | tmux alive? | exitcode exists? | Action |
|---|---|---|---|
| busy | Yes | Yes | Read result with `_parse_stream_result`, mark idle, buffer to pending_deliveries |
| busy | Yes | No | Still running, resume stream monitoring |
| busy | No | — | Mark dead |
| idle/stopped | Yes | — | Reconnect directly |
| idle/stopped | No | — | Recreate tmux session |
| dead | — | — | Load into memory, user can view and /delete |

**Key timing**: `recover()` runs before `start_all()` (when delivery callbacks are not yet set), so completed results are buffered into `_pending_deliveries`. When an extension calls `add_delivery_callback()`, the buffer is automatically flushed.

#### Streaming Output and Heartbeat

SessionManager uses `--output-format stream-json --verbose` and incrementally reads `stream.jsonl` via `_stream_completion`, delivering events to delivery callbacks in real time.

**Stream event classification** (`_classify_stream_event`):

| Event type | content block | Action | metadata |
|------------|--------------|--------|----------|
| `assistant` | `text` | **Deliver** | `{"is_stream": True, "stream_type": "text"}` |
| `assistant` | `tool_use` | **Deliver** | `{"is_stream": True, "stream_type": "tool_use", "tool_name": ..., "tool_input": ...}` |
| `assistant` | `thinking` | Skip | — |
| `user` | `tool_result` | Skip | — |
| `system` | — | Skip | — |
| `result` | — | Extract metadata | `{"is_final": True, "claude_session_id": ..., "total_cost_usd": ..., ...}` |

**Delivery metadata conventions**:

| Field | Meaning |
|-------|---------|
| `is_stream: True` | This is an intermediate streaming event |
| `stream_type: "text"` | Claude's text response |
| `stream_type: "tool_use"` | Claude invoked a tool |
| `is_heartbeat: True` | Heartbeat event (sent only when no recent deliveries) |
| `is_final: True` | Task complete, includes cost/turns summary |
| `is_stopped: True` | Task was interrupted by /stop |
| `is_error: True` | An error occurred (timeout, tmux death, etc.) |

**Heartbeat**: Only sent when no new events have occurred for 30+ seconds since the last delivery (`HEARTBEAT_INTERVAL = 30.0`). Streaming events themselves serve as liveness signals, so heartbeats are not triggered during normal execution.

#### MCP Server Registration

Extensions can provide custom tools to Claude sessions via `register_mcp_server()`:

```python
# Register in extension's start() (with tool metadata for introspection)
self.engine.session_manager.register_mcp_server("cron", {
    "command": "python",
    "args": ["/path/to/mcp_server.py"],
    "env": {"CRON_STORE_PATH": "/path/to/store.json"},
}, tools=[
    {"name": "cron_create", "description": "Create a scheduled task"},
    {"name": "cron_status", "description": "Get job status or list all jobs"},
])

# Introspect registered tools (e.g. for /status command display)
tools = self.engine.session_manager.list_mcp_tools()
# {"cron": [{"name": "cron_create", "description": "..."}, ...], "vault": [...]}
```

SessionManager automatically generates `mcp_config.json` for each session, injecting session-specific env vars (`CLAUDE_EXT_SESSION_ID`, `CLAUDE_EXT_STATE_DIR`, `CLAUDE_EXT_USER_ID`), and adds the `--mcp-config` flag to run.sh. MCP server processes use these env vars to obtain the current session's context. `MCPServerBase` exposes `session_user_id` property for per-user logic (e.g. per-user memory paths).

#### Environment Variable Isolation

Extensions can register env vars to clear in Claude sessions via `register_env_unset()`, preventing sensitive info from leaking into the LLM-accessible process environment:

```python
# In vault extension's start()
self.sm.register_env_unset("CLAUDE_EXT_VAULT_PASSPHRASE")
```

SessionManager unsets all registered variables along with `CLAUDECODE` when generating `claude_cmd.sh`.

#### Built-in Tool Disabling

When extensions provide MCP replacements for built-in tools, they can disable the originals via `register_disallowed_tool()`:

```python
# In ask_user extension's start()
self.sm.register_disallowed_tool("AskUserQuestion")
```

SessionManager merges all registered tool names with `engine.disallowed_tools` config and passes them to Claude CLI via `--disallowedTools` when generating `claude_cmd.sh`. Compared to system prompt instructions like "Do NOT use X", CLI-level disabling is a hard enforcement and saves tokens.

#### Multiple Delivery Callbacks

`add_delivery_callback()` supports registering multiple callbacks. All callbacks are triggered on each result delivery, and each decides whether to handle it based on `session.context`:

```python
# Telegram callback: checks context["chat_id"]
# Cron callback: checks context["cron_job_id"]
# Both fire independently on the same event, without interference
```

#### Active Session Persistence (Extension-Layer Responsibility)

Active session selection is a UX concept belonging to the extension layer, not core. The Telegram extension manages `active_sessions.json` read/write and cleanup on its own. Other extensions may use different persistence strategies or none at all.

### core/events.py — EventLog

Structured event log, JSONL append file.

- Storage: `{state_dir}/events.jsonl`, one JSON object per line
- Format: `{"ts": "ISO8601", "type": "dotted.name", "session_id": "...", "detail": {...}}`
- Concurrency safety: `events.lock` unified lockfile (LOCK_SH/LOCK_EX)
- Rotation: File renamed to `.1` when exceeding 10 MB (single-generation rotation)
- `log()` is best-effort; warns on OSError without raising
- `query(event_type?, session_id?, since?, limit=100)` supports filtering, returns newest-first

**Event naming convention**: `namespace.action` format.

| Namespace | Event types | Trigger location |
|-----------|-------------|-----------------|
| `session` | `session.created` / `session.destroyed` / `session.stopped` / `session.prompt` / `session.completed` / `session.dead` | SessionManager |
| `ext` | `ext.started` / `ext.stopped` / `ext.load_failed` | Registry |
| `vault` | `vault.store` / `vault.retrieve` / `vault.delete` | Vault bridge handler |
| `cron` | `cron.triggered` | Cron `_execute_job()` |
| `heartbeat` | `heartbeat.noop` / `heartbeat.decided` / `heartbeat.started` / `heartbeat.completed` / `heartbeat.skipped` / `heartbeat.triggered` | Heartbeat scheduler + delivery |

### core/engine.py — ClaudeEngine

Provides two invocation modes:

1. **SessionManager (recommended)**: Accessed via `engine.session_manager`, tmux-backed, supports multi-session, crash recovery, async delivery. Used by the Telegram extension.
2. **`engine.ask()` (lightweight)**: Direct subprocess call to `claude -p`, blocks for result. Suitable for simple extensions that don't need persistence/multi-session (e.g. cron, webhook).

**`engine.services: dict[str, Any]`** — Cross-extension service registry. Extensions register service instances in `start()`, and other extensions look them up via `.get()`. The `enabled` list order in `config.yaml` determines load/start order; earlier extensions register services first.

```python
# SessionManager mode (extensions needing multi-session like Telegram)
session = await engine.session_manager.create_session(name="task1", ...)
await engine.session_manager.send_prompt(session.id, "fix the bug")

# Lightweight mode (simple one-shot calls)
response = await engine.ask(prompt="what is 1+1", cwd="/tmp")

# Cross-extension service discovery
self.engine.services["vault"] = self._vault              # Registered in vault extension's start()
vault = self.engine.services.get("vault")                # Looked up in another extension

# Event logging
if self.engine.events:
    self.engine.events.log("vault.store", session_id, {"key": key})

# Health checks (via registry)
health = await engine.registry.health_check_all()  # {"vault": {"status": "ok", ...}, ...}
```

**`engine.events: EventLog | None`** — Structured event log instance, created in `init_sessions()`. Extensions access via `self.engine.events`, guarded with `if self.engine.events:` for compatibility with scenarios without a session manager.

**`engine.registry`** — Registry instance reference, assigned in `main.py`. Used for extension health check aggregation (`registry.health_check_all()`).

### core/bridge.py — Bridge RPC

Unix Domain Socket bridge, allowing MCP child processes (sync blocking) to call back into the main process (async).

- **BridgeServer**: Main process async side. `add_handler(handler)` registers multiple handlers; requests are tried in order, first non-None response wins
- **BridgeClient**: MCP child process sync blocking side. `call(method, params, timeout)` sends a request and blocks for the response, with auto-reconnect support
- **Protocol**: Line-delimited JSON (`{"method": ..., "params": ...}` → `{"result": ...}`), Unix Domain Socket
- **Zero core module dependencies**: stdlib only (json, socket, asyncio)

### core/pending.py — PendingStore

Generic async register → wait → resolve pattern.

- **PendingEntry** data structure: `key` (16-char hex), `session_id`, `data` (extension-defined payload), `future`, `timeout`
- `register(session_id, data, timeout)` → creates entry, returns it for `await wait(key)`
- `resolve(key, value)` → delivers response, wakes waiter
- `cancel_for_session(session_id)` → batch cancel (e.g. when session is stopped/destroyed)
- `get_for_session(session_id)` → get pending entry for a session (used by frontends for UI display)
- **Use cases**: ask_user (current), future: email_wait, approval_gate, etc.

### core/mcp_base.py — MCPServerBase

MCP stdio JSON-RPC protocol boilerplate. Extension MCP servers just inherit and define tools + handlers.

- Obtains session context via env vars: `CLAUDE_EXT_SESSION_ID`, `CLAUDE_EXT_STATE_DIR`, `CLAUDE_EXT_USER_ID`
- `session_user_id` property returns the session's owning user ID (from `CLAUDE_EXT_USER_ID` env var)
- `session_context()` reads the current session's `state.json` (to get user_id, context, etc.)
- Lazy `bridge` property: only initializes BridgeClient when `CLAUDE_EXT_BRIDGE_SOCKET` is set
- Subclasses only need to set `name`, `tools` (schema list), and `self.handlers` (name → callable mapping)
- Zero external dependencies (pure stdlib)

### core/extension.py — Extension Base Class

**This is the only interface contract extensions must follow:**

```python
class Extension(ABC):
    name: str = "unnamed"

    def configure(self, engine: ClaudeEngine, config: dict) -> None:
        """Called once before start, receives engine instance and extension config dict"""
        self.engine = engine
        self.config = config

    @abstractmethod
    async def start(self) -> None: ...  # Start (begin polling, open webhook, etc.)

    @abstractmethod
    async def stop(self) -> None: ...   # Graceful shutdown

    async def health_check(self) -> dict:
        """Return extension health status. Non-abstract, defaults to {"status": "ok"}"""
        return {"status": "ok"}
```

Extensions manage multi-session via `self.engine.session_manager` or make simple calls via `self.engine.ask()`.

**health_check() override convention**: Return a dict with at least a `status` field (`"ok"` / `"degraded"` / `"error"`). May include extension-specific status info (e.g. `secrets` count, `jobs` count, `policies` list). Registry aggregates all extension health via `health_check_all()`, with a 5-second timeout per extension.

### core/registry.py — Registry

Extension discovery and lifecycle management. **Core never hardcodes imports of any extension.**

Discovery: Scans all subdirectories under `extensions/` that contain `extension.py`.
Loading: `importlib.import_module(f"extensions.{name}.extension")` dynamic import, obtains the `ExtensionImpl` class.
Lifecycle: `load()` → `start_all()` → (running) → `stop_all()` (reverse order).
Public interface: `extensions` property returns a copy of loaded extensions list (in registration order).
Health checks: `health_check_all()` concurrently calls all extensions' `health_check()` via `asyncio.gather`, 5-second per-extension timeout, returns `dict[str, dict]`.
Event logging: `start_all()` / `stop_all()` / `load()` automatically log `ext.started` / `ext.stopped` / `ext.load_failed` events.

### core/status.py — Status Queries

Standalone utility module, no extension dependencies.

| Function | Data source | Returns |
|----------|-------------|---------|
| `get_auth_info()` | `claude auth status` command | `{loggedIn, email, subscriptionType, ...}` |
| `get_usage()` | `GET api.anthropic.com/api/oauth/usage` (reads OAuth token from `~/.claude/.credentials.json`) | `{five_hour: {utilization, resets_at}, seven_day: {...}, extra_usage: {...}}` |
| `format_status(auth, usage, session)` | Both above + session metadata | Formatted text string |
| `relative_time(iso_str)` | ISO timestamp | Human-readable relative time string (e.g. `"in 2h30m"`) |

---

## Extension Layer Reference

### How to Add a New Extension (Complete Steps)

Using a hypothetical `discord` extension as an example:

**Step 1: Create directory and files**

```
extensions/discord/
├── __init__.py          # Empty file
├── extension.py         # Must contain ExtensionImpl class
└── requirements.txt     # Extension-specific dependencies (optional)
```

**Step 2: Implement ExtensionImpl**

```python
# extensions/discord/extension.py
from core.extension import Extension

class ExtensionImpl(Extension):
    name = "discord"

    def configure(self, engine, config):
        super().configure(engine, config)
        self.token = config["token"]

    async def start(self) -> None:
        # Register delivery callback (for multi-session support)
        self.engine.session_manager.add_delivery_callback(self._deliver)
        # Start logic
        ...

    async def stop(self) -> None:
        # Stop logic
        ...
```

**Step 3: Register in config.yaml**

```yaml
enabled:
  - telegram
  - discord

extensions:
  discord:
    token: "YOUR_DISCORD_BOT_TOKEN"
```

**Done. No changes needed to any file in core/, no changes to main.py, no changes to other extensions.**

### Existing Extension: telegram

Telegram Bot bridge, based on tmux multi-session management.

**Commands:**

| Command | Function |
|---------|----------|
| `/start` | Welcome message |
| `/new [name] [dir]` | Create new session (optional name and working directory). Single argument recognized as directory. Supports `~` expansion and paths relative to `working_dir` |
| `/sessions` | List all sessions, `*` marks current active |
| `/switch <slot\|name>` | Switch active session (by slot number or name) |
| `/status` | Auth + Usage + current session info |
| `/stop` | Stop running task on current session + drain queue |
| `/delete <slot\|name> [force]` | Delete session (kill tmux + clean files). BUSY sessions require `force` |

**Supported message types:**
- **Text** — sent as prompt directly
- **Photo** (PNG/JPG) — downloaded to `{working_dir}/.claude-ext-uploads/`, prompt includes file path so Claude Code can read the image with its Read tool
- **Document** (any file) — same download flow; Claude reads PDFs, code, etc. via Read tool
- **Other** (voice, video, sticker, etc.) — replied with "Unsupported message type"

**Message processing flow:**
1. User sends message → check for active session; if none, auto-create `session-1` (slot #1). Auto-selection priority: IDLE > STOPPED > BUSY, skips DEAD
2. Active session busy → message queued, reply "Queued (position N)"
3. Active session idle → submit prompt, reply "Processing..."
4. Background worker completes → result delivered to chat via delivery callback

**Streaming delivery**: Every step of Claude's operation is pushed to Telegram in real time. Text events use 2-second debounce aggregation (avoids message flooding), tool call events are sent immediately.

| Event | Display |
|-------|---------|
| Text response | Aggregated full text block |
| Tool call | `🔧 Read: /path/to/file.py`, `🔧 Bash: git status...` summaries |
| Task complete | `--- $cost \| N turns ---` |
| Task stopped | `Task stopped.` |
| Heartbeat | `Still working... (Nm elapsed)` |

Each message is prefixed with `[#slot name]` to identify the source. Long messages are chunked at newline boundaries (4000 char limit); on send failure, subsequent chunks are skipped to avoid log flooding.

**Debounce mechanism**: Each session maintains a `_StreamBuffer`. Text events are appended to the buffer and the 2-second timer is reset. The buffer is flushed when the timer expires, a tool call arrives, or the task ends.

### Existing Extension: cron

Scheduled task scheduler. Serves two roles:
1. **Scheduler**: Triggers Claude session execution based on cron expressions or one-shot delays.
2. **MCP tool provider**: Registers MCP server so Claude can dynamically create/manage scheduled tasks during conversations.

#### Two Job Sources

| Source | Creation method | Typical scenario |
|--------|----------------|-----------------|
| Static (config.yaml) | Admin-defined in config | Daily code review, weekly dependency check |
| Dynamic (Claude calls MCP tools) | Claude calls `cron_create` in conversation | "Check upload status in 20 minutes", "Summarize inbox daily at 8am" |

#### MCP Tools

Claude automatically gets these tools in sessions (injected via `--mcp-config`):

| Tool | Function |
|------|----------|
| `cron_create` | Create scheduled task. `cron_expr` for periodic, `run_at` for one-shot delay (e.g. `+20m`) |
| `cron_delete` | Delete job (supports ID prefix matching) |
| `cron_status` | Query job details (with `job_id`) or list all jobs (without) |

The MCP server obtains current session context via env vars `CLAUDE_EXT_SESSION_ID` and `CLAUDE_EXT_STATE_DIR`, automatically inheriting `user_id`, `context` (including `chat_id`), and `working_dir`.

#### Session Strategy

| Strategy | Description | Typical scenario |
|----------|------------|-----------------|
| `new` | Creates an independent session per trigger, auto-cleaned after completion | Context-free standalone tasks |
| `reuse` | Reuses the session that created the job, preserving full conversation context | "Check if data upload completed in 20 minutes" |

#### Reuse Strategy Robustness

When the reuse target session is DELETEd by the user:
1. Scheduler detects session doesn't exist
2. **Fallback**: Creates a new session, sets `claude_session_id` to the original session's Claude CLI UUID + `prompt_count=1`
3. run.sh automatically uses `--resume` instead of `--session-id`, recovering conversation context from Claude Code's own storage
4. Notifies user: "Original session was deleted, Claude context restored in new session"

Key reason: `destroy_session()` only deletes `~/.claude-ext/sessions/{uuid}/` and kills tmux; it doesn't touch Claude Code's `~/.claude/` directory. `--resume` recovers from Claude Code's own storage.

When the reuse target session is STOPped: No special handling needed — `send_prompt()` automatically resets STOPPED to IDLE.

#### Slots and Auto-Reclamation

`new` strategy sessions occupy slots. If user slots are full:
1. Scheduler attempts to reclaim completed cron sessions (marked with `cron_auto_cleanup`)
2. If no reclaimable slots, job is deferred to next check cycle and user is notified

#### Result Delivery

Cron task results are routed via `session.context`:
- `context["chat_id"]` → Telegram callback delivers to user
- `context["cron_job_id"]` → Cron callback updates job status

Both callbacks fire independently on the same delivery event, without interference.

#### Job Data Model

```python
@dataclass
class CronJob:
    id: str                          # UUID
    name: str                        # Human-readable name
    prompt: str                      # Prompt sent on trigger
    working_dir: str                 # Working directory
    user_id: str                     # Owning user

    cron_expr: str | None            # "0 8 * * *" — periodic
    run_at: str | None               # ISO timestamp — one-shot

    session_strategy: str            # "new" | "reuse"
    session_id: str | None           # Reuse target (our session ID)
    claude_session_id: str | None    # Claude CLI session UUID (for --resume fallback)

    notify_context: dict             # Notification routing (e.g. {"chat_id": 12345})
    enabled: bool
    created_by: str                  # Source session_id or "config"
    last_run: str | None
    next_run: str | None
```

### Existing Extension: vault

Encrypted credential store. Lets the Agent securely store and retrieve API keys, passwords, private keys, and other sensitive information.

**Three-layer architecture**:

```
Claude session
  └─ MCP server (vault_store/list/retrieve/delete)
       └─ bridge RPC (Unix socket, carries session_id)
            └─ main process bridge handler → VaultStore (encrypt/decrypt)
```

The MCP server process **does not hold the passphrase**; all encryption/decryption occurs via bridge RPC in the main process. Passphrase priority: `CLAUDE_EXT_VAULT_PASSPHRASE` env var > `{vault_dir}/.passphrase` file > auto-generated (`secrets.token_urlsafe(32)`, 0600 permissions). Zero-config enabled. Uses `register_env_unset()` to ensure the env var doesn't leak into Claude sessions.

**Security boundary**: Encryption is defense-in-depth (prevents ciphertext files from being directly readable if accidentally copied), not the primary security boundary. In `bypassPermissions` mode Claude has full filesystem access; the real access controls are `_internal_prefixes` (controls what MCP can read) and OS permissions (controls who can run the process).

**store.py — VaultStore**:

- Key derivation: PBKDF2-HMAC-SHA256 (600K iterations) + random salt → Fernet key
- Encryption: Fernet (AES-128-CBC + HMAC authenticated encryption), entire JSON blob encrypted
- Concurrency control: Unified lockfile (`secrets.lock`). Read ops use `LOCK_SH`, write ops use `LOCK_EX` covering the full read-modify-write cycle, preventing concurrent write lost updates
- Atomic writes: temp file + rename
- File permissions: 0700 directory + 0600 files

**MCP Tools**:

| Tool | Function |
|------|----------|
| `vault_store` | Store a secret (key + value + optional tags) |
| `vault_list` | List all keys and tags (does not return values) |
| `vault_retrieve` | Read secret value (enters LLM context) |
| `vault_delete` | Delete a secret |

**Key naming validation**: Bridge handler enforces `category/service/name` format (regex `^[a-zA-Z0-9_-]+(/[a-zA-Z0-9._-]+)+$`). Keys not matching this format are rejected at `vault_store`. Examples: `api/github/token`, `email/smtp/password`, `wallet/eth/0xABC.../privkey`.

**Access control provision**: `extension.py` maintains `_internal_prefixes: list[str]` (currently empty). `vault_retrieve` checks key prefixes; matching requests are rejected with a message to use the dedicated extension tools. Phase 4 Wallet only needs `_internal_prefixes.append("wallet/")` to block private key access via MCP. Other extensions access programmatically via `engine.services["vault"].get()`, bypassing prefix restrictions.

**System prompt constraint**: Injected instructions require Claude to never echo secret values to the user; after retrieval, values should be used directly in subsequent tool calls.

**Audit**: Every bridge call carries `session_id`; the handler logs audit events (`vault_store`, `vault_retrieve`, `vault_delete`).

### Existing Extension: memory

Cross-session persistent memory system. Lets the Agent accumulate knowledge across conversations, remember user preferences, and track project context.

**Design decision: Direct file I/O, no bridge RPC.** Memory is plaintext Markdown with no encryption/access control requirements and no security constraint against entering the LLM context. The MCP server process holds its own MemoryStore instance for direct disk reads/writes, eliminating socket round-trips. If auditing is needed, add `log.info` to MemoryStore methods.

**store.py — MemoryStore + MemoryIndex**:

- Storage format: Plain Markdown files, human-readable, grep-friendly (source of truth)
- Three-tier structure: `MEMORY.md` (hot index) / `topics/<name>.md` (deep knowledge) / `daily/YYYY-MM-DD.md` (logs)
- Path safety: Rejects absolute paths, `..` traversal, non-`.md` files, symlink escapes (`resolve()` + `is_relative_to`)
- Concurrency control: Unified lockfile (`memory.lock`). Read ops use `LOCK_SH`, write ops use `LOCK_EX`
- Atomic writes: `write()` uses temp+rename; `append()` appends directly under `LOCK_EX` (avoids large file copies)
- Read limit: 512 KB truncation, prevents large files from blowing up context
- **FTS5 search index** (`MemoryIndex`): SQLite FTS5 with BM25 ranking and Porter stemming, stored at `.search_index.db`. Derived cache — auto-rebuilds on corruption/deletion. Heading-aware Markdown chunking provides section context in results. Multi-process safe via WAL mode + `busy_timeout`. Graceful fallback to regex if FTS5 unavailable. Queries with regex metacharacters automatically route to legacy regex search.

**MCP Tools**:

| Tool | Function |
|------|----------|
| `memory_read` | Read a memory file |
| `memory_write` | Overwrite/create a file (auto-creates parent directories) |
| `memory_append` | Append content (auto UTC timestamp, suitable for daily logs) |
| `memory_search` | FTS5 full-text search with BM25 ranking (falls back to regex for regex patterns) |
| `memory_list` | List files (sorted by modification time descending, filterable by subdirectory) |

**System prompt driven**: Injects a concise system prompt guiding the Agent to operate memory files only through MCP tools (Session Start reads `MEMORY.md` + Curation rules + file organization instructions). Storage location `~/.claude-ext/memory/` is globally shared, independent of CC's built-in auto-memory (`~/.claude/projects/`). Note: Memory cannot disable built-in Read/Write via `--disallowedTools` (needed for coding), so behavioral guidance via system prompt is retained.

**Seed file**: On first startup, automatically creates a `MEMORY.md` template (with User Preferences / Active Projects / Key Decisions / Topic Files sections), without overwriting existing content.

### Existing Extension: heartbeat

Autonomous heartbeat extension. Periodically wakes Claude to check user-defined task lists, LLM decides whether action is needed, and only creates a full session for execution when necessary.

**Architecture: Dual-channel scheduler + three-tier execution**

```
Timer Channel (periodic timer + adaptive backoff)
  ↓
Scheduler Loop (asyncio.wait: first-come-first-served)
  ↑
Trigger Channel (asyncio.Queue, external extension real-time events)

Tier 0: Scheduler gate (Python, zero cost) → enabled? utilization? concurrent? active hours?
Tier 1: Pre-check (Python) → HEARTBEAT.md non-empty? pending events?
Tier 2: LLM decision (engine.ask(), ~500 tokens) → contains "NOTHING" (≤200 chars) = noop, else task description
Tier 3: Execution (full session, only when Tier 2 decides action needed) → Telegram streaming delivery
```

**Silent suppression**: Tier 2 uses `engine.ask()` (lightweight subprocess), does not create a tmux session. NOOP detection: response contains "NOTHING" and is ≤200 characters (guards against false positives from task descriptions). Only Tier 3 sessions have `chat_id`, so Telegram only delivers for those.

**Storage**:
- State: `{state_dir}/heartbeat/state.json` (JSON + flock)
- Instructions: `{state_dir}/heartbeat/HEARTBEAT.md` (Markdown + flock)
- HeartbeatStore is a pure file I/O layer with no asyncio objects (MCP server processes also instantiate it)

**Event trigger interface** (other extensions call via `engine.services["heartbeat"]`):
```python
hb = engine.services.get("heartbeat")
if hb:
    hb.trigger("wallet", "price_alert", "immediate", {"asset": "BTC", "price": 95000})
```

`trigger()` is a sync method. `immediate` wakes the scheduler immediately; `normal` accumulates until the next timer expiry.

**Service registration**: `engine.services["heartbeat"] = self` (registers the extension instance, not the store), because the service interface includes `trigger()`.

**Usage-aware cost control**:

| Utilization | Timer heartbeat | Immediate trigger |
|-------------|----------------|-------------------|
| < 80%  | Normal | Normal |
| 80-95% | Paused | Allowed |
| ≥ 95%  | Paused | Paused |

Utilization is obtained via `core/status.py: get_usage()`, cached for 60 seconds.

**Adaptive backoff** (affects timer channel only):

| Consecutive NOTHINGs | Interval multiplier | Example (30 min base) |
|---|---|---|
| 0-3 | 1x | 30 minutes |
| 4-6 | 2x | 1 hour |
| 7-9 | 4x | 2 hours |
| 10+ | 8x | 4 hours |

**MCP Tools** (accessible from all sessions; `heartbeat_instructions` and `heartbeat_status` use direct file I/O, `heartbeat_trigger` uses bridge RPC, `heartbeat_get_trigger_command` returns command text):

| Tool | Function |
|------|----------|
| `heartbeat_instructions` | Read HEARTBEAT.md (omit `content`) or overwrite it (provide `content`) |
| `heartbeat_status` | Scheduler status text; optionally set `enabled=false` to pause or `enabled=true` to resume |
| `heartbeat_trigger` | Submit event to trigger heartbeat check (`immediate` wakes immediately, `normal` accumulates until next timer) |
| `heartbeat_get_trigger_command` | Return a shell command usable in external scripts (includes trigger_cli.py path and bridge.sock path, all arguments safely quoted via `shlex.quote()`) |

**External triggering**: `trigger_cli.py` is a pure-stdlib standalone CLI script (checked into repo). Any external process (nohup background tasks, monitoring scripts, CI pipelines) can connect to bridge.sock via it to trigger heartbeats. The Agent obtains the ready-to-use shell command for the current environment via the `heartbeat_get_trigger_command` MCP tool (includes absolute trigger_cli.py path and bridge.sock path, all arguments safely quoted via `shlex.quote()` to prevent command breakage with paths containing spaces or special characters), for embedding in workflows like `nohup bash -c 'rsync ... && <trigger_command>' &` to achieve out-of-session event wakeup.

**HeartbeatState data structure**:

```python
@dataclass
class HeartbeatState:
    enabled: bool = True
    last_run: str | None = None          # ISO (most recent Tier 2 call)
    next_run: str | None = None          # ISO (next timer expiry)
    run_count: int = 0                   # Total Tier 2 call count
    runs_today: int = 0                  # Today's Tier 2 call count
    runs_today_date: str | None = None   # YYYY-MM-DD
    consecutive_noop: int = 0            # Consecutive NOTHING count
    active_session_id: str | None = None # Current Tier 3 session
```

**Safety controls**: Daily run limit of 48, utilization throttle at 80% / pause at 95%, active hours window, concurrency limit of 1, adaptive backoff up to 8x, automatic session cleanup 5 seconds after completion.

**Recovery check**: `start()` checks for residual `active_session_id`; if the target session is DEAD/STOPPED/IDLE/nonexistent, it's cleared to prevent permanent concurrency gate blocking.

**Event logging**: `heartbeat.noop` / `heartbeat.decided` / `heartbeat.started` / `heartbeat.completed` / `heartbeat.skipped` / `heartbeat.triggered`.

### Existing Extension: ask_user

Interactive question extension. Lets Claude ask the user questions mid-session and wait for answers.

**Data flow**:
```
Claude → MCP tool(ask_user) → bridge.call("ask_user") → BridgeServer handler
  → PendingStore.register + SessionManager.deliver(is_question=True)
  → [User answers via Telegram/other frontend]
  → PendingStore.resolve → bridge returns → MCP tool returns → Claude continues
```

**MCP Tool**: `ask_user(question, options?)`
- `question`: Question to ask the user (required)
- `options`: Optional list of choices; omitted for free-text input

**Built-in tool disabling**: Extension disables the CC built-in question tool via `register_disallowed_tool("AskUserQuestion")` (enforced via `--disallowedTools` CLI flag), and injects a concise system prompt guiding use of the MCP ask_user tool.

**Frontend integration**: When the delivery callback receives `{"is_question": True, "request_id": ..., "options": [...]}`, it displays UI (e.g. Telegram inline keyboard). After the user answers, call `engine.pending.resolve(request_id, answer)` to deliver the response.

### Existing Extension: subagent

Multi-agent orchestration. A PM session spawns independent worker sessions (each in its own tmux + optional git worktree), waits for completion, reviews diffs, and merges results back.

**Data flow**:
```
PM Claude → MCP tool(subagent_*) → bridge.call("subagent_*") → BridgeServer handler
  → SessionManager.create_session (worker in isolated tmux)
  → Worker executes independently (parallel with other workers)
  → Delivery callback → SubAgentStore.update + PendingStore.resolve → PM unblocks
```

**MCP Tools** (10):
- `subagent_spawn(task, name?, worktree?, paradigm?)` — Create worker, returns agent_id
- `subagent_wait(agent_ids, timeout?)` — **Blocking**: wait until all specified agents complete
- `subagent_status(agent_id?, include_result?)` — Status + cost + optional result text; omit agent_id to list all
- `subagent_send(agent_id, prompt)` — Follow-up prompt (re-activates completed agents)
- `subagent_stop(agent_id)` — Interrupt running worker
- `subagent_diff(agent_id)` — Full git diff for worktree agent
- `subagent_merge(agent_id)` — Squash-merge worktree into parent branch (staged, not committed)
- `subagent_delete(agent_id)` — Delete completed/stopped/failed agent (destroys session + worktree)
- `subagent_reclaim_respond(request_id, approve)` — Respond to slot reclamation request from another session
- `session_info()` — Get metadata about the current session (ID, status, runtime, cost, working dir)

**Paradigms**: `coder` (full access, auto-cleanup), `reviewer` (read-only, persistent, excludes vault/heartbeat/cron/ask_user), `researcher` (read-only, persistent, excludes vault/heartbeat/cron/ask_user). Custom paradigms via config with optional `exclude_mcp_servers`.

**Key mechanisms**:
- **PendingStore integration**: `subagent_wait` registers pending entries per agent; delivery callback resolves them when workers complete. PM blocks on `asyncio.wait` without polling.
- **Session customizer**: Workers get `exclude_mcp_servers={"subagent"} | paradigm.exclude_mcp_servers` + role-specific system prompt. Tagged system prompts from excluded MCP servers are also filtered out.
- **Prefix ID matching**: `SubAgentStore.get_agent()` supports unique prefix matching (≥6 chars) so truncated IDs from MCP display still resolve correctly.
- **Git worktree isolation**: Workers operate in `{state_dir}/worktrees/{repo}/{branch}/` with branch `subagent/{name}-{hex8}`. Merge uses `git merge --squash` (no checkout).
- **Auto-cleanup**: Completed workers are destroyed after `cleanup_delay` (default 120s). Store records persist for status queries after session destruction.

**Storage**: `{state_dir}/subagent/agents.json` (flock-based, same pattern as HeartbeatStore/JobStore).

### Existing Extension: session_ask

Cross-session RPC. Sessions can ask questions to other sessions and wait for replies, enabling inter-session coordination.

**Data flow**:
```
Session A → MCP tool(session_ask) → bridge.call("session_ask") → BridgeServer handler
  → PendingStore.register + SessionManager.send_prompt(B, question)
  → Session B receives question as prompt
  → Session B calls MCP tool(session_reply) → bridge.call("session_reply")
  → PendingStore.resolve → Session A unblocks with reply
```

**MCP Tools** (3):
- `session_ask(target_session_id, question)` — Send question to another session and **block** until reply
- `session_reply(request_id, reply)` — Reply to an inter-session question (called by the target session)
- `session_list()` — List all active sessions for the current user (ID, name, slot, status)

**Design**:
- Uses PendingStore for async request/response (same pattern as ask_user)
- Question is injected as a prompt into the target session via `send_prompt()`
- Target session's reply is delivered via bridge RPC → `pending.resolve()`
- Default timeout: 300s, configurable
- Fail-closed validation: if the target session doesn't exist or is stopped, the request fails immediately

---

## Configuration: config.yaml

```yaml
engine:
  # model: claude-sonnet-4-6     # Omit to use CLI default
  max_turns: 0                    # 0 = unlimited
  permission_mode: bypassPermissions  # Required; without this in -p mode, tools cannot execute
  allowed_tools: null             # null = allow all; or specify whitelist
  disallowed_tools: null          # null = no extra disabling; merged with extension-registered ones

state_dir: ~/.claude-ext          # Session state persistence directory

sessions:
  max_sessions_per_user: 5        # Max concurrent sessions per user (= slot count)

enabled:                          # Enabled extension names (correspond to directories under extensions/)
  - vault                         # Encrypted credential store (zero-config, passphrase auto-generated)
  - memory                        # Cross-session memory (zero-config)
  - telegram
  # - heartbeat                   # Autonomous heartbeat (periodic check + LLM decision + execution)
  # - ask_user                    # Interactive questioning
  # - cron                        # Scheduled task scheduler

extensions:                       # Per-extension config
  vault:
    {}                            # Passphrase auto-generated and stored in {state_dir}/vault/.passphrase
  memory:
    {}                            # Files stored in {state_dir}/memory/
  telegram:
    token: "BOT_TOKEN"
    allowed_users: [123456789]    # Telegram user ID whitelist
    working_dir: null             # Default working directory, null = current dir; /new supports relative paths and ~ expansion
  heartbeat:
    interval: 1800                # Base interval in seconds (default 30 minutes)
    # active_hours: "08:00-22:00" # Optional UTC active window
    max_daily_runs: 48            # Daily Tier 2 decision limit
    usage_throttle: 80            # Utilization ≥ this: only immediate triggers allowed
    usage_pause: 95               # Utilization ≥ this: all paused
    user_id: "123456789"          # Required: owning user ID
    notify_context:               # Delivery routing
      chat_id: 123456789
    working_dir: null             # Default working directory
  cron:
    jobs:                         # Static job list (optional, Claude can also create dynamically)
      - name: daily-review
        cron_expr: "0 9 * * *"
        prompt: "Review yesterday's commits"
        working_dir: /path/to/project
        user_id: "123456789"
        notify_context: {chat_id: 123456789}
  subagent:
    max_subagents_per_session: 5  # Max concurrent workers per PM session
    default_paradigm: coder       # Default paradigm for new workers
    cleanup_delay: 120.0          # Seconds before auto-destroying completed workers
  session_ask:
    timeout: 300                  # Default timeout for inter-session questions
    max_question_length: 2000     # Max characters for question text
```

**Note: `config.yaml` contains sensitive information (bot token) and is excluded via `.gitignore`. Copy `config.yaml.example` and fill in actual values.**

### SIGHUP Config Reload

Sending SIGHUP to the main process triggers a live config reload without restart:

1. Re-reads `config.yaml` from disk
2. Updates `SessionManager.max_sessions_per_user` and `engine.max_turns`
3. Calls `reconfigure(config)` on all loaded extensions (synchronous, signal-safe)
4. Extensions can update their runtime settings without full restart

The `reconfigure()` method on the `Extension` base class is a no-op by default; extensions override it to handle config changes they care about.

---

## Decoupling Design Principles

The following principles are hard requirements for this framework; all new features must comply:

1. **Core never imports any extension.** Extension discovery is entirely via `importlib` dynamic import. If you find yourself writing `from extensions.xxx import ...` in core/, the design is wrong.

2. **Extensions never depend on each other.** Extension A does not import Extension B, does not call Extension B's methods, does not read Extension B's state. If two extensions need to share data, it should be done indirectly through core-layer shared services: `engine.session_manager` (session management), `engine.services` (cross-extension service registry), `engine.pending` (async request/response).

3. **Each extension is a fully self-contained directory.** Deleting an extension directory + removing it from the `enabled` list = zero impact.

4. **New features = new directories, no changes to existing code.** If adding a new extension requires modifying `core/` or other extensions, the abstraction is leaking.

5. **Core-layer public services must be generic.** `core/session.py` does not bind to any specific extension. Session uses `user_id: str` (generic identifier) and `context: dict` (extension-defined data) instead of platform-specific fields. DeliveryCallback only passes `(session_id, text, metadata)`; extensions read routing info from the session object themselves. Heartbeats are emitted as structured events; extensions format them as needed.

6. **Configuration is declarative.** Extension behavior is controlled by `config.yaml`, not hardcoded.

---

## Running

```bash
cd ~/claude-ext
source .venv/bin/activate    # Python 3.12+, dependencies in .venv
cp config.yaml.example config.yaml  # Fill in actual config before first run
python main.py
```

Requirements:
- Python 3.12+ (uses `str | None` type syntax)
- tmux 3.x+ (for multi-session management)
- Claude Code CLI installed with `claude` command in PATH
- Authenticated via `claude auth login` (subscription user)
- pip dependencies: `python-telegram-bot>=21.0`, `pyyaml>=6.0`, `cryptography>=42.0` (vault extension), `croniter>=1.0.0` (cron extension)

---

## Known Limitations

- **script PTY side effects**: `script -qfec` may write a header line at the beginning of `stream.jsonl` (e.g. `Script started on ...`); the parser skips non-JSON lines. In rare cases, the PTY may inject ANSI escape sequences.
- **Token refresh**: The `accessToken` in `~/.claude/.credentials.json` has an expiration time. Auto-refresh is not currently handled (Claude Code CLI handles its own refresh, but direct usage API calls will fail if the token expires).
- **Global session limit**: Currently only per-user `max_sessions_per_user` limiting exists, with no global cap. Watch server resources in multi-user scenarios.
- **MCP server state sharing**: In stdio mode, each Claude session launches an independent MCP server process. Multiple processes share the same `cron_jobs.json` file via flock. Brief lock contention may occur under high concurrent writes.
- **Cron session recovery**: `--resume` relies on Claude Code's own session storage (`~/.claude/`). Very old sessions may have been cleaned up by Claude Code, in which case fallback resume degrades to a new conversation.
- **Backward compat**: Old session directories may contain `output.json` (batch mode legacy). `_parse_stream_result` preferentially reads `stream.jsonl`; when absent, falls back to `_parse_result` (reads `output.json`).
