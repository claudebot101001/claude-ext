# claude-ext

Extensible framework for Claude Code CLI. Wraps CLI invocations + manages extension lifecycles.

Deep implementation reference: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Quick Reference

```
claude-ext/
├── core/               # Stable layer: engine, sessions, bridge, templates, registry
│   ├── templates/      # Built-in YAML + MD template files (coder, reviewer, researcher)
│   └── *.py            # Core modules (never import extensions)
├── extensions/         # Each subdirectory is a self-contained extension
│   └── <ext>/
│       ├── extension.py      # ExtensionImpl(Extension) — entry point
│       ├── mcp_server.py     # MCP stdio server (optional)
│       └── templates/        # Extension-owned YAML + MD templates (optional)
├── config.yaml         # Runtime config (.gitignored, contains secrets)
├── config.yaml.example # Config template with all options documented
└── main.py             # Entry point
```

**Pre-commit:** Always `ruff format <file>` before commit.

## Design Principles

1. **Core never imports any extension.** Discovery via `importlib` only.
2. **Extensions never import each other.** Use `dependencies` + `engine.services` for runtime access.
3. **Each extension is self-contained.** Delete directory + remove from `enabled` = zero impact.
4. **New features = new directories.** Modifying `core/` or other extensions = abstraction leak.
5. **MCP access is fail-closed.** Templates use allowlists. New extensions auto-excluded from restricted templates.

## Adding a New Extension

1. Create `extensions/<name>/extension.py` with `ExtensionImpl(Extension)`
2. Implement `start()` and `stop()`
3. Add to `enabled` list in `config.yaml`
4. (Optional) Add MCP server: create `mcp_server.py` subclassing `MCPServerBase`, register in `start()`
5. (Optional) Add templates: create `templates/*.yaml` + `.md` files, register in `start()`

No core changes, no other extension changes required.
