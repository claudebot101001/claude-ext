"""Chain adapter abstract base class."""

from abc import ABC, abstractmethod
from typing import Any


class ChainAdapter(ABC):
    """Interface for blockchain operations. One implementation per chain family."""

    @abstractmethod
    async def generate_wallet(self) -> tuple[str, str]:
        """Generate a new wallet. Returns (address, private_key)."""

    @abstractmethod
    async def get_balance(self, address: str) -> dict:
        """Get native token balance. Returns {balance, symbol, wei}."""

    @abstractmethod
    async def get_token_balance(self, address: str, token: str) -> dict:
        """Get ERC-20 token balance. Returns {balance, symbol, decimals}."""

    @abstractmethod
    async def send_native(self, private_key: str, to: str, amount: str) -> str:
        """Send native token. Amount in human units. Returns tx hash."""

    @abstractmethod
    async def send_token(self, private_key: str, to: str, token: str, amount: str) -> str:
        """Send ERC-20 token. Amount in human units (18 decimals assumed). Returns tx hash."""

    @abstractmethod
    async def call_contract(
        self,
        private_key: str,
        contract: str,
        function_sig: str,
        args: list,
        value: str,
    ) -> str:
        """Call a state-changing contract function. Returns tx hash."""

    @abstractmethod
    async def read_contract(self, contract: str, function_sig: str, args: list) -> Any:
        """Read-only contract call. Returns decoded result."""

    @abstractmethod
    async def deploy_contract(
        self,
        private_key: str,
        bytecode: str,
        constructor_args: list | None,
        value: str,
    ) -> dict:
        """Deploy a contract. Returns {tx_hash, contract_address}."""

    async def close(self) -> None:  # noqa: B027
        """Cleanup resources (e.g. HTTP clients)."""
