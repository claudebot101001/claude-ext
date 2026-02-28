# Contributing to claude-ext

## Development Setup

```bash
git clone https://github.com/YOUR_USERNAME/claude-ext.git
cd claude-ext
python -m venv .venv
source .venv/bin/activate
pip install -e ".[all,dev]"
```

## Running Tests

```bash
pytest -v
```

All 239 tests should pass. Tests cover core stores, MCP handlers, extension lifecycle, and bridge communication.

## Code Style

This project uses [ruff](https://docs.astral.sh/ruff/) for linting and formatting:

```bash
ruff check .        # lint
ruff format .       # format
```

CI enforces both. Configuration is in `pyproject.toml` (line length 100, Python 3.12 target).

## Adding a New Extension

1. Create `extensions/<name>/` with at minimum:
   - `__init__.py` (empty)
   - `extension.py` containing an `ExtensionImpl` class that inherits from `core.extension.Extension`
   - `requirements.txt` (optional, for extension-specific dependencies)

2. Implement the required interface:
   ```python
   from core.extension import Extension

   class ExtensionImpl(Extension):
       name = "my_extension"

       async def start(self) -> None:
           # Register callbacks, MCP servers, services, etc.
           ...

       async def stop(self) -> None:
           # Clean up
           ...
   ```

3. Add to `config.yaml`:
   ```yaml
   enabled:
     - my_extension

   extensions:
     my_extension:
       key: value
   ```

## Design Principles

These are hard rules for all contributions:

1. **Core never imports any extension.** Extension discovery is purely dynamic via `importlib`.
2. **Extensions never depend on each other.** Use `engine.services` for cross-extension communication.
3. **Each extension is a self-contained directory.** Deleting it + removing from `enabled` = zero impact.
4. **New features = new directories, no changes to existing code.** If adding an extension requires modifying `core/` or other extensions, the abstraction is leaking.
5. **Configuration is declarative.** Extension behavior is controlled by `config.yaml`, not hardcoded.

## Pull Requests

- One logical change per PR
- Include tests for new functionality
- Ensure `ruff check .` and `ruff format --check .` pass
- Ensure `pytest -v` passes
- Follow existing code patterns and conventions
