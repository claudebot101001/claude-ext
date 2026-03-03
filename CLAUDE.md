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
│   ├── memory/              # Three-layer identity + knowledge store (Markdown + bridge RPC)
│   ├── heartbeat/           # Autonomous periodic agent (dual-channel + 3-tier)
│   ├── cron/                # Scheduled tasks (croniter + MCP)
│   ├── ask_user/            # Interactive questions (bridge + PendingStore)
│   ├── subagent/            # Multi-agent orchestration (PM → worker sessions)
│   ├── session_ask/         # Cross-session RPC (bridge + PendingStore)
│   ├── browser/             # Web browser automation (agent-browser CLI, thin CLI extension)
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
| `add_system_prompt(text, mcp_server=None)` | Append system prompt; optional tag for per-session filtering |
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
| **memory** | `memory_read`, `memory_write`, `memory_append`, `memory_search`, `memory_list`, `personality_read`, `personality_write`, `personality_append` | Direct file I/O + Bridge RPC (personality encryption via vault) |
| **heartbeat** | `heartbeat_instructions`, `heartbeat_status`, `heartbeat_trigger`, `heartbeat_get_trigger_command`, `heartbeat_dry_run`, `heartbeat_set_verification` | Mixed (file I/O + bridge for trigger) |
| **cron** | `cron_create`, `cron_delete`, `cron_status` | Bridge RPC |
| **ask_user** | `ask_user` | Bridge RPC → PendingStore |
| **subagent** | `subagent_spawn`, `subagent_wait`, `subagent_status`, `subagent_send`, `subagent_stop`, `subagent_diff`, `subagent_merge`, `subagent_delete`, `subagent_reclaim_respond`, `session_info` | Bridge RPC → PendingStore + SessionManager |
| **session_ask** | `session_ask`, `session_reply`, `session_list` | Bridge RPC → PendingStore + SessionManager |
| **browser** | (none — thin CLI extension) | System prompt only (Bash → agent-browser) |
| **telegram** | (none — frontend only) | Delivery callbacks |

## Memory Extension: Three-Layer Identity System

The memory extension implements an autonomous agent identity model with encrypted personality storage and per-user profiling.

### Storage Layout

```
~/.claude-ext/memory/
├── constitution.md          # Layer 1: human-authored, AI read-only
├── personality.md.enc       # Layer 2: Fernet-encrypted, AI-managed
├── TOPICS_INDEX.md          # Topic catalog (searched via FTS5, not force-loaded)
├── topics/                  # Deep knowledge per subject
│   └── backlog.md           # Self-improvement backlog
├── users/                   # Layer 3: per-user profiles
│   └── <user_id>/
│       └── profile.md       # User aspirations and demands
├── events/                  # Formative events linked from personality
│   └── <date>-<slug>.md
├── memory.lock
└── .search_index.db         # FTS5 cache (derived, rebuildable)
```

### Layer 1: Constitutional Rules

- **File**: `constitution.md` — human-authored, AI **cannot** modify via MCP tools
- **Injection**: Session customizer reads the file and injects into system prompt for every session
- **Enforcement**: `memory_write` and `memory_append` reject writes to `constitution.md`
- Seed template created on first run; only injected once the operator adds real rules

### Layer 2: Personality Principles

- **Storage**: `personality.md.enc` — Fernet-encrypted binary on disk
- **Encryption key**: Auto-generated, stored in Vault (`memory/personality/encryption_key`); held in main process memory at runtime
- **Access**: MCP tools (`personality_read`, `personality_write`, `personality_append`) → bridge RPC → main process decrypts/encrypts
- **Format**: One principle per line with hyperlinked formative event:
  ```
  - <principle> → [YYYY-MM-DD: description](events/YYYY-MM-DD-slug.md)
  ```
- **Security model**: Defense-in-depth. Encrypted at rest; key in Vault (itself encrypted). Not a hard isolation boundary — a human with filesystem access can extract the key from Vault.
- **Requires**: Vault extension enabled and loaded before memory. Without vault, personality tools return a clear error; all other memory features work normally.

### Layer 3: User Profiles

- **Storage**: `users/<user_id>/profile.md` — one file per user
- **Injection**: Session customizer reads profile based on `session.user_id` and injects into system prompt
- **Content style**: Aspiration/demand-oriented, not definition-based (e.g. "wants logically rigorous code" not "is a software engineer")

### Knowledge Store

- **TOPICS_INDEX.md**: Catalog of topic files. Enables FTS5/BM25 search via `memory_search`.
- **topics/\<name\>.md**: Deep knowledge files. Update `TOPICS_INDEX.md` when creating or modifying.
- **events/\<date\>-\<slug\>.md**: Verifiable experiences referenced from personality principles.

Knowledge is accessed on-demand via `memory_search`, not force-loaded at session start.

### Migration

On first start after upgrade, auto-migrates v1 format:
1. Generates `TOPICS_INDEX.md` from existing topic files
2. Archives `daily/` logs into `topics/daily-archive.md`
3. Seeds `constitution.md`, creates `users/` and `events/` directories
4. Writes `.migrated_v2` marker (idempotent)

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
  # - heartbeat    # Autonomous periodic agent
  # - ask_user     # Interactive questions
  # - cron         # Scheduled tasks
  # - subagent     # Multi-agent orchestration
  # - session_ask  # Cross-session RPC

extensions:
  vault: {}           # Zero-config (passphrase auto-generated)
  memory: {}          # Three-layer identity (requires vault before memory for personality encryption)
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

## Self-Improvement Backlog

Sessions working on claude-ext should record improvement ideas to `topics/backlog.md` (via MCP memory tools). This backlog is processed by the heartbeat during idle time.

**Recording an item:**
1. Before appending, read the current backlog (`memory_read('topics/backlog.md')`) to check for duplicates
2. Assess difficulty level:
   - `L1`: Non-core, small change, confident fix
   - `L2`: Non-core, large or uncertain impact
   - `L3`: Core, simple logic change
   - `L4`: Core complex, or new/refactored extension
3. Append under `## Pending` with format: `- [ ] [L<n>] <description>`

**Do NOT record**: vague wishes, already-tracked items, or items that belong in GitHub Issues instead.

## Known Limitations

- `script -qfec` PTY may inject header line / ANSI escapes in `stream.jsonl` (parser skips non-JSON)
- OAuth token in `~/.claude/.credentials.json` may expire for direct API calls
- No global session cap (only per-user `max_sessions_per_user`)
- MCP server processes share state files via flock (brief contention under high writes)
