"""Post-task reflection engine for knowledge graph evolution.

Two-tier design:
  L1 (deterministic, zero-cost): Runs after every delivery.
     - Boost importance of accessed notes
     - Extract keyword overlaps from result text
     - Suggest relations when 2+ memory paths co-occur

  L2 (LLM-assisted, optional): Triggered conditionally.
     - Controlled by config (extensions.memory.reflection.llm_enabled)
     - Only fires when session context has "audit": true or result contains
       vulnerability-related keywords
     - Uses a lightweight model (Sonnet) for structured analysis
     - Stub implementation — full L2 is Phase 4 work
"""

import logging
import re
from collections import Counter
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Words to ignore in keyword extraction
_STOP_WORDS = frozenset(
    "the a an is are was were be been being have has had do does did will would "
    "shall should may might can could of in to for on with at by from as into "
    "through during before after above below between under again further then "
    "once here there when where why how all each every both few more most other "
    "some such no nor not only own same so than too very just don't should've "
    "that this it its and but or if".split()
)

# Memory path pattern (relative .md paths mentioned in result text)
_PATH_RE = re.compile(r"\b((?:topics|events|users)/[\w\-/]+\.md)\b")

# Min word length for keyword extraction
_MIN_WORD_LEN = 4


@dataclass
class BoostImportance:
    path: str
    kind: str = "boost"
    delta: float = 0.05


@dataclass
class AddKeywords:
    path: str
    kind: str = "add_keywords"
    keywords: list[str] | None = None


@dataclass
class SuggestRelation:
    path: str
    kind: str = "suggest_relation"
    target: str = ""
    rel_type: str = "related"


ReflectionAction = BoostImportance | AddKeywords | SuggestRelation


class ReflectionEngine:
    """Deterministic post-task reflection (L1).

    Analyzes delivery result text to evolve the knowledge graph.
    No LLM calls — pure text analysis.
    """

    def __init__(self, graph, config: dict | None = None):
        """
        Args:
            graph: KnowledgeGraph instance
            config: Optional config dict (extensions.memory.reflection)
        """
        self.graph = graph
        self.config = config or {}

    def reflect(self, session_id: str, result_text: str, metadata: dict) -> list[ReflectionAction]:
        """Analyze result text and produce reflection actions.

        Returns list of actions to apply. Does NOT apply them.
        """
        if not result_text:
            return []

        actions: list[ReflectionAction] = []

        # 1. Find memory paths mentioned in result
        mentioned_paths = set(_PATH_RE.findall(result_text))

        # 2. Boost importance for each mentioned path
        for path in mentioned_paths:
            actions.append(BoostImportance(path=path))

        # 3. Extract frequent words from result text
        result_keywords, word_freq = self._extract_keywords(result_text)

        # 4. For each mentioned path, find keyword overlap and suggest additions
        for path in mentioned_paths:
            meta = self.graph.get_meta(path)
            if not meta:
                continue
            existing_kw = set(meta.get("keywords", []))
            overlap = result_keywords & existing_kw
            new_kw = result_keywords - existing_kw
            # If there's overlap (result is about this note's topic), suggest new keywords
            if overlap and new_kw:
                # Only add top-3 most frequent new keywords
                scored = [(kw, word_freq.get(kw, 0)) for kw in new_kw]
                scored.sort(key=lambda x: x[1], reverse=True)
                top_new = [kw for kw, _ in scored[:3]]
                actions.append(AddKeywords(path=path, keywords=top_new))

        # 5. Suggest relations between co-mentioned paths
        path_list = sorted(mentioned_paths)
        for i, p1 in enumerate(path_list):
            for p2 in path_list[i + 1 :]:
                # Check if relation already exists
                existing_rels = self.graph.get_relations(p1)
                already_linked = any(
                    (r["source"] == p1 and r["target"] == p2)
                    or (r["source"] == p2 and r["target"] == p1)
                    for r in existing_rels
                )
                if not already_linked:
                    actions.append(SuggestRelation(path=p1, target=p2))

        return actions

    def apply(self, actions: list[ReflectionAction]) -> int:
        """Apply reflection actions to the knowledge graph. Returns count applied."""
        applied = 0
        for action in actions:
            try:
                if isinstance(action, BoostImportance):
                    result = self.graph.boost_importance(action.path, action.delta)
                    if result is not None:
                        applied += 1

                elif isinstance(action, AddKeywords):
                    meta = self.graph.get_meta(action.path)
                    if meta and action.keywords:
                        existing = meta.get("keywords", [])
                        merged = list(dict.fromkeys(existing + action.keywords))
                        self.graph.set_meta(action.path, keywords=merged)
                        applied += 1

                elif isinstance(action, SuggestRelation):
                    if self.graph.add_relation(action.path, action.target, action.rel_type):
                        applied += 1

            except Exception:
                log.exception("Failed to apply reflection action: %s", action)

        if applied:
            log.info("Reflection: applied %d/%d actions", applied, len(actions))
        return applied

    def _extract_keywords(self, text: str, min_freq: int = 2) -> tuple[set[str], Counter]:
        """Extract meaningful keywords from text via word frequency.

        Returns (keyword_set, word_frequency_counter).
        """
        words = re.findall(r"[a-z][a-z0-9_-]+", text.lower())
        word_freq = Counter(w for w in words if len(w) >= _MIN_WORD_LEN and w not in _STOP_WORDS)
        return {w for w, c in word_freq.items() if c >= min_freq}, word_freq

    # -- L2 stub (future: LLM-assisted reflection) --------------------------

    def should_trigger_l2(self, session_id: str, result_text: str, metadata: dict) -> bool:
        """Check if L2 (LLM) reflection should be triggered.

        Conditions:
        - Config enables LLM reflection
        - Result text contains vulnerability-related keywords
        """
        if not self.config.get("llm_enabled", False):
            return False

        # Check for vulnerability-related keywords
        vuln_keywords = {
            "vulnerability",
            "exploit",
            "reentrancy",
            "overflow",
            "underflow",
            "flash loan",
            "price manipulation",
            "access control",
            "privilege escalation",
        }
        text_lower = result_text.lower()
        if any(kw in text_lower for kw in vuln_keywords):
            return True

        return False
