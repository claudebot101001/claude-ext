# claude-ext

[![CI](https://github.com/YOUR_USERNAME/claude-ext/actions/workflows/ci.yml/badge.svg)](https://github.com/YOUR_USERNAME/claude-ext/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)

Extensible framework for [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code). Claude Code is already a complete AI coding agent — this framework wraps the CLI and manages independent extensions, nothing more.

## Architecture

```
config.yaml ──▶ main.py ──▶ Registry ──▶ Extensions
                  │                        │
                  ▼                        ▼
            ClaudeEngine          tmux sessions (claude -p)
            SessionManager        MCP servers (per-session)
            Bridge RPC            file IPC (prompt → stream → result)
```

Each Claude Code session runs in its own tmux session, fully decoupled from the main process. Extensions communicate with sessions through file-based IPC and receive results via async delivery callbacks.

## Extensions

| Extension | Description | Status |
|-----------|------------|--------|
| **vault** | Encrypted credential store (Fernet + PBKDF2). MCP tools for Claude to store/retrieve secrets | Stable |
| **memory** | Cross-session persistent memory. Markdown files with MCP tools for read/write/search | Stable |
| **heartbeat** | Autonomous periodic agent. Dual-channel scheduler + LLM decision + usage-aware cost control | Stable |
| **cron** | Scheduled task execution. Static config + dynamic MCP-created jobs | Stable |
| **ask_user** | Interactive questions from Claude to user during session execution | Stable |
| **telegram** | Telegram bot bridge. Multi-session management, streaming output, inline commands | Stable |

## Quick Start

### Prerequisites

- Python 3.12+
- tmux 3.x+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated (`claude auth login`)

### Install

```bash
git clone https://github.com/YOUR_USERNAME/claude-ext.git
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

- [Technical Reference](CLAUDE.md) — full architecture, core APIs, extension details
- [Roadmap](ROADMAP.md) — completed phases and planned features
- [Contributing](CONTRIBUTING.md) — development setup, code style, PR process
- [Security](SECURITY.md) — vulnerability reporting, security model

## License

[MIT](LICENSE)
