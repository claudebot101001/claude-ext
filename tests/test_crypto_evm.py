"""Tests for EVM chain adapter — all RPC calls mocked."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from extensions.crypto.chains.evm import (
    EVMAdapter,
    _encode_address,
    _encode_uint256,
    _parse_function_sig,
    _sanitize_error,
    _secure_wipe,
    _to_wei,
    _validate_address,
)


def _run(coro):
    return asyncio.run(coro)


class TestHelpers:
    def test_to_wei_integer(self):
        assert _to_wei("1") == 10**18

    def test_to_wei_decimal(self):
        assert _to_wei("0.5") == 5 * 10**17

    def test_to_wei_small(self):
        assert _to_wei("0.000000000000000001") == 1

    def test_to_wei_custom_decimals(self):
        assert _to_wei("100", decimals=6) == 100_000_000

    def test_to_wei_rejects_excess_decimals(self):
        with pytest.raises(ValueError, match="decimal places"):
            _to_wei("0.0000000000000000001")  # 19 decimals

    def test_to_wei_rejects_excess_decimals_custom(self):
        with pytest.raises(ValueError, match="decimal places"):
            _to_wei("0.1234567", decimals=6)  # 7 > 6

    def test_validate_address_valid(self):
        _validate_address("0xABCDef0123456789abcdef0123456789ABCDef01")

    def test_validate_address_invalid(self):
        with pytest.raises(ValueError, match="Invalid Ethereum address"):
            _validate_address("not-an-address")

    def test_validate_address_too_short(self):
        with pytest.raises(ValueError, match="Invalid Ethereum address"):
            _validate_address("0xABCD")

    def test_encode_address(self):
        addr = "0xABCDef0123456789abcdef0123456789ABCDef01"
        result = _encode_address(addr)
        assert len(result) == 32
        assert result[:12] == b"\x00" * 12

    def test_encode_uint256(self):
        result = _encode_uint256(100)
        assert len(result) == 32
        assert int.from_bytes(result, "big") == 100

    def test_parse_function_sig(self):
        selector, types = _parse_function_sig("transfer(address,uint256)")
        assert len(selector) == 4
        assert types == ["address", "uint256"]

    def test_parse_function_sig_no_args(self):
        selector, types = _parse_function_sig("totalSupply()")
        assert len(selector) == 4
        assert types == []

    def test_parse_function_sig_invalid(self):
        with pytest.raises(ValueError, match="Invalid function signature"):
            _parse_function_sig("bad sig")

    def test_sanitize_error(self):
        result = _sanitize_error("Error with key abc123def456", "abc123def456")
        assert "abc123def456" not in result
        assert "[REDACTED]" in result

    def test_sanitize_error_no_match(self):
        result = _sanitize_error("Error message", "notpresent")
        assert result == "Error message"

    def test_sanitize_error_0x_prefix(self):
        key = "ab" * 32
        result = _sanitize_error(f"Error with 0x{key}", key)
        assert key not in result
        assert "0xab" not in result

    def test_secure_wipe_none(self):
        _secure_wipe(None)  # should not raise


class TestEVMAdapter:
    @pytest.fixture
    def adapter(self):
        a = EVMAdapter(
            rpc_url="http://localhost:8545",
            chain_id=1,
            receipt_timeout=5,
        )
        yield a
        _run(a.close())

    def test_generate_wallet(self, adapter):
        address, key = _run(adapter.generate_wallet())
        assert address.startswith("0x")
        assert len(address) == 42
        assert len(key) == 64  # hex without 0x prefix

    def test_get_balance(self, adapter):
        mock_rpc = AsyncMock(return_value="0xde0b6b3a7640000")  # 1 ETH
        with patch.object(adapter, "_rpc", mock_rpc):
            result = _run(adapter.get_balance("0x" + "ab" * 20))
        assert result["balance"] == "1.0"
        assert result["symbol"] == "ETH"

    def test_get_token_balance(self, adapter):
        call_count = 0

        async def mock_rpc(method, params=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # decimals() call → 18
                return "0x" + "0" * 62 + "12"
            # balanceOf() call → 1 ETH
            return "0x" + "0" * 48 + "de0b6b3a7640000"

        with patch.object(adapter, "_rpc", side_effect=mock_rpc):
            result = _run(adapter.get_token_balance("0x" + "ab" * 20, "0x" + "cd" * 20))
        assert "balance" in result
        assert result["decimals"] == 18

    def test_send_native(self, adapter):
        from eth_account import Account

        acct = Account.create()
        key = acct.key.hex()
        to = "0x" + "cd" * 20

        async def mock_rpc(method, params=None):
            if method == "eth_getTransactionCount":
                return "0x0"
            if method == "eth_gasPrice":
                return "0x3b9aca00"
            if method == "eth_estimateGas":
                return "0x5208"
            raise ValueError(f"Unexpected RPC: {method}")

        mock_sign = AsyncMock(return_value="0x" + "ab" * 32)
        with (
            patch.object(adapter, "_rpc", side_effect=mock_rpc),
            patch.object(adapter, "_sign_and_send", mock_sign),
        ):
            tx_hash = _run(adapter.send_native(key, to, "0.1"))
        assert tx_hash.startswith("0x")

    def test_read_contract(self, adapter):
        expected = "0x" + "0" * 64
        mock_rpc = AsyncMock(return_value=expected)
        with patch.object(adapter, "_rpc", mock_rpc):
            result = _run(adapter.read_contract("0x" + "ab" * 20, "totalSupply()", []))
        assert result == expected

    def test_deploy_contract(self, adapter):
        from eth_account import Account

        acct = Account.create()
        key = acct.key.hex()

        async def mock_rpc(method, params=None):
            if method == "eth_getTransactionCount":
                return "0x0"
            if method == "eth_gasPrice":
                return "0x3b9aca00"
            if method == "eth_estimateGas":
                return "0x5208"
            if method == "eth_sendRawTransaction":
                return "0x" + "ab" * 32
            if method == "eth_getTransactionReceipt":
                return {
                    "status": "0x1",
                    "contractAddress": "0x" + "ef" * 20,
                }
            raise ValueError(f"Unexpected RPC: {method}")

        with patch.object(adapter, "_rpc", side_effect=mock_rpc):
            result = _run(adapter.deploy_contract(key, "0x6060", None, "0"))
        assert result["contract_address"] == "0x" + "ef" * 20
        assert result["tx_hash"].startswith("0x")

    def test_deploy_contract_reverted(self, adapter):
        from eth_account import Account

        acct = Account.create()
        key = acct.key.hex()

        async def mock_rpc(method, params=None):
            if method == "eth_getTransactionCount":
                return "0x0"
            if method == "eth_gasPrice":
                return "0x3b9aca00"
            if method == "eth_estimateGas":
                return "0x5208"
            if method == "eth_sendRawTransaction":
                return "0x" + "ab" * 32
            if method == "eth_getTransactionReceipt":
                return {"status": "0x0"}
            raise ValueError(f"Unexpected RPC: {method}")

        with (
            patch.object(adapter, "_rpc", side_effect=mock_rpc),
            pytest.raises(RuntimeError, match="reverted"),
        ):
            _run(adapter.deploy_contract(key, "0x6060", None, "0"))

    def test_wait_for_receipt_timeout(self, adapter):
        adapter._receipt_timeout = 0.1
        mock_rpc = AsyncMock(return_value=None)
        with (
            patch.object(adapter, "_rpc", mock_rpc),
            pytest.raises(TimeoutError, match="Receipt not found"),
        ):
            _run(adapter._wait_for_receipt("0x" + "ab" * 32))

    def test_call_contract(self, adapter):
        from eth_account import Account

        acct = Account.create()
        key = acct.key.hex()

        async def mock_rpc(method, params=None):
            if method == "eth_getTransactionCount":
                return "0x0"
            if method == "eth_gasPrice":
                return "0x3b9aca00"
            if method == "eth_estimateGas":
                return "0x5208"
            raise ValueError(f"Unexpected RPC: {method}")

        mock_sign = AsyncMock(return_value="0x" + "cd" * 32)
        with (
            patch.object(adapter, "_rpc", side_effect=mock_rpc),
            patch.object(adapter, "_sign_and_send", mock_sign),
        ):
            tx_hash = _run(
                adapter.call_contract(
                    key,
                    "0x" + "ab" * 20,
                    "transfer(address,uint256)",
                    ["0x" + "cd" * 20, 1000],
                    "0",
                )
            )
        assert tx_hash.startswith("0x")
