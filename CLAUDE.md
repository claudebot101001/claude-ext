# claude-ext Technical Documentation

Extensible framework for Claude Code CLI. Core philosophy: **wrap CLI invocations + manage extension lifecycles**. Nothing more.

For full architecture details, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Directory Structure

```
claude-ext/
├── core/                    # Core layer (stable, rarely modified)
│   ├── engine.py            # ClaudeEngine: CLI wrapper + services registry
│   ├── session.py           # SessionManager: tmux-backed multi-session
│   ├── bridge.py            # Unix socket RPC (main process ↔ MCP child)
│   ├── mcp_base.py          # MCP stdio server base class
│   ├── extension.py         # Extension base class (interface contract)
│   ├── registry.py          # Extension discovery + lifecycle
│   ├── pending.py           # Async request/response registry
│   ├── events.py            # Structured event log (JSONL)
│   └── status.py            # Auth + usage API queries
├── extensions/              # Each subdirectory is fully independent
│   ├── vault/               # Encrypted credential store (Fernet + bridge RPC)
│   ├── memory/              # Cross-session persistent memory (Markdown + direct I/O)
│   ├── heartbeat/           # Autonomous periodic agent (dual-channel + 3-tier)
│   ├── cron/                # Scheduled tasks (croniter + MCP)
│   ├── ask_user/            # Interactive questions (bridge + PendingStore)
│   ├── subagent/            # Multi-agent orchestration (PM → worker sessions)
│   └── telegram/            # Telegram bot bridge (multi-session + streaming)
├── config.yaml              # Runtime config (.gitignored)
├── config.yaml.example      # Config template
└── main.py                  # Entry point
```

## Architecture

```
config.yaml → main.py → Registry → Extensions
                │                      │
                ▼                      ▼
          ClaudeEngine         tmux sessions (claude -p)
          SessionManager       MCP servers (per-session)
          Bridge RPC           file IPC (prompt → stream → result)
```

**Data flow**: Extensions → engine/session_manager → tmux → CLI. Reverse: MCP server → bridge.sock → main process → PendingStore/deliver.

## Core APIs

### Extension Interface

```python
class Extension(ABC):
    name: str = "unnamed"

    def configure(self, engine: ClaudeEngine, config: dict) -> None:
        self.engine = engine
        self.config = config

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    async def health_check(self) -> dict:
        return {"status": "ok"}
```

### ClaudeEngine

Two invocation modes:
- **`engine.session_manager`** — tmux-backed, multi-session, crash recovery, async delivery
- **`engine.ask(prompt, cwd)`** — lightweight one-shot subprocess call

Shared services:
- **`engine.services`** — cross-extension service registry (e.g. `engine.services["vault"]`)
- **`engine.events`** — structured event log (`engine.events.log(type, session_id, detail)`)
- **`engine.pending`** — async request/response (register → wait → resolve)

### SessionManager Key Methods

| Method | Purpose |
|--------|---------|
| `create_session(name, user_id, working_dir, context)` | Create tmux session + queue worker |
| `send_prompt(session_id, prompt)` | Enqueue prompt, return queue position |
| `stop_session(session_id)` | Drain queue + Ctrl-C + mark STOPPED |
| `destroy_session(session_id)` | Kill tmux + delete state |
| `add_delivery_callback(cb)` | Register `async (session_id, text, metadata)` callback |
| `register_mcp_server(name, config, tools)` | Add MCP server to all future sessions |
| `register_env_unset(var)` | Unset env var in Claude sessions |
| `register_disallowed_tool(name)` | Disable built-in CC tool via `--disallowedTools` |
| `add_session_customizer(cb)` | Register per-session customization callback (called per-prompt) |

Session status: `IDLE → BUSY → IDLE` / `→ STOPPED` / `→ DEAD`. `context: dict` carries extension-defined routing data (e.g. `{"chat_id": ...}`).

### Per-Session Customization

Extensions can register **session customizers** — synchronous callbacks that receive a `Session` and return `SessionOverrides` (or `None`). Customizers are called before every prompt execution (not just session creation), so they must be fast, synchronous, and side-effect-free.

```python
@dataclass
class SessionOverrides:
    extra_system_prompt: list[str] | None = None       # Appended to global system prompt
    exclude_mcp_servers: set[str] | None = None        # Removed from global MCP registry
    extra_mcp_servers: dict[str, dict] | None = None   # Added (last-wins on key conflict)
    extra_disallowed_tools: list[str] | None = None    # Appended to disallowed list
    extra_env_unset: list[str] | None = None           # Appended to unset list
```

Semantic rules: `exclude` only removes from the global registry, never from another customizer's `extra_mcp_servers`. Multiple customizers' results are merged in registration order.

### Delivery Metadata

| Field | Meaning |
|-------|---------|
| `is_stream` | Intermediate streaming event (`stream_type`: `"text"` or `"tool_use"`) |
| `is_final` | Task complete (includes `total_cost_usd`, `claude_session_id`) |
| `is_stopped` | Task interrupted by /stop |
| `is_error` | Error occurred |
| `is_heartbeat` | Liveness signal (30s no activity) |

### Bridge RPC

MCP child processes call main process via Unix socket. Line-delimited JSON. `BridgeClient.call(method, params, timeout)`. Extensions add handlers via `engine.bridge.add_handler(fn)`.

### MCP Server Base

Subclass `MCPServerBase`, set `name`, `tools`, `handlers`. Gets session context via env vars (`CLAUDE_EXT_SESSION_ID`, `CLAUDE_EXT_STATE_DIR`, `CLAUDE_EXT_USER_ID`). Lazy `bridge` property for RPC calls. `session_user_id` property for per-user logic.

## Extensions Summary

| Extension | MCP Tools | Communication |
|-----------|-----------|---------------|
| **vault** | `vault_store`, `vault_list`, `vault_retrieve`, `vault_delete` | Bridge RPC (passphrase never in MCP process) |
| **memory** | `memory_read`, `memory_write`, `memory_append`, `memory_search`, `memory_list` | Direct file I/O (no bridge needed) |
| **heartbeat** | `heartbeat_instructions`, `heartbeat_status`, `heartbeat_trigger`, `heartbeat_get_trigger_command` | Mixed (file I/O + bridge for trigger) |
| **cron** | `cron_create`, `cron_delete`, `cron_status` | Bridge RPC |
| **ask_user** | `ask_user` | Bridge RPC → PendingStore |
| **subagent** | `subagent_spawn`, `subagent_wait`, `subagent_status`, `subagent_send`, `subagent_stop`, `subagent_diff`, `subagent_merge` | Bridge RPC → PendingStore + SessionManager |
| **telegram** | (none — frontend only) | Delivery callbacks |

## Adding a New Extension

1. Create `extensions/<name>/extension.py` with `ExtensionImpl(Extension)`
2. Implement `start()` and `stop()`
3. Add to `enabled` list in `config.yaml`

No core changes, no other extension changes required.

## Decoupling Design Principles

Hard rules for all contributions:

1. **Core never imports any extension.** Discovery is via `importlib` only.
2. **Extensions never depend on each other.** Share data through `engine.services`, `engine.session_manager`, `engine.pending`.
3. **Each extension is a self-contained directory.** Delete directory + remove from `enabled` = zero impact.
4. **New features = new directories.** If adding an extension requires modifying `core/` or other extensions, the abstraction is leaking.
5. **Core services are generic.** Session uses `user_id: str` + `context: dict`, not platform-specific fields.
6. **Configuration is declarative.** Behavior controlled by `config.yaml`, not hardcoded.

## Configuration

```yaml
engine:
  max_turns: 0                        # 0 = unlimited
  permission_mode: bypassPermissions  # Required for -p mode tool execution
state_dir: ~/.claude-ext
sessions:
  max_sessions_per_user: 5

enabled:
  - vault       # Encrypted credential store
  - memory      # Cross-session memory
  - telegram    # Telegram bot frontend
  # - heartbeat # Autonomous periodic agent
  # - ask_user  # Interactive questions
  # - cron      # Scheduled tasks
  # - subagent  # Multi-agent orchestration

extensions:
  vault: {}           # Zero-config (passphrase auto-generated)
  memory: {}          # Zero-config (files in state_dir/memory/)
  telegram:
    token: "BOT_TOKEN"
    allowed_users: [123456789]
    working_dir: null
  heartbeat:
    interval: 1800
    max_daily_runs: 48
    usage_throttle: 80
    usage_pause: 95
    user_id: "123456789"
    notify_context: { chat_id: 123456789 }
  cron:
    jobs: []          # Static jobs; Claude also creates dynamically via MCP
  subagent:
    max_subagents_per_session: 5
    default_paradigm: coder
    cleanup_delay: 120.0
```

**`config.yaml` is gitignored** (contains secrets). Use `config.yaml.example` as template.

## Running

```bash
source .venv/bin/activate
cp config.yaml.example config.yaml  # First run only
python main.py
```

Requirements: Python 3.12+, tmux 3.x+, Claude Code CLI (`claude auth login`).

## Known Limitations

- `script -qfec` PTY may inject header line / ANSI escapes in `stream.jsonl` (parser skips non-JSON)
- OAuth token in `~/.claude/.credentials.json` may expire for direct API calls
- No global session cap (only per-user `max_sessions_per_user`)
- MCP server processes share state files via flock (brief contention under high writes)
