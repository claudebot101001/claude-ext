"""Post-task reflection engine for knowledge graph evolution.

Two-tier design:
  L1 (deterministic, zero-cost): Runs after every delivery.
     - Boost importance of accessed notes
     - Extract keyword overlaps from result text
     - Suggest relations when 2+ memory paths co-occur

  L2 (LLM-assisted, optional): Triggered conditionally.
     - Controlled by config (extensions.memory.reflection.llm_enabled)
     - Only fires when result text contains vulnerability-related keywords
     - Uses Sonnet for structured analysis (no tools, single turn)
     - Returns structured JSON: keyword additions, relation suggestions,
       importance adjustments, and optional new note creation
"""

import json as _json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Words to ignore in keyword extraction
_STOP_WORDS = frozenset(
    [
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "shall",
        "should",
        "may",
        "might",
        "can",
        "could",
        "of",
        "in",
        "to",
        "for",
        "on",
        "with",
        "at",
        "by",
        "from",
        "as",
        "into",
        "through",
        "during",
        "before",
        "after",
        "above",
        "below",
        "between",
        "under",
        "again",
        "further",
        "then",
        "once",
        "here",
        "there",
        "when",
        "where",
        "why",
        "how",
        "all",
        "each",
        "every",
        "both",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "no",
        "nor",
        "not",
        "only",
        "own",
        "same",
        "so",
        "than",
        "too",
        "very",
        "just",
        "don't",
        "should've",
        "that",
        "this",
        "it",
        "its",
        "and",
        "but",
        "or",
        "if",
    ]
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


@dataclass
class SetImportance:
    path: str
    kind: str = "set_importance"
    importance: float = 0.5


@dataclass
class CreateNote:
    """L2-only: create a new knowledge note from reflection analysis."""

    path: str
    kind: str = "create_note"
    content: str = ""
    tags: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    importance: float = 0.5
    relations: list[dict] = field(default_factory=list)


ReflectionAction = BoostImportance | AddKeywords | SuggestRelation | SetImportance | CreateNote


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

    def apply(self, actions: list[ReflectionAction], store=None) -> int:
        """Apply reflection actions to the knowledge graph. Returns count applied.

        Args:
            store: Optional MemoryStore for CreateNote actions (file I/O).
        """
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

                elif isinstance(action, SetImportance):
                    # Only update if the path exists in the graph (avoid phantom entries)
                    if self.graph.get_meta(action.path) is not None:
                        self.graph.set_meta(action.path, importance=action.importance)
                        applied += 1

                elif isinstance(action, CreateNote):
                    if store is not None:
                        # Safety: L2-created notes restricted to topics/ prefix
                        if not action.path.startswith("topics/"):
                            log.warning(
                                "L2 CreateNote blocked: path %r not under topics/",
                                action.path,
                            )
                            continue
                        from extensions.memory.frontmatter import (
                            NoteMeta,
                            serialize_frontmatter,
                        )

                        meta = NoteMeta(
                            tags=action.tags,
                            keywords=action.keywords,
                            importance=action.importance,
                        )
                        full_content = serialize_frontmatter(meta, action.content)
                        store.write(action.path, full_content)
                        # Add relations
                        for rel in action.relations:
                            target = rel.get("target", "")
                            rel_type = rel.get("type", "related")
                            if target and rel_type:
                                self.graph.add_relation(action.path, target, rel_type)
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

    # -- L2: LLM-assisted reflection -----------------------------------------

    _VULN_KEYWORDS = frozenset(
        {
            "vulnerability",
            "exploit",
            "reentrancy",
            "overflow",
            "underflow",
            "flash loan",
            "price manipulation",
            "access control",
            "privilege escalation",
            "delegatecall",
            "storage collision",
            "front-running",
            "sandwich",
            "oracle manipulation",
            "unchecked return",
            "integer overflow",
            "rug pull",
        }
    )

    def should_trigger_l2(self, result_text: str) -> bool:
        """Check if L2 (LLM) reflection should be triggered.

        Conditions:
        - Config enables LLM reflection
        - Result text contains vulnerability-related keywords
        """
        if not self.config.get("llm_enabled", False):
            return False

        text_lower = result_text.lower()
        return any(kw in text_lower for kw in self._VULN_KEYWORDS)

    def build_l2_prompt(self, result_text: str, existing_notes: list[dict]) -> str:
        """Build the Sonnet prompt for L2 reflection.

        Args:
            result_text: The delivery result text to analyze.
            existing_notes: List of {path, keywords, tags, importance} from graph.
        """
        # Truncate result to avoid huge prompts
        max_result = 4000
        truncated = result_text[:max_result]
        if len(result_text) > max_result:
            truncated += "\n... [truncated]"

        # Build existing notes context
        notes_ctx = ""
        for n in existing_notes[:30]:  # Cap at 30 notes
            kw = ", ".join(n.get("keywords", [])[:10])
            tags = ", ".join(n.get("tags", [])[:5])
            notes_ctx += (
                f"- {n['path']} [imp={n.get('importance', 0.5)}, tags={tags}, keywords={kw}]\n"
            )

        return f"""Analyze this task result for knowledge graph updates. You are curating a security research knowledge base.

## Task Result
{truncated}

## Existing Knowledge Notes
{notes_ctx if notes_ctx else "(none)"}

## Instructions
Analyze the result and output a JSON object with these optional fields:

1. **keyword_updates**: Add domain-specific keywords to existing notes when the result reveals they're relevant.
   Format: [{{"path": "topics/x.md", "add_keywords": ["keyword1", "keyword2"]}}]

2. **relation_suggestions**: Create edges between notes that share patterns discovered in this result.
   Format: [{{"source": "topics/a.md", "target": "topics/b.md", "type": "shares_pattern"}}]
   Types: related, depends_on, similar_to, caused_by, exploits, mitigates, composes_with, shares_pattern

3. **importance_updates**: Adjust importance when the result reveals a note is more/less critical.
   Format: [{{"path": "topics/x.md", "importance": 0.8}}]

4. **new_notes**: Create a new knowledge note if the result contains a novel pattern/finding worth preserving.
   Format: [{{"path": "topics/pattern-name.md", "content": "# Title\\n...", "tags": [...], "keywords": [...], "importance": 0.7, "relations": [{{"target": "...", "type": "..."}}]}}]
   Only create notes for genuinely novel findings — not routine task output.

Output ONLY valid JSON. No markdown fences. Empty arrays for fields with no suggestions.
Example: {{"keyword_updates": [], "relation_suggestions": [], "importance_updates": [], "new_notes": []}}"""

    def parse_l2_response(self, response: str) -> list[ReflectionAction]:
        """Parse Sonnet's JSON response into ReflectionActions."""
        actions: list[ReflectionAction] = []

        # Strip markdown fences if present
        text = response.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            # Remove first and last fence lines
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines)

        try:
            data = _json.loads(text)
        except _json.JSONDecodeError:
            log.warning("L2 reflection: failed to parse JSON response")
            return []

        if not isinstance(data, dict):
            return []

        # keyword_updates
        for update in data.get("keyword_updates", []):
            path = update.get("path", "")
            kw = update.get("add_keywords", [])
            if path and kw and isinstance(kw, list):
                actions.append(AddKeywords(path=path, keywords=[str(k) for k in kw[:10]]))

        # relation_suggestions
        for rel in data.get("relation_suggestions", []):
            source = rel.get("source", "")
            target = rel.get("target", "")
            rel_type = rel.get("type", "related")
            if source and target:
                actions.append(SuggestRelation(path=source, target=target, rel_type=str(rel_type)))

        # importance_updates
        for update in data.get("importance_updates", []):
            path = update.get("path", "")
            imp = update.get("importance")
            if path and imp is not None:
                try:
                    actions.append(
                        SetImportance(path=path, importance=max(0.0, min(1.0, float(imp))))
                    )
                except (ValueError, TypeError):
                    pass

        # new_notes
        for note in data.get("new_notes", []):
            path = note.get("path", "")
            content = note.get("content", "")
            if path and content:
                try:
                    imp = max(0.0, min(1.0, float(note.get("importance", 0.5))))
                except (ValueError, TypeError):
                    imp = 0.5
                actions.append(
                    CreateNote(
                        path=path,
                        content=content,
                        tags=[str(t) for t in note.get("tags", [])],
                        keywords=[str(k) for k in note.get("keywords", [])],
                        importance=imp,
                        relations=note.get("relations", []),
                    )
                )

        return actions
