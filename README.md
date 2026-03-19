# claude-ext

Extensible framework for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI. Wraps `claude -p` invocations and manages extension lifecycles.

## What This Is

claude-ext turns Claude Code into a persistent, multi-session agent platform. It runs as a background service, managing Claude sessions in tmux windows and exposing extension capabilities via MCP (Model Context Protocol).

**Core loop:** User message → create/reuse session → `claude -p` in tmux → stream output → deliver result.

## Architecture

```
┌─────────────────────────────────────────────────┐
│  Main Process (asyncio)                         │
│  ┌──────────┐ ┌──────────┐ ┌─────────────────┐ │
│  │  Engine   │ │  Bridge  │ │ SessionManager  │ │
│  │          ◄──►  Server  ◄──►  (tmux + MCP)  │ │
│  └──────────┘ └────▲─────┘ └─────────────────┘ │
│                    │ Unix Socket                │
│  ┌─────────────────┼───────────────────┐        │
│  │  Extensions     │                   │        │
│  │  vault ─────────┤ (bridge handler)  │        │
│  │  cron           │                   │        │
│  │  ask_user ──────┤                   │        │
│  │  telegram       │ (frontend)        │        │
│  └─────────────────┴───────────────────┘        │
└─────────────────────────────────────────────────┘
         │                    │
    ┌────▼────┐         ┌────▼────┐
    │  tmux   │         │  tmux   │
    │ session │         │ session │
    │claude -p│         │claude -p│
    │ ┌─────┐ │         │ ┌─────┐ │
    │ │ MCP │ │         │ │ MCP │ │
    │ │srvrs│ │         │ │srvrs│ │
    │ └──┬──┘ │         │ └──┬──┘ │
    └────┼────┘         └────┼────┘
         │                    │
         └──── bridge.sock ───┘
```

### Key Design Decisions

1. **Core never imports extensions.** Extensions are discovered via `importlib` at startup.
2. **Extensions never import each other.** Cross-extension communication goes through the Bridge.
3. **MCP servers run as child processes.** Claude Code requires stdio-based MCP. Each session spawns its own MCP server processes.
4. **Bridge RPC mediates everything.** A Unix socket server in the main process lets MCP child processes call back for resources they can't hold (encryption keys, session management, cross-extension data).

### The Bridge Pattern

MCP servers are isolated child processes — they can't access the main process's memory. The Bridge solves this:

```
MCP Server (child)  ──Unix Socket──►  BridgeServer (main process)
                                         │
                                    Handler Chain
                               (first non-None wins)
```

Each extension registers handlers for its method prefix (e.g., `vault_*`). Any MCP server can call any handler by method name, without knowing which extension owns it. Additionally, `dispatch()` lets the main process itself call the same handler chain for in-process cross-extension coordination.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the complete technical reference.

## Quick Start

### Prerequisites

- Python 3.12+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated
- tmux

### Setup

```bash
git clone https://github.com/anthropics/claude-ext.git
cd claude-ext
pip install -r requirements.txt
cp config.yaml.example config.yaml
# Edit config.yaml — add your Telegram bot token at minimum
```

### Run

```bash
# Via systemd (recommended):
# Copy the systemd unit, enable, start

# Or directly for testing:
python main.py
```

### Configuration

See `config.yaml.example` for all options. Minimal config:

```yaml
engine:
  permission_mode: bypassPermissions

enabled:
  - vault
  - cron
  - ask_user
  - telegram

extensions:
  telegram:
    token: "YOUR_BOT_TOKEN"
    allowed_users: [YOUR_TELEGRAM_ID]
```

## Included Extensions

| Extension | Type | MCP Tools | Purpose |
|-----------|------|:---------:|---------|
| **vault** | Bridge proxy | 3 | Encrypted credential store. Passphrase stays in main process; MCP server calls via bridge. |
| **cron** | Self-contained | 3 | Scheduled tasks via cron expressions. No bridge dependency — manages its own file store. |
| **ask_user** | Bridge proxy | 1 | Interactive questions from Claude to user, routed through the frontend. |
| **telegram** | Frontend | 0 | Telegram bot interface. Multi-session, streaming responses, inline commands. |

These demonstrate the three extension patterns:
- **Bridge proxy** (vault, ask_user): All logic in main process, MCP server is a thin RPC shell
- **Self-contained** (cron): MCP server handles everything locally, zero bridge calls
- **Frontend** (telegram): No MCP server, drives sessions from external input

## Adding Extensions

```
extensions/my-ext/
├── extension.py      # ExtensionImpl(Extension) — lifecycle
├── mcp_server.py     # MCPServerBase subclass (optional)
└── templates/        # YAML + MD agent templates (optional)
```

1. Create `extensions/<name>/extension.py` with `ExtensionImpl(Extension)`
2. Implement `start()` and `stop()`
3. Add to `enabled` list in `config.yaml`
4. (Optional) Add MCP server, bridge handlers, templates

No core changes required. See the included extensions for reference implementations.

## Tests

```bash
pytest tests/ -v
```

## License

MIT
