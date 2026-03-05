"""EVM chain adapter — Ethereum-compatible chains via JSON-RPC.

Uses eth_account for signing (no web3.py). Raw JSON-RPC over httpx.
"""

import asyncio
import logging
import re
from typing import Any

import httpx
from eth_abi import encode as abi_encode
from eth_account import Account
from eth_utils import keccak

from .base import ChainAdapter

log = logging.getLogger(__name__)

# ERC-20 transfer(address,uint256)
_ERC20_TRANSFER_SELECTOR = keccak(b"transfer(address,uint256)")[:4]


def _sanitize_error(msg: str, *sensitive: str | None) -> str:
    """Strip sensitive values from error messages.

    Handles both raw hex and 0x-prefixed variants to prevent partial leaks.
    """
    result = str(msg)
    for val in sensitive:
        if not val:
            continue
        result = result.replace(val, "[REDACTED]")
        # Also strip without/with 0x prefix
        stripped = val.removeprefix("0x")
        if stripped != val:
            result = result.replace(stripped, "[REDACTED]")
        else:
            result = result.replace("0x" + val, "[REDACTED]")
    return result


def _secure_wipe(key: str | None) -> None:
    """Best-effort wipe of key material from memory.

    LIMITATION: Python strings are immutable. This wipes a copy (bytearray),
    not the original str object's internal buffer. The original bytes persist
    in the CPython heap until freed and overwritten by unrelated allocations.
    The primary mitigation is minimizing key lifetime via try/finally scope.
    """
    if key is None:
        return
    try:
        ba = bytearray(key.encode())
        for i in range(len(ba)):
            ba[i] = 0
        del ba
    except Exception:
        pass


def _to_wei(amount: str, decimals: int = 18) -> int:
    """Convert human-readable token amount to smallest unit.

    Args:
        amount: Human-readable amount (e.g. '0.1').
        decimals: Token decimals (18 for ETH, 6 for USDC, etc.).

    Raises ValueError if more decimal places than supported.
    """
    parts = amount.split(".")
    if len(parts) == 1:
        return int(parts[0]) * 10**decimals
    frac = parts[1]
    if len(frac) > decimals:
        raise ValueError(f"Amount has {len(frac)} decimal places, max {decimals} for this token")
    decimal_str = frac.ljust(decimals, "0")
    return int(parts[0]) * 10**decimals + int(decimal_str)


_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def _validate_address(addr: str) -> None:
    """Validate Ethereum address format."""
    if not _ADDR_RE.match(addr):
        raise ValueError(f"Invalid Ethereum address: {addr}")


def _encode_address(addr: str) -> bytes:
    """Encode address as 32-byte ABI word."""
    return bytes.fromhex(addr.lower().removeprefix("0x").zfill(64))


def _encode_uint256(value: int) -> bytes:
    """Encode uint256 as 32-byte ABI word."""
    return value.to_bytes(32, "big")


def _parse_function_sig(sig: str) -> tuple[bytes, list[str]]:
    """Parse 'functionName(type1,type2)' → (selector, [types]).

    Returns 4-byte selector and list of ABI type strings.
    """
    match = re.match(r"^(\w+)\(([^)]*)\)$", sig)
    if not match:
        raise ValueError(f"Invalid function signature: {sig}")
    name = match.group(1)
    types_str = match.group(2).strip()
    types = [t.strip() for t in types_str.split(",")] if types_str else []
    canonical = f"{name}({','.join(types)})"
    selector = keccak(canonical.encode())[:4]
    return selector, types


class EVMAdapter(ChainAdapter):
    """EVM chain adapter using raw JSON-RPC."""

    def __init__(
        self,
        rpc_url: str,
        chain_id: int,
        native_symbol: str = "ETH",
        gas_multiplier: float = 1.2,
        receipt_timeout: int = 120,
    ):
        self.rpc_url = rpc_url
        self.chain_id = chain_id
        self.native_symbol = native_symbol
        self._gas_multiplier = gas_multiplier
        self._receipt_timeout = receipt_timeout
        self._client = httpx.AsyncClient(timeout=30)
        self._rpc_id = 0

    async def _rpc(self, method: str, params: list | None = None) -> Any:
        """Make a JSON-RPC call."""
        self._rpc_id += 1
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or [],
            "id": self._rpc_id,
        }
        resp = await self._client.post(self.rpc_url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"RPC error: {data['error']}")
        return data.get("result")

    async def _get_nonce(self, address: str) -> int:
        result = await self._rpc("eth_getTransactionCount", [address, "pending"])
        return int(result, 16)

    async def _get_gas_price(self) -> int:
        result = await self._rpc("eth_gasPrice")
        return int(result, 16)

    async def _estimate_gas(self, tx: dict) -> int:
        result = await self._rpc("eth_estimateGas", [tx])
        return int(result, 16)

    async def _sign_and_send(self, private_key: str, tx: dict) -> str:
        """Sign transaction and broadcast. Returns tx hash."""
        acct = None
        try:
            acct = Account.from_key(private_key)
            signed = acct.sign_transaction(tx)
            raw = "0x" + signed.raw_transaction.hex()
            tx_hash = await self._rpc("eth_sendRawTransaction", [raw])
            return tx_hash
        finally:
            del acct

    async def _wait_for_receipt(self, tx_hash: str) -> dict:
        """Poll for transaction receipt with exponential backoff.

        Returns the full receipt dict. Raises on timeout or reverted tx.
        """
        delay = 1.0
        elapsed = 0.0
        while elapsed < self._receipt_timeout:
            receipt = await self._rpc("eth_getTransactionReceipt", [tx_hash])
            if receipt is not None:
                status = int(receipt.get("status", "0x1"), 16)
                if status == 0:
                    raise RuntimeError(
                        f"Transaction reverted (tx_hash={tx_hash}). Check explorer for details."
                    )
                return receipt
            await asyncio.sleep(delay)
            elapsed += delay
            delay = min(delay * 2, 5.0)
        raise TimeoutError(f"Receipt not found after {self._receipt_timeout}s (tx_hash={tx_hash})")

    # -- ChainAdapter implementation --

    async def generate_wallet(self) -> tuple[str, str]:
        acct = Account.create()
        address = acct.address
        key = acct.key.hex()
        del acct
        return address, key

    async def get_balance(self, address: str) -> dict:
        result = await self._rpc("eth_getBalance", [address, "latest"])
        wei = int(result, 16)
        balance = str(wei / 10**18)
        return {"balance": balance, "symbol": self.native_symbol, "wei": str(wei)}

    async def _get_token_decimals(self, token: str) -> int:
        """Query ERC-20 decimals() from the token contract."""
        selector = keccak(b"decimals()")[:4]
        data = "0x" + selector.hex()
        try:
            result = await self._rpc("eth_call", [{"to": token, "data": data}, "latest"])
            return int(result, 16)
        except Exception:
            return 18  # fallback

    async def get_token_balance(self, address: str, token: str) -> dict:
        decimals = await self._get_token_decimals(token)
        # balanceOf(address)
        selector = keccak(b"balanceOf(address)")[:4]
        data = "0x" + selector.hex() + _encode_address(address).hex()
        result = await self._rpc("eth_call", [{"to": token, "data": data}, "latest"])
        raw_balance = int(result, 16)
        balance = str(raw_balance / 10**decimals)
        return {"balance": balance, "raw_balance": str(raw_balance), "decimals": decimals}

    async def send_native(self, private_key: str, to: str, amount: str) -> str:
        _validate_address(to)
        key = private_key
        acct = None
        try:
            acct = Account.from_key(key)
            nonce = await self._get_nonce(acct.address)
            gas_price = await self._get_gas_price()
            value = _to_wei(amount)
            tx_for_estimate = {
                "from": acct.address,
                "to": to,
                "value": hex(value),
            }
            gas = await self._estimate_gas(tx_for_estimate)
            gas = int(gas * self._gas_multiplier)
            tx = {
                "nonce": nonce,
                "to": to,
                "value": value,
                "gas": gas,
                "gasPrice": gas_price,
                "chainId": self.chain_id,
            }
            return await self._sign_and_send(key, tx)
        except Exception as e:
            raise RuntimeError(_sanitize_error(str(e), key)) from None
        finally:
            del acct

    async def send_token(self, private_key: str, to: str, token: str, amount: str) -> str:
        _validate_address(to)
        _validate_address(token)
        key = private_key
        acct = None
        try:
            acct = Account.from_key(key)
            nonce = await self._get_nonce(acct.address)
            gas_price = await self._get_gas_price()
            # Query token decimals for correct amount encoding
            decimals = await self._get_token_decimals(token)
            value_raw = _to_wei(amount, decimals)
            calldata = _ERC20_TRANSFER_SELECTOR + _encode_address(to) + _encode_uint256(value_raw)
            tx_for_estimate = {
                "from": acct.address,
                "to": token,
                "data": "0x" + calldata.hex(),
            }
            gas = await self._estimate_gas(tx_for_estimate)
            gas = int(gas * self._gas_multiplier)
            tx = {
                "nonce": nonce,
                "to": token,
                "value": 0,
                "gas": gas,
                "gasPrice": gas_price,
                "chainId": self.chain_id,
                "data": calldata,
            }
            return await self._sign_and_send(key, tx)
        except Exception as e:
            raise RuntimeError(_sanitize_error(str(e), key)) from None
        finally:
            del acct

    async def call_contract(
        self,
        private_key: str,
        contract: str,
        function_sig: str,
        args: list,
        value: str,
    ) -> str:
        _validate_address(contract)
        key = private_key
        acct = None
        try:
            acct = Account.from_key(key)
            nonce = await self._get_nonce(acct.address)
            gas_price = await self._get_gas_price()
            selector, types = _parse_function_sig(function_sig)
            encoded_args = abi_encode(types, args) if types else b""
            calldata = selector + encoded_args
            tx_for_estimate = {
                "from": acct.address,
                "to": contract,
                "data": "0x" + calldata.hex(),
                "value": hex(_to_wei(value)) if value != "0" else "0x0",
            }
            gas = await self._estimate_gas(tx_for_estimate)
            gas = int(gas * self._gas_multiplier)
            tx = {
                "nonce": nonce,
                "to": contract,
                "value": _to_wei(value) if value != "0" else 0,
                "gas": gas,
                "gasPrice": gas_price,
                "chainId": self.chain_id,
                "data": calldata,
            }
            return await self._sign_and_send(key, tx)
        except Exception as e:
            raise RuntimeError(_sanitize_error(str(e), key)) from None
        finally:
            del acct

    async def read_contract(self, contract: str, function_sig: str, args: list) -> Any:
        selector, types = _parse_function_sig(function_sig)
        encoded_args = abi_encode(types, args) if types else b""
        calldata = selector + encoded_args
        data = "0x" + calldata.hex()
        result = await self._rpc("eth_call", [{"to": contract, "data": data}, "latest"])
        return result

    async def deploy_contract(
        self,
        private_key: str,
        bytecode: str,
        constructor_args: list | None,
        value: str,
    ) -> dict:
        key = private_key
        acct = None
        try:
            acct = Account.from_key(key)
            nonce = await self._get_nonce(acct.address)
            gas_price = await self._get_gas_price()
            data = bytes.fromhex(bytecode.removeprefix("0x"))
            if constructor_args:
                # Constructor args need ABI types — passed as [type, value] pairs
                # e.g. [["uint256", 100], ["address", "0x..."]]
                types = [a[0] for a in constructor_args]
                values = [a[1] for a in constructor_args]
                data += abi_encode(types, values)
            tx_for_estimate = {
                "from": acct.address,
                "data": "0x" + data.hex(),
                "value": hex(_to_wei(value)) if value != "0" else "0x0",
            }
            gas = await self._estimate_gas(tx_for_estimate)
            gas = int(gas * self._gas_multiplier)
            tx = {
                "nonce": nonce,
                "value": _to_wei(value) if value != "0" else 0,
                "gas": gas,
                "gasPrice": gas_price,
                "chainId": self.chain_id,
                "data": data,
            }
            tx_hash = await self._sign_and_send(key, tx)
            receipt = await self._wait_for_receipt(tx_hash)
            contract_address = receipt.get("contractAddress")
            if not contract_address:
                raise RuntimeError(f"No contract address in receipt (tx_hash={tx_hash})")
            return {"tx_hash": tx_hash, "contract_address": contract_address}
        except Exception as e:
            raise RuntimeError(_sanitize_error(str(e), key)) from None
        finally:
            del acct

    async def close(self) -> None:
        await self._client.aclose()
