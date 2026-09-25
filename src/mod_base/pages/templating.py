"""Build-time templating of the kit front end (MB6, SPEC §6.2).

Closed placeholder set ``{{mb:<name>}}`` (values HTML-escaped with ``quote=True``) allowed only in
text nodes and in ``href``, ``content`` and ``aria-label`` attribute values; non-nesting
``<!-- mb:if <condition> -->...<!-- mb:endif -->`` blocks with no ``else``. An unknown placeholder
or condition, a leftover ``{{`` or ``<!-- mb:``, or a placeholder in any other position is a build
error. ``theme_css`` generates ``assets/theme.css`` from ``config.theme``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

OWNER = "MB6"

PLACEHOLDERS = frozenset({
    "name", "tagline", "eyebrow", "description", "license", "repository_url", "issues_url", "actions_url",
    "primary_link_url", "primary_link_title", "meta_description", "theme_color",
})
CONDITIONS = frozenset({"icon", "primary_link", "families"})
ATTRIBUTES = frozenset({"href", "content", "aria-label"})


def render(template: str, values: Mapping[str, str], conditions: Mapping[str, bool]) -> str:
    """Substitute ``values`` (exactly :data:`PLACEHOLDERS`) and resolve ``conditions`` (exactly
    :data:`CONDITIONS`); raise :class:`mod_base.errors.MbError` on any template violation."""

    raise NotImplementedError("owned by MB6")


def theme_css(theme: Mapping[str, Any]) -> str:
    """``:root{--bg:...}`` from ``theme.dark`` plus, when ``theme.light`` is set, a
    ``@media (prefers-color-scheme: light){:root{...}}`` block; values are validated colours."""

    raise NotImplementedError("owned by MB6")
