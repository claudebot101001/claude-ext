"""Helpers for extension-scoped session context state.

Top-level ``session.context`` remains the place for routing/public metadata
such as ``chat_id`` or domain selectors. Extension-internal control state is
stored under the reserved ``_extensions`` namespace to avoid leaking an
implicit global schema across extensions. Extensions may register legacy flat
keys they previously used so recovered sessions can be migrated centrally.
"""

from collections.abc import Mapping, MutableMapping
from typing import Any

EXTENSIONS_CONTEXT_KEY = "_extensions"
_LEGACY_EXTENSION_STATE_MAP: dict[str, tuple[str, str]] = {}


def register_legacy_keys(mapping: Mapping[str, tuple[str, str]]) -> None:
    """Register legacy flat context keys owned by an extension.

    Re-registering the same key with the same target is allowed. Conflicting
    registrations raise to surface accidental overlap between extensions.
    """
    for legacy_key, target in mapping.items():
        existing = _LEGACY_EXTENSION_STATE_MAP.get(legacy_key)
        if existing is not None and existing != target:
            raise ValueError(
                f"Legacy key {legacy_key!r} already registered for {existing}, cannot map to {target}"
            )
        _LEGACY_EXTENSION_STATE_MAP[legacy_key] = target


def _context_dict(session_or_context: Any) -> MutableMapping[str, Any]:
    if isinstance(session_or_context, MutableMapping):
        return session_or_context
    return session_or_context.context


def clone_public_context(session_or_context: Any) -> dict[str, Any]:
    """Return a shallow copy of public routing context only."""
    ctx = _context_dict(session_or_context)
    return {k: v for k, v in ctx.items() if k != EXTENSIONS_CONTEXT_KEY}


def extension_context(
    session_or_context: Any,
    namespace: str,
    *,
    create: bool = False,
) -> MutableMapping[str, Any] | None:
    """Return the dict for one extension namespace."""
    ctx = _context_dict(session_or_context)
    root = ctx.get(EXTENSIONS_CONTEXT_KEY)
    if not isinstance(root, MutableMapping):
        if not create:
            return None
        root = {}
        ctx[EXTENSIONS_CONTEXT_KEY] = root

    state = root.get(namespace)
    if not isinstance(state, MutableMapping):
        if not create:
            return None
        state = {}
        root[namespace] = state
    return state


def normalize_extension_state(
    session_or_context: Any,
    *,
    mapping: Mapping[str, tuple[str, str]] | None = None,
) -> bool:
    """Migrate legacy flat extension keys into the reserved namespace."""
    ctx = _context_dict(session_or_context)
    effective_mapping = _LEGACY_EXTENSION_STATE_MAP if mapping is None else mapping
    changed = False
    for legacy_key, (namespace, key) in effective_mapping.items():
        if legacy_key not in ctx:
            continue
        value = ctx.pop(legacy_key)
        state = extension_context(ctx, namespace, create=True)
        state.setdefault(key, value)
        changed = True
    return changed


def has_extension_state(
    session_or_context: Any,
    namespace: str,
    key: str,
) -> bool:
    """Return True if the namespaced key is present."""
    state = extension_context(session_or_context, namespace, create=False)
    return state is not None and key in state


def get_extension_state(
    session_or_context: Any,
    namespace: str,
    key: str,
    *,
    default: Any = None,
) -> Any:
    """Read extension state."""
    state = extension_context(session_or_context, namespace, create=False)
    if state is not None and key in state:
        return state[key]
    return default


def set_extension_state(
    session_or_context: Any,
    namespace: str,
    key: str,
    value: Any,
) -> None:
    """Write extension state into the namespace."""
    state = extension_context(session_or_context, namespace, create=True)
    state[key] = value


def pop_extension_state(
    session_or_context: Any,
    namespace: str,
    key: str,
    *,
    default: Any = None,
) -> Any:
    """Delete extension state from the namespace."""
    state = extension_context(session_or_context, namespace, create=False)
    if state is not None and key in state:
        return state.pop(key)
    return default


def export_legacy_context(
    session_or_context: Any,
    mapping: Mapping[str, tuple[str, str]],
) -> dict[str, Any]:
    """Project namespaced state back into a legacy flat dict view."""
    exported: dict[str, Any] = {}
    for legacy_key, (namespace, key) in mapping.items():
        if has_extension_state(session_or_context, namespace, key):
            exported[legacy_key] = get_extension_state(session_or_context, namespace, key)
    return exported
