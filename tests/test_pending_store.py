"""Tests for core.pending PendingStore."""

import asyncio

from core.pending import PendingStore


def test_len_empty():
    store = PendingStore()
    assert len(store) == 0


def test_len_after_register():
    async def _run():
        store = PendingStore()
        store.register("sess-1", {"q": "?"})
        assert len(store) == 1
        store.register("sess-2", {"q": "??"})
        assert len(store) == 2

    asyncio.run(_run())


def test_len_after_resolve():
    async def _run():
        store = PendingStore()
        entry = store.register("sess-1", {"q": "?"})
        assert len(store) == 1
        store.resolve(entry.key, "answer")
        # Entry is still present until wait() cleans up, but resolve is done
        assert len(store) == 1

    asyncio.run(_run())
