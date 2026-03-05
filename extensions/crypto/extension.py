"""Crypto extension — on-chain wallet management for autonomous agents.

Provides wallet generation, token transfers, contract interaction,
contract deployment, and x402 payment protocol support.

All signing operations happen in the main process via bridge RPC.
Private keys never leave the vault + bridge boundary.
"""

import logging
import re
import sys
from pathlib import Path

from core.extension import Extension
from extensions.crypto.chains.evm import EVMAdapter, _sanitize_error, _secure_wipe
from extensions.crypto.portfolio import PortfolioStore
from extensions.crypto.x402_handler import X402Handler

log = logging.getLogger(__name__)


class _KeyRedactionFilter(logging.Filter):
    """Redact 64-char hex strings (private keys) from log output."""

    _HEX_64 = re.compile(r"(?:0x)?[0-9a-fA-F]{64}")

    def filter(self, record):
        if isinstance(record.msg, str):
            record.msg = self._HEX_64.sub("[REDACTED]", record.msg)
        return True


_SYSTEM_PROMPT = """\
## Crypto Wallet
You manage crypto wallets with full on-chain capabilities via the crypto gateway tool.
SECURITY: Private keys are in the encrypted vault and never exposed. All signing is server-side.
x402: For paid web resources returning HTTP 402, use x402_pay to auto-handle payment.
Amounts are in human units (e.g. '0.1' ETH, not wei)."""


class ExtensionImpl(Extension):
    name = "crypto"

    def configure(self, engine, config):
        super().configure(engine, config)
        self._portfolio: PortfolioStore | None = None
        self._adapters: dict[str, EVMAdapter] = {}
        self._x402_handler: X402Handler | None = None
        self._chain_configs: dict = {}

    @property
    def sm(self):
        return self.engine.session_manager

    def _get_chain_adapter(self, chain: str) -> EVMAdapter:
        """Get or create chain adapter."""
        if chain in self._adapters:
            return self._adapters[chain]
        cfg = self._chain_configs.get(chain)
        if not cfg:
            raise ValueError(f"Unknown chain: {chain}. Available: {list(self._chain_configs)}")
        tx_cfg = self.config.get("tx", {})
        adapter = EVMAdapter(
            rpc_url=cfg["rpc_url"],
            chain_id=cfg["chain_id"],
            native_symbol=cfg.get("native_symbol", "ETH"),
            gas_multiplier=tx_cfg.get("gas_multiplier", 1.2),
            receipt_timeout=tx_cfg.get("receipt_timeout", 120),
        )
        self._adapters[chain] = adapter
        return adapter

    async def start(self) -> None:
        # Install log redaction filter on crypto and dependency loggers
        redaction_filter = _KeyRedactionFilter()
        for logger_name in ("extensions.crypto", "eth_account", "eth_abi"):
            logging.getLogger(logger_name).addFilter(redaction_filter)

        # Load chain configs
        self._chain_configs = self.config.get("chains", {})
        if not self._chain_configs:
            log.warning("Crypto: no chains configured")

        # Initialize portfolio store
        state_dir = (
            self.sm.base_dir / "crypto" if self.sm else Path("~/.claude-ext/crypto").expanduser()
        )
        state_dir.mkdir(parents=True, exist_ok=True)
        self._portfolio = PortfolioStore(state_dir)

        # Vault integration — register internal prefix to block LLM access
        vault_ext = self.engine.services.get("vault_ext")
        if vault_ext:
            vault_ext.register_internal_prefix("crypto/")
        else:
            log.warning(
                "Crypto: vault_ext service not found. Private keys won't be prefix-protected."
            )

        # Initialize x402 handler
        vault = self.engine.services.get("vault")
        if vault:
            self._x402_handler = X402Handler(vault, self._chain_configs)
        else:
            log.warning("Crypto: vault service not found. x402 payments disabled.")

        # Register MCP server
        mcp_script = str(Path(__file__).with_name("mcp_server.py"))
        self.sm.register_mcp_server(
            "crypto",
            {
                "command": sys.executable,
                "args": [mcp_script],
                "env": {},
            },
            tools=[
                {"name": "crypto", "description": "Crypto wallet and on-chain operations"},
            ],
        )

        # Register bridge handler
        self.engine.bridge.add_handler(self._bridge_handler)

        # System prompt
        self.sm.add_system_prompt(_SYSTEM_PROMPT, mcp_server="crypto")

        log.info(
            "Crypto extension started. %d chain(s), %d wallet(s).",
            len(self._chain_configs),
            len(self._portfolio.list_wallets()),
        )

    async def stop(self) -> None:
        for adapter in self._adapters.values():
            await adapter.close()
        self._adapters.clear()
        log.info("Crypto extension stopped.")

    async def health_check(self) -> dict:
        result: dict = {"status": "ok"}
        if self._portfolio:
            result["wallets"] = len(self._portfolio.list_wallets())
        result["chains"] = list(self._chain_configs.keys())
        result["x402_configured"] = bool(self._x402_handler and self._x402_handler.is_configured)
        return result

    def reconfigure(self, config: dict) -> None:
        super().reconfigure(config)
        self._chain_configs = config.get("chains", {})

    # -- Bridge handler --------------------------------------------------------

    async def _bridge_handler(self, method: str, params: dict) -> dict | None:
        if not method.startswith("crypto_"):
            return None

        session_id = params.get("session_id", "unknown")
        handler_name = method.removeprefix("crypto_")

        handlers = {
            "wallet_create": self._handle_wallet_create,
            "wallet_list": self._handle_wallet_list,
            "balance": self._handle_balance,
            "send": self._handle_send,
            "send_token": self._handle_send_token,
            "contract_deploy": self._handle_contract_deploy,
            "contract_call": self._handle_contract_call,
            "x402_pay": self._handle_x402_pay,
            "x402_configure": self._handle_x402_configure,
            "get_chain_config": self._handle_get_chain_config,
        }

        handler = handlers.get(handler_name)
        if not handler:
            return {"error": f"Unknown crypto method: {method}"}

        try:
            return await handler(params, session_id)
        except Exception as e:
            log.exception("Crypto bridge handler error: %s", method)
            # Redact any 64-char hex strings that could be private keys
            sanitized = re.sub(r"(?:0x)?[0-9a-fA-F]{64}", "[REDACTED]", str(e))
            return {"error": f"Internal error: {sanitized}"}

    # -- Wallet handlers -------------------------------------------------------

    async def _handle_wallet_create(self, params: dict, session_id: str) -> dict:
        chain = params.get("chain", self.config.get("default_chain", "ethereum"))
        label = params.get("label", "")

        vault = self.engine.services.get("vault")
        if not vault:
            return {"error": "Vault service not available"}

        adapter = self._get_chain_adapter(chain)
        address, private_key = await adapter.generate_wallet()

        try:
            vault_key = f"crypto/{chain}/{address}/privkey"
            vault.put(vault_key, private_key, tags=["crypto", chain])
        finally:
            _secure_wipe(private_key)

        entry = self._portfolio.add_wallet(address, chain, label)

        if self.engine.events:
            self.engine.events.log(
                "crypto.wallet_created",
                session_id,
                {"address": address, "chain": chain, "label": label},
            )

        return {
            "address": address,
            "chain": chain,
            "label": label,
            "created_at": entry["created_at"],
        }

    async def _handle_wallet_list(self, params: dict, session_id: str) -> dict:
        chain = params.get("chain")
        wallets = self._portfolio.list_wallets(chain)

        if params.get("balances"):
            for w in wallets:
                try:
                    adapter = self._get_chain_adapter(w["chain"])
                    bal = await adapter.get_balance(w["address"])
                    w["balance"] = bal["balance"]
                    w["symbol"] = bal["symbol"]
                except Exception as e:
                    w["balance_error"] = str(e)

        return {"wallets": wallets}

    async def _handle_balance(self, params: dict, session_id: str) -> dict:
        address = params.get("address", "")
        chain = params.get("chain", self.config.get("default_chain", "ethereum"))
        token = params.get("token")

        if not address:
            return {"error": "address is required"}

        adapter = self._get_chain_adapter(chain)
        if token:
            return await adapter.get_token_balance(address, token)
        return await adapter.get_balance(address)

    # -- Transaction handlers --------------------------------------------------

    async def _handle_send(self, params: dict, session_id: str) -> dict:
        chain = params.get("chain", self.config.get("default_chain", "ethereum"))
        from_wallet = params.get("from", "")
        to = params.get("to", "")
        amount = params.get("amount", "")

        if not from_wallet or not to or not amount:
            return {"error": "from, to, and amount are required"}

        vault = self.engine.services.get("vault")
        if not vault:
            return {"error": "Vault service not available"}

        key = None
        try:
            vault_key = f"crypto/{chain}/{from_wallet}/privkey"
            key = vault.get(vault_key)
            if not key:
                return {"error": f"No key for wallet {from_wallet} on {chain}"}

            adapter = self._get_chain_adapter(chain)
            tx_hash = await adapter.send_native(key, to, amount)

            if self.engine.events:
                self.engine.events.log(
                    "crypto.send",
                    session_id,
                    {
                        "from": from_wallet,
                        "to": to,
                        "amount": amount,
                        "chain": chain,
                        "tx_hash": tx_hash,
                    },
                )

            return {
                "tx_hash": tx_hash,
                "chain": chain,
                "from": from_wallet,
                "to": to,
                "amount": amount,
            }
        except Exception as e:
            return {"error": _sanitize_error(str(e), key)}
        finally:
            _secure_wipe(key)

    async def _handle_send_token(self, params: dict, session_id: str) -> dict:
        chain = params.get("chain", self.config.get("default_chain", "ethereum"))
        from_wallet = params.get("from", "")
        to = params.get("to", "")
        token = params.get("token", "")
        amount = params.get("amount", "")

        if not from_wallet or not to or not token or not amount:
            return {"error": "from, to, token, and amount are required"}

        vault = self.engine.services.get("vault")
        if not vault:
            return {"error": "Vault service not available"}

        key = None
        try:
            vault_key = f"crypto/{chain}/{from_wallet}/privkey"
            key = vault.get(vault_key)
            if not key:
                return {"error": f"No key for wallet {from_wallet} on {chain}"}

            adapter = self._get_chain_adapter(chain)
            tx_hash = await adapter.send_token(key, to, token, amount)

            if self.engine.events:
                self.engine.events.log(
                    "crypto.send_token",
                    session_id,
                    {
                        "from": from_wallet,
                        "to": to,
                        "token": token,
                        "amount": amount,
                        "chain": chain,
                        "tx_hash": tx_hash,
                    },
                )

            return {
                "tx_hash": tx_hash,
                "chain": chain,
                "from": from_wallet,
                "to": to,
                "token": token,
                "amount": amount,
            }
        except Exception as e:
            return {"error": _sanitize_error(str(e), key)}
        finally:
            _secure_wipe(key)

    # -- Contract handlers -----------------------------------------------------

    async def _handle_contract_deploy(self, params: dict, session_id: str) -> dict:
        chain = params.get("chain", self.config.get("default_chain", "ethereum"))
        from_wallet = params.get("from", "")
        bytecode = params.get("bytecode", "")
        constructor_args = params.get("constructor_args")
        value = params.get("value", "0")

        if not from_wallet or not bytecode:
            return {"error": "from and bytecode are required"}

        vault = self.engine.services.get("vault")
        if not vault:
            return {"error": "Vault service not available"}

        key = None
        try:
            vault_key = f"crypto/{chain}/{from_wallet}/privkey"
            key = vault.get(vault_key)
            if not key:
                return {"error": f"No key for wallet {from_wallet} on {chain}"}

            adapter = self._get_chain_adapter(chain)
            result = await adapter.deploy_contract(key, bytecode, constructor_args, value)

            if self.engine.events:
                self.engine.events.log(
                    "crypto.contract_deployed",
                    session_id,
                    {
                        "from": from_wallet,
                        "chain": chain,
                        "contract_address": result["contract_address"],
                        "tx_hash": result["tx_hash"],
                    },
                )

            return {**result, "chain": chain, "from": from_wallet}
        except Exception as e:
            return {"error": _sanitize_error(str(e), key)}
        finally:
            _secure_wipe(key)

    async def _handle_contract_call(self, params: dict, session_id: str) -> dict:
        chain = params.get("chain", self.config.get("default_chain", "ethereum"))
        from_wallet = params.get("from", "")
        contract = params.get("contract", "")
        function_sig = params.get("function", "")
        args = params.get("args", [])
        value = params.get("value", "0")

        if not from_wallet or not contract or not function_sig:
            return {"error": "from, contract, and function are required"}

        vault = self.engine.services.get("vault")
        if not vault:
            return {"error": "Vault service not available"}

        key = None
        try:
            vault_key = f"crypto/{chain}/{from_wallet}/privkey"
            key = vault.get(vault_key)
            if not key:
                return {"error": f"No key for wallet {from_wallet} on {chain}"}

            adapter = self._get_chain_adapter(chain)
            tx_hash = await adapter.call_contract(key, contract, function_sig, args, value)

            if self.engine.events:
                self.engine.events.log(
                    "crypto.contract_call",
                    session_id,
                    {
                        "from": from_wallet,
                        "contract": contract,
                        "function": function_sig,
                        "chain": chain,
                        "tx_hash": tx_hash,
                    },
                )

            return {"tx_hash": tx_hash, "chain": chain, "from": from_wallet, "contract": contract}
        except Exception as e:
            return {"error": _sanitize_error(str(e), key)}
        finally:
            _secure_wipe(key)

    # -- x402 handlers ---------------------------------------------------------

    async def _handle_x402_pay(self, params: dict, session_id: str) -> dict:
        if not self._x402_handler:
            return {"error": "x402 handler not available (vault required)"}

        url = params.get("url", "")
        method = params.get("method", "GET")
        headers = params.get("headers")
        body = params.get("body")

        if not url:
            return {"error": "url is required"}

        result = await self._x402_handler.execute_request(url, method, headers, body)

        if result.get("payment_made") and self.engine.events:
            self.engine.events.log(
                "crypto.x402_payment",
                session_id,
                {
                    "url": url,
                    "amount": result.get("payment", {}).get("amount"),
                    "network": result.get("payment", {}).get("network"),
                },
            )

        return result

    async def _handle_x402_configure(self, params: dict, session_id: str) -> dict:
        if not self._x402_handler:
            return {"error": "x402 handler not available (vault required)"}

        wallet = params.get("wallet", "")
        network = params.get("network", self.config.get("x402", {}).get("default_network", "base"))
        facilitator_url = params.get("facilitator_url")

        if not wallet:
            return {"error": "wallet address is required"}

        if not self._portfolio.get_wallet(wallet):
            return {"error": f"Wallet {wallet} is not managed. Create it first."}

        self._x402_handler.configure(wallet, network, facilitator_url)
        return {"wallet": wallet, "network": network, "status": "configured"}

    # -- Internal bridge handlers ----------------------------------------------

    async def _handle_get_chain_config(self, params: dict, session_id: str) -> dict:
        """Return chain config for MCP server (used by contract_read)."""
        chain = params.get("chain", self.config.get("default_chain", "ethereum"))
        cfg = self._chain_configs.get(chain)
        if not cfg:
            return {"error": f"Unknown chain: {chain}. Available: {list(self._chain_configs)}"}
        return {
            "rpc_url": cfg["rpc_url"],
            "chain_id": cfg["chain_id"],
            "native_symbol": cfg.get("native_symbol", "ETH"),
        }
