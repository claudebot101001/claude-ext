"""Smoke tests: verify all extensions and MCP servers are importable."""

from pathlib import Path

import pytest

EXTENSIONS_DIR = Path(__file__).parent.parent / "extensions"

# Discover all extension directories that have extension.py
_ext_dirs = sorted(
    d.name for d in EXTENSIONS_DIR.iterdir() if d.is_dir() and (d / "extension.py").exists()
)

# Discover all extension directories that have mcp_server.py
_mcp_dirs = sorted(
    d.name for d in EXTENSIONS_DIR.iterdir() if d.is_dir() and (d / "mcp_server.py").exists()
)


@pytest.mark.parametrize("ext_name", _ext_dirs)
def test_extension_importable(ext_name):
    """Each extension module must import without error and expose ExtensionImpl."""
    import importlib

    mod = importlib.import_module(f"extensions.{ext_name}.extension")
    assert hasattr(mod, "ExtensionImpl"), f"extensions.{ext_name}.extension missing ExtensionImpl"


@pytest.mark.parametrize("ext_name", _mcp_dirs)
def test_mcp_server_importable(ext_name):
    """Each MCP server module must import without error."""
    import importlib

    importlib.import_module(f"extensions.{ext_name}.mcp_server")
