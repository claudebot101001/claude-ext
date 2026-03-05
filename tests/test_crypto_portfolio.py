"""Tests for crypto portfolio store."""


import pytest

from extensions.crypto.portfolio import PortfolioStore


@pytest.fixture
def store(tmp_path):
    return PortfolioStore(tmp_path / "crypto")


class TestPortfolioStore:
    def test_add_wallet(self, store):
        entry = store.add_wallet("0xABC", "ethereum", "main")
        assert entry["address"] == "0xABC"
        assert entry["chain"] == "ethereum"
        assert entry["label"] == "main"
        assert "created_at" in entry

    def test_list_wallets_empty(self, store):
        assert store.list_wallets() == []

    def test_list_wallets_returns_all(self, store):
        store.add_wallet("0xA", "ethereum")
        store.add_wallet("0xB", "base")
        assert len(store.list_wallets()) == 2

    def test_list_wallets_filter_chain(self, store):
        store.add_wallet("0xA", "ethereum")
        store.add_wallet("0xB", "base")
        result = store.list_wallets(chain="base")
        assert len(result) == 1
        assert result[0]["address"] == "0xB"

    def test_get_wallet_found(self, store):
        store.add_wallet("0xDEAD", "ethereum", "test")
        result = store.get_wallet("0xDEAD")
        assert result is not None
        assert result["label"] == "test"

    def test_get_wallet_case_insensitive(self, store):
        store.add_wallet("0xAbCd", "ethereum")
        assert store.get_wallet("0xabcd") is not None
        assert store.get_wallet("0xABCD") is not None

    def test_get_wallet_not_found(self, store):
        assert store.get_wallet("0xNONE") is None

    def test_persistence(self, tmp_path):
        state_dir = tmp_path / "crypto"
        store1 = PortfolioStore(state_dir)
        store1.add_wallet("0xPersist", "base", "saved")

        store2 = PortfolioStore(state_dir)
        assert len(store2.list_wallets()) == 1
        assert store2.get_wallet("0xPersist")["label"] == "saved"
