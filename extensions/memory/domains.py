"""Domain manager — isolated MemoryStore + KnowledgeGraph per domain.

Each domain gets its own subdirectory under `memory_dir/domains/<name>/`
with independent FTS5 index, knowledge graph, and file locking.
"""

import logging
from pathlib import Path

from extensions.memory.graph import KnowledgeGraph
from extensions.memory.store import MemoryStore

log = logging.getLogger(__name__)


class DomainManager:
    """Factory for domain-scoped MemoryStore + KnowledgeGraph instances."""

    def __init__(self, memory_dir: Path, domain_configs: dict):
        self._base = memory_dir / "domains"
        self._base.mkdir(parents=True, exist_ok=True)
        self._configs = domain_configs
        self._stores: dict[str, MemoryStore] = {}
        self._graphs: dict[str, KnowledgeGraph] = {}

        for name, cfg in domain_configs.items():
            domain_dir = self._base / name
            domain_dir.mkdir(parents=True, exist_ok=True)
            (domain_dir / "topics").mkdir(exist_ok=True)

            half_life = cfg.get("knowledge_injection", {}).get("half_life_days", 30)
            store = MemoryStore(domain_dir)
            graph = KnowledgeGraph(domain_dir, half_life_days=half_life)

            self._stores[name] = store
            self._graphs[name] = graph
            log.info("Domain '%s' initialized at %s", name, domain_dir)

    def get_store(self, name: str) -> MemoryStore | None:
        return self._stores.get(name)

    def get_graph(self, name: str) -> KnowledgeGraph | None:
        return self._graphs.get(name)

    def get_config(self, name: str) -> dict:
        return self._configs.get(name, {})

    def list_domains(self) -> list[str]:
        return list(self._stores.keys())

    def close_all(self) -> None:
        for _name, graph in self._graphs.items():
            graph.close()
        for _name, store in self._stores.items():
            store.close()
        self._stores.clear()
        self._graphs.clear()
        log.info("All domain stores closed")
