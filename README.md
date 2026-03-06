English | [中文](README.zh-CN.md)

# claude-ext

[![CI](https://github.com/claudebot101001/claude-ext/actions/workflows/ci.yml/badge.svg)](https://github.com/claudebot101001/claude-ext/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)

Server-side framework that turns [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) into a programmable, always-on, multi-session agent platform. It wraps the official `claude` binary and manages independent extensions — nothing more.

> **27k LoC** | **11 extensions** | **782 tests** | **Pure Python, zero framework dependencies**

## Why claude-ext

### Subscription-based agent platform

claude-ext invokes `claude -p` under the hood, running as a first-party Claude Code session authenticated via your existing subscription (Free, Pro, or Max plan OAuth). Other approaches — the [Agent SDK](https://platform.claude.com/docs/en/agent-sdk/overview), custom API integrations, or third-party wrappers — must use API key authentication with pay-per-token billing.

For heavy agent workloads (autonomous heartbeat, multi-agent orchestration, scheduled tasks), this is a significant cost difference. A Max plan gives a flat monthly rate; equivalent API usage can easily exceed $3,000/month.

### Integrated infrastructure, not just a wrapper

Most Claude Code extension projects focus on a single concern — session management, Telegram bridging, or cron scheduling. claude-ext provides a **complete server-side infrastructure layer**:

| Capability | What it does |
|---|---|
| **Multi-session management** | tmux-backed sessions with per-user slots, prompt queuing, crash recovery |
| **MCP server injection** | Per-session MCP servers with dynamic tool registration and session customizers |
| **Bridge RPC** | Bidirectional Unix socket IPC between MCP child processes and main process |
| **Gateway mode** | Multi-tool MCP servers consolidated to 1 gateway tool (98% token reduction) |
| **Extension lifecycle** | Dynamic discovery, hot-reload (SIGHUP), health checks, strict decoupling |
| **Delivery callbacks** | Real-time streaming with structured metadata (cost, status, tool use) |
| **Pending store** | Generic async request/response registry for cross-process coordination |

These core primitives power 11 independent extensions that can be enabled/disabled via config.

### How it stays compliant

claude-ext does not extract, store, or proxy OAuth tokens. It spawns the official `claude` CLI binary as a subprocess — the same binary you'd run in your terminal. Token handling is entirely within Anthropic's own code.

> **Disclaimer**: Anthropic's terms and policies may change at any time. Check the [latest terms](https://code.claude.com/docs/en/legal-and-compliance) to confirm current policy.

## Architecture

```
User / Frontend (Telegram, CLI, ...)
        │
        ▼
   Extensions ──── config.yaml
        │
        ▼
   ClaudeEngine ─── Bridge RPC (Unix socket)
   SessionManager   PendingStore
   EventLog         Service Registry
        │
        ▼
   tmux sessions ──── MCP servers (per-session, gateway mode)
        │
        ▼
   claude -p ──── file IPC (prompt → stream.jsonl → result)
```

**Key properties:**

- **Crash-resilient** — tmux sessions survive main process restarts; automatic recovery on startup
- **Multi-user** — per-user session slots with independent prompt queues
- **Dynamic extensions** — add/remove via `config.yaml`; no core or cross-extension changes
- **Session customizers** — per-prompt MCP/system-prompt/tool modification via `SessionOverrides`
- **SIGHUP reload** — update config without restart; extensions receive `reconfigure()` callback
- **Usage-aware** — query auth status and API usage quotas for cost control
- **Structured events** — JSONL event log with rotation for observability

## Extensions

| Extension | MCP Tools | Description |
|-----------|:---------:|-------------|
| **vault** | 4 | Encrypted credential store (Fernet + PBKDF2). Passphrase never leaves main process |
| **memory** | 8 | Three-layer identity (constitution + encrypted personality + per-user profiles) + knowledge store with FTS5 search |
| **heartbeat** | 7 | Autonomous periodic agent. 3-tier execution (gate → LLM decision → session) with 5 safety valves |
| **cron** | 3 | Scheduled task execution. Cron expressions + one-time delays, static or MCP-created |
| **ask_user** | 1 | Interactive questions from Claude to user, with optional choice buttons |
| **subagent** | 10 | Multi-agent orchestration. PM spawns worker sessions with paradigms + git worktree isolation |
| **session_ask** | 3 | Cross-session RPC. Sessions ask questions to each other and wait for replies |
| **context** | 3 | Context window monitoring + auto-compaction. Per-session token tracking via delivery callbacks |
| **browser** | 3+25 | Web automation (agent-browser CLI) + anti-bot scraping (Scrapling) + stealth browsing (Patchright anti-detect + CAPTCHA solver) |
| **crypto** | 11 | On-chain wallet management. Multi-chain EVM, token transfers, contract deploy/call, EIP-191 signing, x402 payment protocol |
| **telegram** | 0 | Telegram bot frontend. Multi-session, streaming output, inline commands |

All extensions are fully independent — delete the directory + remove from `enabled` = zero impact.

## Comparison

### vs. OpenClaw / NanoClaw

These are the two major open-source "Claw" frameworks. claude-ext takes a fundamentally different architectural approach:

| | claude-ext | [OpenClaw](https://github.com/openclaw/openclaw) (257k stars) | [NanoClaw](https://github.com/qwibitai/nanoclaw) (18k stars) |
|---|---|---|---|
| **Codebase** | ~27k LoC (Python) | ~800k LoC (TypeScript) | ~3.9k LoC (TypeScript) |
| **Architecture** | Plugin-based core + extensions | Monolithic Gateway | Minimal host + containers |
| **CLI wrapping** | `claude -p` via tmux | SDK-native (no CLI) | Claude Agent SDK in containers |
| **IPC** | Unix socket bridge RPC (0.06ms) | WebSocket RPC | Filesystem polling (1s interval) |
| **MCP integration** | Per-session injection + gateway mode | MCP Registry + skill injection | Single MCP server per container |
| **Session customization** | Per-prompt `SessionOverrides` | Per-agent config files | Per-group CLAUDE.md |
| **Memory** | 3-layer encrypted + FTS5 | File-based (MEMORY.md) | 2-level CLAUDE.md |
| **Secrets** | Fernet vault + bridge isolation | SecretRef in config | Mount validation filtering |
| **Extension model** | `start()/stop()` lifecycle + engine services | Skills + Channel + Provider plugins | Skills (Markdown) + channel registry |
| **Messaging** | Telegram (via extension) | 22+ platforms (built-in) | 5 platforms (built-in) |
| **Isolation** | tmux + env unset + tool disallow | Loopback + optional Docker | Container-first (Docker) |

**Key differentiators**:

- **CLI wrapping vs SDK integration**: claude-ext wraps `claude -p`, inheriting the full Claude Code feature set (permission modes, built-in tools, MCP client). OpenClaw and NanoClaw use SDK-native integration, requiring them to reimplement features that Claude Code provides for free.
- **Per-session MCP injection**: claude-ext registers different MCP server configurations per session, with customizers that dynamically include/exclude servers per prompt. Neither Claw project offers this granularity.
- **Gateway mode**: 98% token reduction for multi-tool MCP servers (e.g. Scrapling: 5,640 → 120 tokens). No equivalent in either Claw project.
- **Bridge RPC**: Unix socket with 0.06ms latency, purpose-built for MCP↔main-process communication. OpenClaw uses a general-purpose WebSocket; NanoClaw polls filesystem at 1s intervals.
- **Encrypted vault with process isolation**: Passphrase held only in main process memory; MCP servers access via bridge RPC. Neither Claw project has comparable credential isolation.

**Trade-offs**: OpenClaw has vastly more messaging platform coverage (22+ vs 1) and a community skill marketplace (13k+ skills). NanoClaw provides stronger container-level isolation. claude-ext is designed for depth of infrastructure, not breadth of integrations.

### vs. Agent SDK / API-based tools

| | claude-ext | Agent SDK | API wrappers |
|---|---|---|---|
| **Billing** | Flat subscription | Pay-per-token | Pay-per-token |
| **Runtime** | tmux + `claude -p` | In-process API | In-process API |
| **Multi-session** | Built-in (per-user slots, queue) | Manual | Manual |
| **Tool system** | MCP servers (per-session injection) | Native tool_use | Varies |
| **Crash recovery** | Automatic (tmux survives) | Application-level | Application-level |
| **Model support** | Claude only (via CLI) | Claude only (via API) | Often multi-provider |

### vs. Multi-agent orchestrators

Orchestrators like [CrewAI](https://github.com/crewAIInc/crewAI) (45k stars), [AutoGen](https://github.com/microsoft/autogen) (55k stars), and [LangGraph](https://github.com/langchain-ai/langgraph) (25k stars) coordinate agents at the API/SDK level. claude-ext provides the **infrastructure layer** beneath orchestration:

| Capability | claude-ext | Typical orchestrators |
|---|---|---|
| **Session lifecycle** | Managed (create → queue → execute → recover) | Spawn and forget |
| **MCP injection** | Per-session, dynamic, with gateway consolidation | None or static |
| **Bridge RPC** | Bidirectional (MCP ↔ main process) | None |
| **Credential vault** | Built-in (encrypted, bridge-isolated) | None |
| **Autonomous heartbeat** | 3-tier with usage throttling | Basic cron at best |
| **Extension system** | Decoupled lifecycle with health checks | Often monolithic |

### vs. Claude Code plugins

[Claude Code plugins](https://docs.anthropic.com/en/docs/claude-code) extend a single session. claude-ext manages **multiple concurrent sessions** with server-side infrastructure (persistent state, credential vault, autonomous scheduling, cross-session coordination). They are complementary: claude-ext sessions can use Claude Code plugins.

**When to use what:**

- **claude-ext** — You want always-on autonomous agents on subscription pricing, with multi-session management and server-side infrastructure
- **Agent SDK** — You're building a product with Claude and need direct API control and tight integration
- **CC plugins** — You want to extend a single Claude Code session with custom tools and skills

## Quick Start

### Prerequisites

- Python 3.12+
- tmux 3.x+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated (`claude auth login`)

### Install

```bash
git clone https://github.com/claudebot101001/claude-ext.git
cd claude-ext
python -m venv .venv
source .venv/bin/activate
pip install -e ".[all]"
```

### Configure

```bash
cp config.yaml.example config.yaml
# Edit config.yaml with your settings (bot tokens, user IDs, etc.)
```

### Run

```bash
python main.py
```

## Adding an Extension

Create a new directory under `extensions/` with an `extension.py`:

```python
from core.extension import Extension

class ExtensionImpl(Extension):
    name = "my_extension"

    async def start(self) -> None:
        self.engine.session_manager.add_delivery_callback(self._deliver)

    async def stop(self) -> None:
        pass
```

Then add it to the `enabled` list in `config.yaml`. No changes to core or other extensions required.

## Documentation

- [Architecture Deep Dive](docs/ARCHITECTURE.md) — full implementation details, core APIs, extension reference
- [Technical Reference](CLAUDE.md) — quick development guide
- [Roadmap](ROADMAP.md) — completed phases and planned features
- [Contributing](CONTRIBUTING.md) — development setup, code style, PR process
- [Security](SECURITY.md) — vulnerability reporting, security model

## License

[MIT](LICENSE)
