"""Tests for x402 payment handler — all HTTP mocked."""

import asyncio
import json
from unittest.mock import patch

import httpx
import pytest

pytest.importorskip("eth_account")

from extensions.crypto.x402_handler import X402Handler


def _run(coro):
    return asyncio.run(coro)


class _StubVault:
    def __init__(self):
        self._store = {}

    def put(self, key, value, tags=None):
        self._store[key] = value

    def get(self, key):
        return self._store.get(key)


_CHAIN_CONFIGS = {
    "base": {
        "rpc_url": "https://mainnet.base.org",
        "chain_id": 8453,
        "usdc_address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    },
    "ethereum": {
        "rpc_url": "https://eth.llamarpc.com",
        "chain_id": 1,
        "usdc_address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    },
}


@pytest.fixture
def handler():
    vault = _StubVault()
    vault.put("crypto/base/0xPAYER/privkey", "ab" * 32)
    h = X402Handler(vault, _CHAIN_CONFIGS)
    h.configure("0xPAYER", "base")
    return h


class TestX402Handler:
    def test_not_configured(self):
        vault = _StubVault()
        h = X402Handler(vault, _CHAIN_CONFIGS)
        result = _run(h.execute_request("https://example.com"))
        assert "error" in result
        assert "not configured" in result["error"]

    def test_non_402_passthrough(self, handler):
        mock_response = httpx.Response(
            200,
            text="OK",
            headers={"content-type": "text/plain"},
            request=httpx.Request("GET", "https://example.com"),
        )

        async def mock_request(*args, **kwargs):
            return mock_response

        with patch("httpx.AsyncClient.request", side_effect=mock_request):
            result = _run(handler.execute_request("https://example.com"))
        assert result["status_code"] == 200
        assert result["payment_made"] is False

    def test_402_no_json_body(self, handler):
        mock_response = httpx.Response(
            402,
            text="Payment Required",
            headers={"content-type": "text/plain"},
            request=httpx.Request("GET", "https://example.com"),
        )

        async def mock_request(*args, **kwargs):
            return mock_response

        with patch("httpx.AsyncClient.request", side_effect=mock_request):
            result = _run(handler.execute_request("https://example.com"))
        assert "error" in result

    def test_402_no_compatible_network(self, handler):
        pay_body = {
            "accepts": [
                {
                    "scheme": "exact",
                    "network": "solana",
                    "maxAmountRequired": "1000000",
                }
            ]
        }
        mock_response = httpx.Response(
            402,
            json=pay_body,
            headers={"content-type": "application/json"},
            request=httpx.Request("GET", "https://example.com"),
        )

        async def mock_request(*args, **kwargs):
            return mock_response

        with patch("httpx.AsyncClient.request", side_effect=mock_request):
            result = _run(handler.execute_request("https://example.com"))
        assert "error" in result
        assert "No compatible payment option" in result["error"]

    def test_402_payment_flow(self, handler):
        """Full 402 → sign → retry flow."""
        pay_body = {
            "accepts": [
                {
                    "scheme": "exact",
                    "network": "base",
                    "maxAmountRequired": "1000000",
                    "payeeAddress": "0x" + "bb" * 20,
                    "nonce": "0x" + "00" * 32,
                    "deadline": "999999999999",
                }
            ]
        }
        call_count = 0

        async def mock_request(method, url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(
                    402,
                    json=pay_body,
                    headers={"content-type": "application/json"},
                    request=httpx.Request(method, url),
                )
            # Second call should have X-PAYMENT header
            headers = kwargs.get("headers", {})
            assert "X-PAYMENT" in headers
            payment = json.loads(headers["X-PAYMENT"])
            assert payment["x402Version"] == 1
            assert payment["network"] == "base"
            return httpx.Response(
                200,
                json={"data": "resource content"},
                headers={"content-type": "application/json"},
                request=httpx.Request(method, url),
            )

        with patch("httpx.AsyncClient.request", side_effect=mock_request):
            result = _run(handler.execute_request("https://example.com/paid"))

        assert result["status_code"] == 200
        assert result["payment_made"] is True
        assert result["payment"]["network"] == "base"
        assert call_count == 2

    def test_no_key_in_vault(self):
        vault = _StubVault()  # No key stored
        h = X402Handler(vault, _CHAIN_CONFIGS)
        h.configure("0xNOKEY", "base")
        result = _run(h.execute_request("https://example.com"))
        assert "error" in result
        assert "No key" in result["error"]

    def test_is_configured_property(self):
        vault = _StubVault()
        h = X402Handler(vault, _CHAIN_CONFIGS)
        assert h.is_configured is False
        h.configure("0xWALLET", "base")
        assert h.is_configured is True

    def test_find_compatible_option(self, handler):
        accepts = [
            {"network": "ethereum", "maxAmountRequired": "100"},
            {"network": "base", "maxAmountRequired": "200"},
        ]
        result = handler._find_compatible_option(accepts)
        assert result is not None
        assert result["network"] == "base"

    def test_find_compatible_option_none(self, handler):
        accepts = [{"network": "solana", "maxAmountRequired": "100"}]
        result = handler._find_compatible_option(accepts)
        assert result is None
