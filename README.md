English | [中文](README.zh-CN.md)

# claude-ext

[![CI](https://github.com/claudebot101001/claude-ext/actions/workflows/ci.yml/badge.svg)](https://github.com/claudebot101001/claude-ext/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)

Extensible framework for [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code). Claude Code is already a complete AI coding agent — this framework wraps the CLI and manages independent extensions, nothing more.

## Why claude-ext

### The subscription advantage

claude-ext invokes the official `claude` binary (`claude -p`) under the hood. This means it runs as a first-party Claude Code session, authenticated via your existing Claude subscription (Free, Pro, or Max plan OAuth).

Other approaches — the [Agent SDK](https://platform.claude.com/docs/en/agent-sdk/overview), [OpenClaw](https://github.com/anthropics/openclaw), or any third-party tool calling the API directly — must use API key authentication with pay-per-token billing.

For heavy agent workloads, this is a significant cost difference. A Max plan gives you a flat monthly rate, while API usage for similar workloads can easily exceed $3,000/month depending on volume.

| | claude-ext | Agent SDK | OpenClaw |
|---|---|---|---|
| **Authentication** | Subscription OAuth (via CLI) | API key | API key |
| **Billing model** | Flat monthly plan | Pay-per-token | Pay-per-token |
| **How it works** | Wraps `claude -p` binary | Direct API calls | Direct API calls |

### How it stays compliant

claude-ext does not extract, store, or proxy OAuth tokens. It spawns the official `claude` CLI binary as a subprocess — the same binary you'd run in your terminal. The token handling is entirely within Anthropic's own code.

Anthropic's [legal and compliance documentation](https://code.claude.com/docs/en/legal-and-compliance) states:

> **OAuth authentication** (used with Free, Pro, and Max plans) is intended exclusively for Claude Code and Claude.ai. Using OAuth tokens obtained through Claude Free, Pro, or Max accounts in any other product, tool, or service — including the Agent SDK — is not permitted and constitutes a violation of the Consumer Terms of Service.

claude-ext delegates to Claude Code itself, which is exactly what OAuth tokens are intended for.

> **Disclaimer**: Anthropic's terms and policies may change at any time. Always check the [latest terms](https://code.claude.com/docs/en/legal-and-compliance) to confirm current policy.

## How It Works

```
User / Frontend (e.g. Telegram)
        │
        ▼
   Extensions ──── config.yaml
        │
        ▼
   ClaudeEngine
   SessionManager
   Bridge RPC
        │
        ▼
   tmux sessions ──── MCP servers (per-session)
        │
        ▼
   claude -p ──── file IPC (prompt → stream → result)
```

Each Claude Code session runs in its own tmux session, fully decoupled from the main process. Extensions communicate with sessions through file-based IPC and receive results via async delivery callbacks.

**Key properties:**

- **Crash-resilient** — tmux sessions survive main process restarts; recovery is automatic
- **Multi-user** — per-user session slots with independent queues
- **Dynamic extensions** — add/remove extensions without touching core or other extensions
- **Bridge RPC** — MCP child processes call the main process via Unix socket (line-delimited JSON)
- **Structured events** — JSONL event log with rotation for observability
- **Usage-aware** — extensions can query auth status and API usage for cost control

## Comparison

| | claude-ext | Agent SDK | OpenClaw |
|---|---|---|---|
| **Authentication** | Subscription OAuth (via CLI) | API key | API key |
| **Billing** | Flat monthly plan | Pay-per-token | Pay-per-token |
| **Runtime** | tmux + `claude -p` subprocess | In-process API calls | In-process API calls |
| **Language** | Python | TypeScript / Python | Python |
| **Multi-session** | Yes (per-user slots, queue) | Manual | Manual |
| **Tool system** | MCP servers (per-session) | Native tool_use | Skills (5700+) |
| **Crash recovery** | Automatic (tmux survives) | Application-level | Application-level |
| **Model support** | Claude only (via CLI) | Claude only (via API) | 12+ providers |

**When to use what:**

- **claude-ext** — You want autonomous Claude agents on subscription pricing, with multi-session management, and Claude Code's full built-in toolset (file editing, bash, etc.)
- **Agent SDK** — You're building a product with Claude and need direct API control, custom tool definitions, and tight integration
- **OpenClaw** — You need multi-provider support, a large skill library, or cross-platform frontends (Discord, Slack, Web, etc.)

These are complementary tools for different use cases.

## Extensions

| Extension | Description | Status |
|-----------|------------|--------|
| **telegram** | Telegram bot bridge. Multi-session management, streaming output, inline commands | Stable |
| **vault** | Encrypted credential store (Fernet + PBKDF2). MCP tools for Claude to store/retrieve secrets | Stable |
| **memory** | Cross-session persistent memory. Markdown files with MCP tools for read/write/search | Stable |
| **heartbeat** | Autonomous periodic agent. Dual-channel scheduler + LLM decision + usage-aware cost control | Stable |
| **cron** | Scheduled task execution. Static config + dynamic MCP-created jobs | Stable |
| **ask_user** | Interactive questions from Claude to user during session execution | Stable |

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

- [Technical Reference](CLAUDE.md) — development guide and core APIs
- [Architecture Deep Dive](docs/ARCHITECTURE.md) — full implementation details
- [Roadmap](ROADMAP.md) — completed phases and planned features
- [Contributing](CONTRIBUTING.md) — development setup, code style, PR process
- [Security](SECURITY.md) — vulnerability reporting, security model

## License

[MIT](LICENSE)
