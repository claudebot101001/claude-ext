"""Extension base class - the only contract extensions must follow."""

from abc import ABC, abstractmethod

from core.engine import ClaudeEngine


class Extension(ABC):
    """All extensions implement this interface. Nothing more."""

    name: str = "unnamed"

    def configure(self, engine: ClaudeEngine, config: dict) -> None:
        """Receive engine and extension-specific config. Called once at startup."""
        self.engine = engine
        self.config = config

    @abstractmethod
    async def start(self) -> None:
        """Start the extension (e.g. begin polling, open webhook)."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Gracefully stop the extension."""
        ...
