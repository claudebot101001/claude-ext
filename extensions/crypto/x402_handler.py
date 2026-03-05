"""x402 payment protocol handler.

Implements the HTTP 402 payment flow:
1. Make request → get 402 with payment requirements (JSON body)
2. Sign EIP-712 payment authorization (EIP-3009 TransferWithAuthorization)
3. Retry with X-PAYMENT header

Runs in main process only (key isolation). Self-implemented (~100 lines)
to avoid x402 SDK dependency (supply chain risk).
"""

import json
import logging

import httpx
from eth_account import Account
from eth_account.messages import encode_typed_data

from .chains.evm import _sanitize_error, _secure_wipe

log = logging.getLogger(__name__)

# Default Coinbase CDP facilitator
_DEFAULT_FACILITATOR = "https://x402.org/facilitator"


class X402Handler:
    """Executes x402 payments. Runs in main process only (key isolation)."""

    def __init__(self, vault, chain_configs: dict):
        self._vault = vault
        self._chain_configs = chain_configs  # {chain_name: {rpc_url, chain_id, usdc_address, ...}}
        self._config: dict = {}  # {wallet, network, facilitator_url}

    def configure(self, wallet: str, network: str, facilitator_url: str | None = None):
        """Set which wallet and network to use for x402 payments."""
        self._config = {
            "wallet": wallet,
            "network": network,
            "facilitator_url": facilitator_url or _DEFAULT_FACILITATOR,
        }

    @property
    def is_configured(self) -> bool:
        return bool(self._config.get("wallet"))

    async def execute_request(
        self,
        url: str,
        method: str = "GET",
        headers: dict | None = None,
        body: str | None = None,
    ) -> dict:
        """Make HTTP request, auto-handle 402 with payment."""
        cfg = self._config
        if not cfg.get("wallet"):
            return {"error": "x402 not configured. Use x402_configure first."}

        key = None
        acct = None
        try:
            vault_key = f"crypto/{cfg['network']}/{cfg['wallet']}/privkey"
            key = self._vault.get(vault_key)
            if not key:
                return {"error": f"No key for wallet {cfg['wallet']} on {cfg['network']}"}

            async with httpx.AsyncClient(timeout=30) as client:
                # Step 1: Initial request
                resp = await client.request(method, url, headers=headers, content=body)

                if resp.status_code != 402:
                    return self._format_response(resp, payment_made=False)

                # Step 2: Parse 402 payment requirements from JSON body
                try:
                    pay_body = resp.json()
                except Exception:
                    return {
                        "error": "402 response has no valid JSON body",
                        "status_code": 402,
                    }

                accepts = pay_body.get("accepts", [])
                if not accepts:
                    return {"error": "402 response has no payment options"}

                # Find compatible payment option
                payment_req = self._find_compatible_option(accepts)
                if payment_req is None:
                    networks = [a.get("network", "?") for a in accepts]
                    return {
                        "error": f"No compatible payment option. Server accepts: {networks}. "
                        f"Configured: {cfg['network']}"
                    }

                # Step 3: Sign EIP-712 payment authorization
                acct = Account.from_key(key)
                payment_header = self._sign_payment(acct, payment_req)

                # Step 4: Retry with payment
                retry_headers = {**(headers or {}), "X-PAYMENT": payment_header}
                resp2 = await client.request(method, url, headers=retry_headers, content=body)

                return self._format_response(
                    resp2,
                    payment_made=True,
                    payment_info={
                        "amount": payment_req.get("maxAmountRequired", "0"),
                        "network": payment_req.get("network", cfg["network"]),
                        "from": acct.address,
                    },
                )

        except Exception as e:
            return {"error": _sanitize_error(str(e), key)}
        finally:
            _secure_wipe(key)
            del acct

    def _find_compatible_option(self, accepts: list[dict]) -> dict | None:
        """Find a payment option matching our configured network (exact match only)."""
        cfg_network = self._config.get("network", "")
        for option in accepts:
            if option.get("network", "") == cfg_network:
                return option
        return None

    def _sign_payment(self, acct: Account, payment_req: dict) -> str:
        """Sign EIP-712 TransferWithAuthorization for EIP-3009 gasless USDC transfer.

        Returns the X-PAYMENT header value (JSON with signature + payment details).
        """
        network = payment_req.get("network", self._config.get("network", ""))
        chain_cfg = self._chain_configs.get(network, {})
        usdc_address = chain_cfg.get("usdc_address", "")
        chain_id = chain_cfg.get("chain_id", 1)

        if not usdc_address:
            raise ValueError(f"No USDC address configured for network '{network}'")

        amount = payment_req.get("maxAmountRequired", "0")
        recipient = payment_req.get("payeeAddress", "")
        nonce = payment_req.get("nonce", "0")
        deadline = payment_req.get("deadline", "0")

        # EIP-712 typed data for TransferWithAuthorization (EIP-3009)
        typed_data = {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                "TransferWithAuthorization": [
                    {"name": "from", "type": "address"},
                    {"name": "to", "type": "address"},
                    {"name": "value", "type": "uint256"},
                    {"name": "validAfter", "type": "uint256"},
                    {"name": "validBefore", "type": "uint256"},
                    {"name": "nonce", "type": "bytes32"},
                ],
            },
            "primaryType": "TransferWithAuthorization",
            "domain": {
                "name": "USD Coin",
                "version": "2",
                "chainId": chain_id,
                "verifyingContract": usdc_address,
            },
            "message": {
                "from": acct.address,
                "to": recipient,
                "value": int(amount),
                "validAfter": 0,
                "validBefore": int(deadline),
                "nonce": bytes.fromhex(nonce.removeprefix("0x").zfill(64))
                if isinstance(nonce, str)
                else nonce,
            },
        }

        signable = encode_typed_data(full_message=typed_data)
        signed = acct.sign_message(signable)

        # X-PAYMENT header is a JSON-encoded object
        payment_payload = {
            "x402Version": 1,
            "scheme": "exact",
            "network": network,
            "payload": {
                "signature": signed.signature.hex(),
                "authorization": {
                    "from": acct.address,
                    "to": recipient,
                    "value": str(amount),
                    "validAfter": "0",
                    "validBefore": str(deadline),
                    "nonce": nonce,
                },
            },
        }
        return json.dumps(payment_payload, separators=(",", ":"))

    @staticmethod
    def _format_response(
        resp: httpx.Response,
        payment_made: bool,
        payment_info: dict | None = None,
    ) -> dict:
        """Format HTTP response for return to caller."""
        result: dict = {
            "status_code": resp.status_code,
            "payment_made": payment_made,
        }
        content_type = resp.headers.get("content-type", "")
        if "json" in content_type:
            try:
                result["body"] = resp.json()
            except Exception:
                result["body"] = resp.text[:10000]
        else:
            result["body"] = resp.text[:10000]
        if payment_info:
            result["payment"] = payment_info
        return result
