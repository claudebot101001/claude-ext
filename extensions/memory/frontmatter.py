"""YAML frontmatter parser/serializer for memory notes.

Handles optional YAML frontmatter at the top of Markdown files.
Files without frontmatter work transparently (defaults applied).

Frontmatter stores only *intentional* metadata: tags, keywords, importance,
created timestamp, and relations. Access-tracking (accessed, access_count)
lives only in SQLite — never in frontmatter.
"""

import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Must start at byte 0, DOTALL for multi-line YAML
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# Relation types are free-form strings with format validation only.
# Recommended types listed in MCP tool descriptions, not enforced here.
_RELATION_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]{0,49}$")


@dataclass
class Relation:
    target: str
    type: str
    weight: float = 1.0


@dataclass
class NoteMeta:
    tags: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    importance: float = 0.5
    created: str = ""
    relations: list[Relation] = field(default_factory=list)


def validate_relation_type(rel_type: str) -> bool:
    """Check relation type format: lowercase alphanumeric + underscore."""
    return bool(_RELATION_TYPE_RE.match(rel_type))


def parse_frontmatter(text: str) -> tuple[NoteMeta, str]:
    """Parse optional YAML frontmatter from markdown text.

    Returns (metadata, body) where body is the content after frontmatter.
    If no frontmatter or parse error, returns (NoteMeta(), full_text).
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return NoteMeta(), text

    yaml_str = match.group(1)
    body = text[match.end() :]

    try:
        import yaml

        data = yaml.safe_load(yaml_str)
    except Exception as e:
        log.warning("Malformed YAML frontmatter, ignoring: %s", e)
        return NoteMeta(), text

    if not isinstance(data, dict):
        return NoteMeta(), text

    meta = NoteMeta()

    # tags
    raw_tags = data.get("tags", [])
    if isinstance(raw_tags, list):
        meta.tags = [str(t).strip() for t in raw_tags if t]

    # keywords
    raw_kw = data.get("keywords", [])
    if isinstance(raw_kw, list):
        meta.keywords = [str(k).strip() for k in raw_kw if k]

    # importance
    raw_imp = data.get("importance")
    if raw_imp is not None:
        try:
            meta.importance = max(0.0, min(1.0, float(raw_imp)))
        except (ValueError, TypeError):
            pass

    # created
    raw_created = data.get("created", "")
    if raw_created:
        from datetime import datetime

        try:
            datetime.fromisoformat(str(raw_created).replace("Z", "+00:00"))
            meta.created = str(raw_created)
        except (ValueError, TypeError):
            log.warning("Invalid created timestamp in frontmatter: %s", raw_created)

    # relations
    raw_rels = data.get("relations", [])
    if isinstance(raw_rels, list):
        for r in raw_rels:
            if not isinstance(r, dict):
                continue
            target = r.get("target", "")
            rel_type = r.get("type", "")
            if not target or not rel_type:
                continue
            if not validate_relation_type(rel_type):
                log.warning("Invalid relation type format: %r", rel_type)
                continue
            weight = 1.0
            raw_w = r.get("weight")
            if raw_w is not None:
                try:
                    weight = float(raw_w)
                except (ValueError, TypeError):
                    pass
            meta.relations.append(Relation(target=str(target), type=str(rel_type), weight=weight))

    return meta, body


def serialize_frontmatter(meta: NoteMeta, body: str) -> str:
    """Serialize NoteMeta into YAML frontmatter prepended to body.

    Only writes frontmatter if there's meaningful metadata beyond defaults.
    """
    import yaml

    data: dict = {}

    if meta.tags:
        data["tags"] = meta.tags
    if meta.keywords:
        data["keywords"] = meta.keywords
    if meta.importance != 0.5:
        data["importance"] = round(meta.importance, 2)
    if meta.created:
        data["created"] = meta.created
    if meta.relations:
        data["relations"] = [
            {"target": r.target, "type": r.type}
            if r.weight == 1.0
            else {"target": r.target, "type": r.type, "weight": round(r.weight, 2)}
            for r in meta.relations
        ]

    if not data:
        return body

    yaml_str = yaml.safe_dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)
    return f"---\n{yaml_str}---\n{body}"


def strip_frontmatter(text: str) -> str:
    """Remove frontmatter from text, returning only body."""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return text
    return text[match.end() :]


def merge_meta(existing: NoteMeta, updates: dict) -> NoteMeta:
    """Merge partial updates into existing metadata.

    Only keys present in `updates` are changed. Missing keys keep existing values.
    """
    merged = NoteMeta(
        tags=list(existing.tags),
        keywords=list(existing.keywords),
        importance=existing.importance,
        created=existing.created,
        relations=list(existing.relations),
    )

    if "tags" in updates:
        merged.tags = [str(t).strip() for t in updates["tags"] if t]
    if "keywords" in updates:
        merged.keywords = [str(k).strip() for k in updates["keywords"] if k]
    if "importance" in updates:
        try:
            merged.importance = max(0.0, min(1.0, float(updates["importance"])))
        except (ValueError, TypeError):
            pass
    if "created" in updates:
        merged.created = str(updates["created"])

    return merged
