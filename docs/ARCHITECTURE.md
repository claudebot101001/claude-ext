# Architecture Reference

## Process Model

claude-ext runs as a single asyncio main process. Claude Code sessions run in tmux windows as separate processes. MCP servers are stdio child processes spawned per-session by Claude Code.

```
Main Process (asyncio, persistent)
  ├── Engine              — top-level coordinator
  ├── BridgeServer        — Unix socket RPC server
  ├── SessionManager      — tmux session lifecycle
  ├── TemplateRegistry    — agent blueprint management
  └── Extensions          — plugin lifecycle (start/stop)

Per-Session (ephemeral, in tmux)
  └── claude -p           — Claude Code CLI
       ├── MCP Server A   — stdio child process
       ├── MCP Server B   — stdio child process
       └── ...
```

## Session Lifecycle

Each session is a tmux window running `claude -p`. File-based IPC in `~/.claude-ext/sessions/{uuid}/`.

**Prompt execution flow** (`session.py`):
1. `send_prompt()` → enqueue to per-session `asyncio.Queue`
2. `_queue_worker()` → pulls prompts sequentially, calls `_execute_prompt()`
3. `_execute_prompt()`:
   - Clean artifacts from previous prompt
   - Write `prompt.txt`
   - Run pre-prompt hooks (async, I/O-heavy)
   - Collect customizer overrides (sync, fast)
   - Generate `claude_cmd.sh` (CLI invocation) + `run.sh` (PTY wrapper)
   - `tmux send-keys` to execute
   - Stream `stream.jsonl` incrementally, deliver events via callbacks
   - On completion: mark IDLE, capture metadata, deliver final result

**States:** `IDLE → BUSY → IDLE` / `→ STOPPED` (user stop) / `→ DEAD` (tmux died).

**Session files:**

| File | Purpose |
|------|---------|
| `state.json` | Session metadata (atomic write: temp → rename) |
| `prompt.txt` | Current prompt content |
| `claude_cmd.sh` | Inner CLI command with all flags |
| `run.sh` | PTY wrapper (`script -qfec`) |
| `stream.jsonl` | Streaming output (line-delimited JSON) |
| `exitcode` | Existence = prompt done, contents = exit code |
| `mcp_config.json` | Per-session MCP server config |

## Bridge RPC

Unix socket (`~/.claude-ext/bridge.sock`) for MCP child → main process communication.

### Why It Exists

MCP servers are isolated child processes (required by Claude Code's stdio protocol). They cannot access:
- Encryption keys (vault passphrase)
- Database connections
- Session management APIs
- Other extensions' capabilities

The Bridge is the sole channel for child processes to call back into the main process.

### Protocol

Line-delimited JSON over Unix domain socket:

```
Client → Server:  {"method": "vault_store", "params": {"key": "api/gh/token", "value": "ghp_xxx"}}\n
Server → Client:  {"result": {"status": "stored"}}\n
```

### Server (main process)

`BridgeServer` maintains a **handler chain** — an ordered list of async handlers. On each request:
1. Iterate handlers
2. First non-None response wins
3. All None → error response

Extensions register handlers in `start()`:
```python
self.engine.bridge.add_handler(self._bridge_handler)
```

### Client (MCP child process)

`BridgeClient` is synchronous and blocking (MCP servers are sync). Features:
- **Inode-based reconnection**: Detects when `bridge.sock` is replaced (main process restart) and auto-reconnects
- **Transparent retry**: On broken pipe, reconnects and resends once
- **Lazy initialization**: `MCPServerBase.bridge` property connects on first use

### In-Process Dispatch

`BridgeServer.dispatch()` lets main-process code call the same handler chain without going through the socket. Used when Extension A needs to call Extension B's bridge handlers from within the main process.

```python
# From an extension in the main process:
result = await self.engine.bridge.dispatch("team_create", config)
```

## MCP Tool Servers

Extensions expose tools via `MCPServerBase` subclass (JSON-RPC over stdio).

```python
class MyMCPServer(MCPServerBase):
    name = "my_ext"
    tools = [{"name": "action1", "description": "...", "inputSchema": {...}}]
    def __init__(self):
        super().__init__()
        self.handlers = {"action1": self._handle_action1}
```

**Environment injected per-session:** `CLAUDE_EXT_SESSION_ID`, `CLAUDE_EXT_STATE_DIR`, `CLAUDE_EXT_USER_ID`, `CLAUDE_EXT_BRIDGE_SOCKET`, `CLAUDE_EXT_GATEWAY_MODE`.

**Gateway mode** (`engine.gateway_mode: true`): Servers with >1 tool consolidate into a single gateway tool. Claude calls `action='help'` for discovery, then `action='<command>'` with `params={...}`.

## Extension Lifecycle

**Discovery** (`registry.py`): Scans `extensions/*/extension.py`, loads `ExtensionImpl` via `importlib`.

**Dependency resolution:** Topological sort on `dependencies` (hard) + `soft_dependencies` (optional, load-order only).

**Startup sequence:**
1. For each extension in dependency order:
   - Snapshot runtime state
   - `await ext.start()`
   - On failure: restore snapshot → rollback

**Shutdown:** Reverse order (LIFO).

## Template System

Templates are agent blueprints: tool restrictions, system prompts, MCP access, model overrides.

**Three-layer load order:**
1. **Core** (`core/templates/`) — built-in: coder, reviewer, researcher
2. **Config** (`config.yaml → templates:`) — user overrides
3. **Extension** (`extensions/<ext>/templates/`) — registered in `start()`

**YAML file structure:**
```yaml
description: "Code review agent"
model: opus
disallowed_tools: [Write, Edit]
mcp_servers: [vault]           # Allowlist (fail-closed)
visibility: internal           # Hidden from user-facing lists
```

**MCP access is fail-closed:** `mcp_servers: [vault]` means ONLY vault is available. New extensions are automatically excluded from restricted templates unless explicitly listed.

## Per-Session Customization

Every prompt execution runs this chain:

1. **Pre-prompt hooks** (async): Heavy I/O prep
2. **Session customizers** (sync): Return `SessionOverrides` to adjust MCP, tools, prompts
3. **Override merge rules:**
   - `extra_system_prompt`: concatenate
   - `exclude_mcp_servers`: union
   - `extra_disallowed_tools`: union
   - `allowed_tools`: intersection (most restrictive wins)
   - `allowed_mcp_servers`: intersection (most restrictive wins)

## Design Principles

1. **Core never imports any extension.** Discovery via `importlib` only.
2. **Extensions never import each other.** Use `dependencies` + `engine.services`.
3. **Each extension is self-contained.** Delete directory + remove from config = zero impact.
4. **New features = new directories.** Modifying core = abstraction leak.
5. **MCP access is fail-closed.** Templates use allowlists.
