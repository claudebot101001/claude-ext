"""Template registry — agent blueprints for session configuration.

A Template defines "who am I, what tools can I use, what do I produce".
Templates are channel-agnostic: Telegram, Discord, subagent, or any other
entry point creates sessions from the same registry.

Load order:
  1. YAML files from ``core/templates/*.yaml`` (built-in)
  2. YAML files registered by extensions via ``register_directory()``
  3. ``config["templates"]`` overrides (same-name entries replace/extend)

Each YAML file defines template metadata.  A co-located ``.md`` file with
the same stem provides the system prompt (optional).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger(__name__)


@dataclass
class Template:
    """Agent blueprint — defines capabilities and defaults for a session."""

    name: str
    description: str = ""
    system_prompt: str = ""
    working_dir: str | None = None
    context_defaults: dict = field(default_factory=dict)
    disallowed_tools: list[str] = field(default_factory=list)
    allowed_tools: list[str] | None = None  # None = inherit global
    mcp_servers: set[str] | None = None  # None = inherit global
    exclude_mcp_servers: set[str] = field(default_factory=set)
    exclude_mcp_tags: set[str] = field(default_factory=set)
    auto_cleanup: bool = True
    model: str | None = None  # per-template model override (e.g. "sonnet", "opus")
    inject_identity: bool = True  # False = skip constitution/personality/profile injection
    visibility: str = "public"  # "public" = user-visible, "internal" = extension-only
    owner: str | None = None  # owning extension name (documentation only)


# ---------------------------------------------------------------------------
# YAML → Template loader
# ---------------------------------------------------------------------------

_CORE_TEMPLATE_DIR = Path(__file__).parent / "templates"


def load_template_from_yaml(yaml_path: Path) -> Template:
    """Load a Template from a YAML file + optional co-located .md prompt.

    The template ``name`` is derived from the YAML filename stem unless
    explicitly set in the YAML.  A ``.md`` file with the same stem in the
    same directory provides the system prompt.
    """
    raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    name = raw.get("name", yaml_path.stem)

    # Co-located .md prompt file
    system_prompt = raw.get("system_prompt", "")
    md_path = yaml_path.with_suffix(".md")
    if md_path.is_file():
        try:
            system_prompt = md_path.read_text(encoding="utf-8").strip()
        except OSError:
            log.warning("Failed to read prompt file %s", md_path)

    return Template(
        name=name,
        description=raw.get("description", ""),
        system_prompt=system_prompt,
        working_dir=raw.get("working_dir"),
        context_defaults=raw.get("context_defaults", {}),
        disallowed_tools=raw.get("disallowed_tools", []),
        allowed_tools=raw.get("allowed_tools"),
        mcp_servers=set(raw["mcp_servers"]) if "mcp_servers" in raw else None,
        exclude_mcp_servers=set(raw.get("exclude_mcp_servers", [])),
        exclude_mcp_tags=set(raw.get("exclude_mcp_tags", [])),
        auto_cleanup=raw.get("auto_cleanup", True),
        model=raw.get("model"),
        inject_identity=raw.get("inject_identity", True),
        visibility=raw.get("visibility", "public"),
        owner=raw.get("owner"),
    )


def load_templates_from_directory(directory: Path) -> dict[str, Template]:
    """Load all ``*.yaml`` template files from a directory.

    Returns a dict keyed by template name.
    """
    templates: dict[str, Template] = {}
    if not directory.is_dir():
        return templates
    for yaml_file in sorted(directory.glob("*.yaml")):
        try:
            tpl = load_template_from_yaml(yaml_file)
            templates[tpl.name] = tpl
            log.debug("Loaded template %r from %s", tpl.name, yaml_file)
        except Exception:
            log.exception("Failed to load template from %s", yaml_file)
    return templates


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TemplateRegistry:
    """Config-backed template registry.

    Load order:
      1. YAML files from ``core/templates/*.yaml``
      2. ``config["templates"]`` overrides (same-name entries replace/extend)
      3. Extensions call ``register()`` or ``register_directory()`` at runtime
    """

    def __init__(self, config_templates: dict | None = None):
        self._templates: dict[str, Template] = {}
        # 1. Load core YAML templates
        self._templates.update(load_templates_from_directory(_CORE_TEMPLATE_DIR))
        # 2. Config overrides/additions
        if config_templates:
            self._load_config(config_templates)
        names = ", ".join(sorted(self._templates))
        log.info("TemplateRegistry loaded: %s", names)

    def _load_config(self, raw: dict) -> None:
        for name, tdef in raw.items():
            if not isinstance(tdef, dict):
                log.warning(
                    "Skipping template %r: expected dict, got %s",
                    name,
                    type(tdef).__name__,
                )
                continue
            # Config-level system_prompt_file overrides inline system_prompt
            system_prompt = tdef.get("system_prompt", "")
            prompt_file = tdef.get("system_prompt_file")
            if prompt_file:
                prompt_path = os.path.expanduser(prompt_file)
                try:
                    system_prompt = open(prompt_path, encoding="utf-8").read().strip()
                    log.info(
                        "Loaded custom prompt file for template %r: %s",
                        name,
                        prompt_path,
                    )
                except OSError:
                    log.warning(
                        "Template %r: system_prompt_file %r not found, using inline",
                        name,
                        prompt_path,
                    )
            self._templates[name] = Template(
                name=name,
                description=tdef.get("description", ""),
                system_prompt=system_prompt,
                working_dir=tdef.get("working_dir"),
                context_defaults=tdef.get("context_defaults", {}),
                disallowed_tools=tdef.get("disallowed_tools", []),
                allowed_tools=tdef.get("allowed_tools"),
                mcp_servers=set(tdef["mcp_servers"]) if "mcp_servers" in tdef else None,
                exclude_mcp_servers=set(tdef.get("exclude_mcp_servers", [])),
                exclude_mcp_tags=set(tdef.get("exclude_mcp_tags", [])),
                auto_cleanup=tdef.get("auto_cleanup", True),
                model=tdef.get("model"),
                inject_identity=tdef.get("inject_identity", True),
                visibility=tdef.get("visibility", "public"),
                owner=tdef.get("owner"),
            )

    def names(self, *, include_internal: bool = False) -> list[str]:
        """Return sorted list of registered template names.

        By default only ``public`` templates are returned.  Pass
        ``include_internal=True`` to include ``internal`` templates.
        """
        return sorted(
            n for n, t in self._templates.items() if include_internal or t.visibility == "public"
        )

    def list(self, *, include_internal: bool = False) -> list[Template]:
        """Return registered templates (sorted by name).

        By default only ``public`` templates are returned.
        """
        return [
            t
            for t in (self._templates[n] for n in sorted(self._templates))
            if include_internal or t.visibility == "public"
        ]

    def has(self, name: str) -> bool:
        return name in self._templates

    def get(self, name: str) -> Template | None:
        return self._templates.get(name)

    def require(self, name: str) -> Template:
        """Get template by name, raise KeyError if not found."""
        tpl = self._templates.get(name)
        if tpl is None:
            available = ", ".join(sorted(self._templates))
            raise KeyError(f"Template {name!r} not found. Available: {available}")
        return tpl

    def register(self, template: Template, *, override: bool = False) -> None:
        """Register a template at runtime (e.g. from an extension).

        Does not overwrite existing templates unless *override* is True.
        Config-loaded templates always take precedence (loaded at init time).
        """
        if template.name in self._templates and not override:
            log.debug("Template %r already registered, skipping", template.name)
            return
        self._templates[template.name] = template
        log.info("Template %r registered at runtime", template.name)

    def register_directory(self, directory: Path, *, override: bool = False) -> int:
        """Register all YAML templates from a directory.

        Returns the number of templates registered.
        """
        loaded = load_templates_from_directory(directory)
        count = 0
        for tpl in loaded.values():
            if tpl.name not in self._templates or override:
                self._templates[tpl.name] = tpl
                count += 1
                log.info("Template %r registered from %s", tpl.name, directory)
            else:
                log.debug("Template %r already registered, skipping", tpl.name)
        return count


# ---------------------------------------------------------------------------
# Session init helper — shared by Telegram, subagent, and future frontends
# ---------------------------------------------------------------------------


def resolve_template_session_init(
    registry: TemplateRegistry,
    template_name: str | None,
    *,
    default_working_dir: str,
    explicit_working_dir: str | None = None,
    base_context: dict | None = None,
) -> tuple[str, dict]:
    """Resolve template defaults into ``(working_dir, context)`` for session creation.

    Priority:
      - working_dir: *explicit* > template.working_dir > *default*
      - context: template.context_defaults (lowest) < base_context (overrides)
      - Sets ``_extensions.templates.name`` in context when a template is used

    Raises :class:`KeyError` if *template_name* is set but not found in *registry*.

    Returns:
        ``(working_dir, context)`` ready for ``SessionManager.create_session()``.
    """
    from core.session_context import set_extension_state

    context: dict = {}

    if template_name:
        template = registry.require(template_name)
        # context_defaults = lowest priority layer
        if template.context_defaults:
            context.update(template.context_defaults)
        # working_dir: explicit > template > default
        raw_dir = explicit_working_dir or template.working_dir or default_working_dir
    else:
        raw_dir = explicit_working_dir or default_working_dir

    # Resolve ~ and relative paths (relative to default_working_dir)
    working_dir = os.path.expanduser(raw_dir)
    if not os.path.isabs(working_dir):
        working_dir = os.path.normpath(os.path.join(default_working_dir, working_dir))

    # base_context overrides template defaults
    if base_context:
        context.update(base_context)

    # Stamp template identity so the per-prompt customizer can apply restrictions
    if template_name:
        set_extension_state(context, "templates", "name", template_name)

    return working_dir, context
