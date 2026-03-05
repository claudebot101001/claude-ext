"""Tests for crypto extension bridge handlers."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from extensions.crypto.extension import ExtensionImpl
from extensions.crypto.portfolio import PortfolioStore


def _run(coro):
    return asyncio.run(coro)


class _StubVault:
    def __init__(self):
        self._store = {}

    def put(self, key, value, tags=None):
        self._store[key] = value

    def get(self, key):
        return self._store.get(key)

    def delete(self, key):
        return self._store.pop(key, None) is not None


class _StubVaultExt:
    def __init__(self):
        self.registered_prefixes = []

    def register_internal_prefix(self, prefix):
        self.registered_prefixes.append(prefix)


class _StubEngine:
    events = None

    def __init__(self):
        self.services = {}


_SID = "test-session-00000000"


@pytest.fixture
def ext(tmp_path):
    engine = _StubEngine()
    vault = _StubVault()
    vault_ext = _StubVaultExt()
    engine.services["vault"] = vault
    engine.services["vault_ext"] = vault_ext

    config = {
        "chains": {
            "ethereum": {
                "rpc_url": "http://localhost:8545",
                "chain_id": 1,
                "native_symbol": "ETH",
            },
            "base": {
                "rpc_url": "http://localhost:8546",
                "chain_id": 8453,
                "native_symbol": "ETH",
                "usdc_address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            },
        },
        "default_chain": "ethereum",
        "tx": {"gas_multiplier": 1.2, "receipt_timeout": 5},
    }
    e = ExtensionImpl()
    e.configure(engine, config)
    e._chain_configs = config["chains"]
    e._portfolio = PortfolioStore(tmp_path / "crypto")

    from extensions.crypto.x402_handler import X402Handler

    e._x402_handler = X402Handler(vault, e._chain_configs)

    return e


class TestBridgeHandler:
    def test_ignores_non_crypto_methods(self, ext):
        result = _run(ext._bridge_handler("vault_store", {}))
        assert result is None

    def test_unknown_crypto_method(self, ext):
        result = _run(ext._bridge_handler("crypto_unknown", {"session_id": _SID}))
        assert "error" in result

    def test_wallet_create(self, ext):
        adapter_mock = AsyncMock()
        adapter_mock.generate_wallet.return_value = ("0xNEW", "deadbeef" * 8)
        ext._adapters["ethereum"] = adapter_mock

        result = _run(ext._handle_wallet_create({"chain": "ethereum", "label": "test"}, _SID))

        assert result["address"] == "0xNEW"
        assert result["chain"] == "ethereum"
        assert result["label"] == "test"
        # Key stored in vault
        assert ext.engine.services["vault"].get("crypto/ethereum/0xNEW/privkey") is not None
        # Wallet in portfolio
        assert ext._portfolio.get_wallet("0xNEW") is not None

    def test_wallet_list(self, ext):
        ext._portfolio.add_wallet("0xA", "ethereum")
        ext._portfolio.add_wallet("0xB", "base")
        result = _run(ext._handle_wallet_list({}, _SID))
        assert len(result["wallets"]) == 2

    def test_wallet_list_filter_chain(self, ext):
        ext._portfolio.add_wallet("0xA", "ethereum")
        ext._portfolio.add_wallet("0xB", "base")
        result = _run(ext._handle_wallet_list({"chain": "base"}, _SID))
        assert len(result["wallets"]) == 1

    def test_balance(self, ext):
        adapter_mock = AsyncMock()
        adapter_mock.get_balance.return_value = {
            "balance": "1.0",
            "symbol": "ETH",
            "wei": "1000000000000000000",
        }
        ext._adapters["ethereum"] = adapter_mock

        result = _run(ext._handle_balance({"address": "0x" + "ab" * 20, "chain": "ethereum"}, _SID))
        assert result["balance"] == "1.0"

    def test_balance_missing_address(self, ext):
        result = _run(ext._handle_balance({}, _SID))
        assert "error" in result

    def test_send(self, ext):
        ext.engine.services["vault"].put("crypto/ethereum/0xSENDER/privkey", "ab" * 32)
        adapter_mock = AsyncMock()
        adapter_mock.send_native.return_value = "0x" + "ff" * 32
        ext._adapters["ethereum"] = adapter_mock

        result = _run(
            ext._handle_send(
                {"from": "0xSENDER", "to": "0xRECEIVER", "amount": "0.1", "chain": "ethereum"},
                _SID,
            )
        )
        assert result["tx_hash"] == "0x" + "ff" * 32

    def test_send_missing_params(self, ext):
        result = _run(ext._handle_send({"from": "0xA"}, _SID))
        assert "error" in result

    def test_send_no_key(self, ext):
        result = _run(
            ext._handle_send(
                {"from": "0xNOKEY", "to": "0xB", "amount": "1", "chain": "ethereum"},
                _SID,
            )
        )
        assert "error" in result
        assert "No key" in result["error"]

    def test_send_token(self, ext):
        ext.engine.services["vault"].put("crypto/ethereum/0xSENDER/privkey", "ab" * 32)
        adapter_mock = AsyncMock()
        adapter_mock.send_token.return_value = "0x" + "ee" * 32
        ext._adapters["ethereum"] = adapter_mock

        result = _run(
            ext._handle_send_token(
                {
                    "from": "0xSENDER",
                    "to": "0xRECEIVER",
                    "token": "0xTOKEN",
                    "amount": "100",
                    "chain": "ethereum",
                },
                _SID,
            )
        )
        assert result["tx_hash"] == "0x" + "ee" * 32

    def test_contract_deploy(self, ext):
        ext.engine.services["vault"].put("crypto/ethereum/0xDEPLOYER/privkey", "ab" * 32)
        adapter_mock = AsyncMock()
        adapter_mock.deploy_contract.return_value = {
            "tx_hash": "0x" + "dd" * 32,
            "contract_address": "0x" + "cc" * 20,
        }
        ext._adapters["ethereum"] = adapter_mock

        result = _run(
            ext._handle_contract_deploy(
                {"from": "0xDEPLOYER", "bytecode": "0x6060", "chain": "ethereum"},
                _SID,
            )
        )
        assert result["contract_address"] == "0x" + "cc" * 20

    def test_contract_call(self, ext):
        ext.engine.services["vault"].put("crypto/ethereum/0xCALLER/privkey", "ab" * 32)
        adapter_mock = AsyncMock()
        adapter_mock.call_contract.return_value = "0x" + "bb" * 32
        ext._adapters["ethereum"] = adapter_mock

        result = _run(
            ext._handle_contract_call(
                {
                    "from": "0xCALLER",
                    "contract": "0xCONTRACT",
                    "function": "transfer(address,uint256)",
                    "args": ["0x" + "cd" * 20, 1000],
                    "chain": "ethereum",
                },
                _SID,
            )
        )
        assert result["tx_hash"] == "0x" + "bb" * 32

    def test_x402_configure(self, ext):
        ext._portfolio.add_wallet("0xPAY", "base")
        result = _run(ext._handle_x402_configure({"wallet": "0xPAY", "network": "base"}, _SID))
        assert result["status"] == "configured"

    def test_x402_configure_unknown_wallet(self, ext):
        result = _run(ext._handle_x402_configure({"wallet": "0xNONE", "network": "base"}, _SID))
        assert "error" in result

    def test_get_chain_config(self, ext):
        result = _run(ext._handle_get_chain_config({"chain": "ethereum"}, _SID))
        assert result["rpc_url"] == "http://localhost:8545"
        assert result["chain_id"] == 1

    def test_get_chain_config_unknown(self, ext):
        result = _run(ext._handle_get_chain_config({"chain": "solana"}, _SID))
        assert "error" in result
