#!/usr/bin/env python3
"""Crypto extension MCP server — gateway mode.

10 tools consolidated into 1 gateway tool. All signing operations
go through bridge RPC. Only contract_read is handled directly (no signing).
"""

import asyncio
import json
import sys
from pathlib import Path

_project_root = str(Path(__file__).resolve().parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from core.mcp_base import MCPServerBase  # noqa: E402


class CryptoMCPServer(MCPServerBase):
    name = "crypto"
    gateway_description = (
        "Crypto wallet, transactions, contracts, and x402 payments. action='help' for details."
    )
    tools = [
        {
            "name": "wallet_create",
            "description": "Generate a new wallet. Key stored in vault, address returned.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "chain": {
                        "type": "string",
                        "description": "Chain name (default: configured default_chain)",
                    },
                    "label": {
                        "type": "string",
                        "description": "Optional human-readable label",
                    },
                },
            },
        },
        {
            "name": "wallet_list",
            "description": "List managed wallets with optional live balances.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "chain": {
                        "type": "string",
                        "description": "Filter by chain name",
                    },
                    "balances": {
                        "type": "boolean",
                        "description": "Include live balance queries (slower)",
                    },
                },
            },
        },
        {
            "name": "balance",
            "description": "Check native or token balance for any address.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "address": {
                        "type": "string",
                        "description": "Wallet address to check",
                    },
                    "chain": {
                        "type": "string",
                        "description": "Chain name (default: configured default_chain)",
                    },
                    "token": {
                        "type": "string",
                        "description": "ERC-20 contract address (omit for native balance)",
                    },
                },
                "required": ["address"],
            },
        },
        {
            "name": "send",
            "description": "Send native token (ETH etc.). Amount in human units.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "from": {
                        "type": "string",
                        "description": "Sender wallet address (must be managed)",
                    },
                    "to": {
                        "type": "string",
                        "description": "Recipient address",
                    },
                    "amount": {
                        "type": "string",
                        "description": "Amount in human units (e.g. '0.1')",
                    },
                    "chain": {
                        "type": "string",
                        "description": "Chain name",
                    },
                },
                "required": ["from", "to", "amount"],
            },
        },
        {
            "name": "send_token",
            "description": "Send ERC-20 token. Amount in human units (18 decimals assumed).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "from": {
                        "type": "string",
                        "description": "Sender wallet address (must be managed)",
                    },
                    "to": {
                        "type": "string",
                        "description": "Recipient address",
                    },
                    "token": {
                        "type": "string",
                        "description": "ERC-20 token contract address",
                    },
                    "amount": {
                        "type": "string",
                        "description": "Amount in human units",
                    },
                    "chain": {
                        "type": "string",
                        "description": "Chain name",
                    },
                },
                "required": ["from", "to", "token", "amount"],
            },
        },
        {
            "name": "contract_deploy",
            "description": "Deploy a smart contract from bytecode. Returns contract address.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "from": {
                        "type": "string",
                        "description": "Deployer wallet address (must be managed)",
                    },
                    "bytecode": {
                        "type": "string",
                        "description": "Contract bytecode (hex, with or without 0x prefix)",
                    },
                    "constructor_args": {
                        "type": "array",
                        "description": 'Constructor args as [type, value] pairs, e.g. [["uint256", 100], ["address", "0x..."]]',
                        "items": {"type": "array"},
                    },
                    "value": {
                        "type": "string",
                        "description": "ETH to send with deployment (default: '0')",
                    },
                    "chain": {
                        "type": "string",
                        "description": "Chain name",
                    },
                },
                "required": ["from", "bytecode"],
            },
        },
        {
            "name": "contract_call",
            "description": "Call a state-changing contract function. Returns tx hash.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "from": {
                        "type": "string",
                        "description": "Caller wallet address (must be managed)",
                    },
                    "contract": {
                        "type": "string",
                        "description": "Contract address",
                    },
                    "function": {
                        "type": "string",
                        "description": "Function signature, e.g. 'transfer(address,uint256)'",
                    },
                    "args": {
                        "type": "array",
                        "description": "Function arguments",
                    },
                    "value": {
                        "type": "string",
                        "description": "ETH to send with call (default: '0')",
                    },
                    "chain": {
                        "type": "string",
                        "description": "Chain name",
                    },
                },
                "required": ["from", "contract", "function"],
            },
        },
        {
            "name": "contract_read",
            "description": "Read-only contract call (no signing needed).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "contract": {
                        "type": "string",
                        "description": "Contract address",
                    },
                    "function": {
                        "type": "string",
                        "description": "Function signature, e.g. 'totalSupply()'",
                    },
                    "args": {
                        "type": "array",
                        "description": "Function arguments",
                    },
                    "chain": {
                        "type": "string",
                        "description": "Chain name",
                    },
                },
                "required": ["contract", "function"],
            },
        },
        {
            "name": "x402_pay",
            "description": "Make HTTP request with automatic x402 payment handling.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL to request",
                    },
                    "method": {
                        "type": "string",
                        "description": "HTTP method (default: GET)",
                    },
                    "headers": {
                        "type": "object",
                        "description": "Optional HTTP headers",
                    },
                    "body": {
                        "type": "string",
                        "description": "Optional request body",
                    },
                },
                "required": ["url"],
            },
        },
        {
            "name": "x402_configure",
            "description": "Set which wallet and network to use for x402 payments.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "wallet": {
                        "type": "string",
                        "description": "Wallet address to pay from (must be managed)",
                    },
                    "network": {
                        "type": "string",
                        "description": "Network for payments (default: base)",
                    },
                    "facilitator_url": {
                        "type": "string",
                        "description": "Custom facilitator URL (default: Coinbase CDP)",
                    },
                },
                "required": ["wallet"],
            },
        },
    ]

    def __init__(self):
        super().__init__()
        # contract_read is direct (no signing), everything else goes through bridge
        self.handlers = {"contract_read": self._handle_contract_read}
        for tool in [
            "wallet_create",
            "wallet_list",
            "balance",
            "send",
            "send_token",
            "contract_deploy",
            "contract_call",
            "x402_pay",
            "x402_configure",
        ]:
            self.handlers[tool] = self._make_bridge_handler(f"crypto_{tool}")

    def _make_bridge_handler(self, bridge_method: str):
        """Factory for bridge RPC handlers."""

        def handler(args: dict) -> str:
            if not self.bridge:
                return json.dumps({"error": "Bridge not available"})
            params = {**args, "session_id": self.session_id}
            try:
                result = self.bridge.call(bridge_method, params)
                return json.dumps(result)
            except Exception as e:
                return json.dumps({"error": str(e)})

        return handler

    def _handle_contract_read(self, args: dict) -> str:
        """Read-only contract call — no signing, runs directly in MCP process."""
        contract = args.get("contract", "")
        function = args.get("function", "")
        call_args = args.get("args", [])
        chain = args.get("chain", "")

        if not contract or not function:
            return json.dumps({"error": "contract and function are required"})

        # We need chain config to know the RPC URL — get it via bridge
        if not self.bridge:
            return json.dumps({"error": "Bridge not available"})

        try:
            result = self._run_async(self._do_contract_read(contract, function, call_args, chain))
            return json.dumps(result)
        except Exception as e:
            return json.dumps({"error": str(e)})

    async def _do_contract_read(self, contract: str, function: str, args: list, chain: str) -> dict:
        """Execute read-only contract call."""
        # Get chain config via bridge (MCP process doesn't have chain configs)
        config_result = self.bridge.call(
            "crypto_get_chain_config", {"chain": chain, "session_id": self.session_id}
        )
        if "error" in config_result:
            return config_result

        rpc_url = config_result.get("rpc_url", "")
        chain_id = config_result.get("chain_id", 1)

        adapter = EVMAdapter(rpc_url=rpc_url, chain_id=chain_id)
        try:
            result = await adapter.read_contract(contract, function, args)
            return {"result": result}
        finally:
            await adapter.close()

    @staticmethod
    def _run_async(coro):
        """Run async code from sync MCP handler context."""
        return asyncio.run(coro)


# Import needed for contract_read direct execution
from extensions.crypto.chains.evm import EVMAdapter  # noqa: E402


if __name__ == "__main__":
    CryptoMCPServer().run()
