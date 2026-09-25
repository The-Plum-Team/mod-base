"""Build-time templating of the kit front end (MB6, SPEC §6.2).

Closed placeholder set ``{{mb:<name>}}`` (values HTML-escaped with ``quote=True``) allowed only in
text nodes and in ``href``, ``content`` and ``aria-label`` attribute values; non-nesting
``<!-- mb:if <condition> -->...<!-- mb:endif -->`` blocks with no ``else``. An unknown placeholder
or condition, a leftover ``{{`` or ``<!-- mb:``, or a placeholder in any other position is a build
error. ``theme_css`` generates ``assets/theme.css`` from ``config.theme``.

Placement is checked on the whole template (every block kept), so a misplaced placeholder inside a
block whose condition happens to be false is still an error: the result never depends on which
conditions a mod sets. A placeholder inside an attribute must sit in a double-quoted value of an
allowed attribute; text inside ``<script>``/``<style>``, comments and declarations may hold none.

``theme_css`` writes the configured palette as custom properties plus the few derived tokens the
stylesheet needs instead of colour literals (``--accent-rgb``, ``--accent-strong-rgb``,
``--surface-overlay``, ``--on-accent``, ``--code-well``, ``--backdrop``, ``--shadow-rgb``) and the
matching ``color-scheme``; the derivations reproduce Quick Skin's hand-written values exactly
(``--on-accent`` #07150c, ``--code-well`` #0b100d, ``--backdrop`` ``rgb(4 8 6 / 74%)``: a
near-black tint of the accent, so the dialog backdrop stays dark on a light palette too) and keep
text on accent buttons readable on a light palette. ``theme_color`` is the ``theme-color`` meta
value: the midpoint of the dark background and surface (Quick Skin's #111713).
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping
from html.parser import HTMLParser
from typing import Any

from mod_base.config import THEME_KEYS
from mod_base.errors import MbError

OWNER = "MB6"

PLACEHOLDERS = frozenset({
    "name", "tagline", "eyebrow", "description", "license", "repository_url", "issues_url", "actions_url",
    "primary_link_url", "primary_link_title", "meta_description", "theme_color",
})
CONDITIONS = frozenset({"icon", "primary_link", "families"})
ATTRIBUTES = frozenset({"href", "content", "aria-label"})

#: The largest template the renderer accepts (the kit's own pages are a few KiB).
MAX_TEMPLATE_CHARS = 256 * 1024
#: The largest substituted value (config text is at most 400 characters; URLs at most 2048).
MAX_VALUE_CHARS = 4096

_PLACEHOLDER = re.compile(r"\{\{mb:([a-z_]+)\}\}")
_DIRECTIVE = re.compile(r"<!-- mb:(?:if ([a-z_]+)|endif) -->")
_DIRECTIVE_START = "<!-- mb:"
_COLOUR = re.compile(r"^#[0-9a-f]{6}$")
#: One attribute of a start tag, as the HTML tokenizer reads it (name, optional value).
_ATTRIBUTE = re.compile(r"""\s*([^\s"'<>/=]+)(?:\s*=\s*("[^"]*"|'[^']*'|[^\s"'=<>`]+))?""")
_TAG_NAME = re.compile(r"<([a-zA-Z][^\s/>]*)")
_TAG_END = re.compile(r"\s*/?>$")

#: Quick Skin's hand-written derivations: accent-button text is the strong accent scaled by this
#: factor (#40c973 -> #07150c), code wells are the background scaled by 0.88 (#0d120f -> #0b100d) and
#: the dialog backdrop is the accent scaled by 0.036 (#77e39b -> rgb(4 8 6)).
_ON_ACCENT_SCALE = 0.105
_CODE_WELL_SCALE = 0.88
_BACKDROP_SCALE = 0.036
_SURFACE_OVERLAY_ALPHA = "88%"
_BACKDROP_ALPHA = "74%"


class TemplateError(MbError):
    """A kit template or theme violates the templating rules (exit 2)."""

    default_reason = "template"


def _fail(message: str) -> TemplateError:
    return TemplateError(message[:300])


def _check_inputs(values: Mapping[str, str], conditions: Mapping[str, bool]) -> None:
    if not isinstance(values, Mapping) or set(values) != PLACEHOLDERS:
        raise _fail(f"values must name exactly the placeholders {sorted(PLACEHOLDERS)}")
    for name, value in values.items():
        if not isinstance(value, str) or not value or len(value) > MAX_VALUE_CHARS:
            raise _fail(f"placeholder value {name!r} must be non-empty text of at most {MAX_VALUE_CHARS} characters")
        if "{{" in value or "}}" in value or _DIRECTIVE_START in value:
            raise _fail(f"placeholder value {name!r} may not contain template syntax")
    if not isinstance(conditions, Mapping) or set(conditions) != CONDITIONS:
        raise _fail(f"conditions must name exactly {sorted(CONDITIONS)}")
    for name, value in conditions.items():
        if not isinstance(value, bool):
            raise _fail(f"condition {name!r} must be a boolean")


def _blocks(template: str) -> list[tuple[int, int, str | None]]:
    """Every directive as ``(start, end, condition or None for endif)``, checked for pairing."""

    found: list[tuple[int, int, str | None]] = []
    open_condition: str | None = None
    position = template.find(_DIRECTIVE_START)
    while position != -1:
        match = _DIRECTIVE.match(template, position)
        if match is None:
            raise _fail("a <!-- mb: directive is not exactly '<!-- mb:if <condition> -->' or '<!-- mb:endif -->'")
        condition = match.group(1)
        if condition is not None:
            if condition not in CONDITIONS:
                raise _fail(f"unknown template condition {condition!r}")
            if open_condition is not None:
                raise _fail("template blocks cannot nest")
            open_condition = condition
        else:
            if open_condition is None:
                raise _fail("<!-- mb:endif --> without an open block")
            open_condition = None
        found.append((match.start(), match.end(), condition))
        position = template.find(_DIRECTIVE_START, match.end())
    if open_condition is not None:
        raise _fail(f"template block {open_condition!r} is never closed")
    return found


class _Placement(HTMLParser):
    """Refuse a placeholder anywhere but a text node or an allowed double-quoted attribute value."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.raw_text: str | None = None

    def _start(self, tag: str) -> None:
        raw = self.get_starttag_text() or ""
        name = _TAG_NAME.match(raw)
        if name is None or "{{" in name.group(1):
            raise _fail("a placeholder may not name an element")
        position = name.end()
        while True:
            attribute = _ATTRIBUTE.match(raw, position)
            if attribute is None or attribute.end() == position:
                break
            attribute_name, value = attribute.group(1), attribute.group(2)
            if "{{" in attribute_name:
                raise _fail("a placeholder may not name an attribute")
            if value is not None and "{{" in value and (
                    attribute_name.lower() not in ATTRIBUTES or not value.startswith('"')):
                raise _fail(f"a placeholder may appear only in double-quoted {sorted(ATTRIBUTES)} values, "
                            f"not in {attribute_name!r}")
            position = attribute.end()
        if _TAG_END.match(raw, position) is None:
            raise _fail(f"cannot parse the <{tag}> start tag")
        if tag in ("script", "style"):
            self.raw_text = tag

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag)
        self.raw_text = None

    def handle_endtag(self, tag: str) -> None:
        if tag == self.raw_text:
            self.raw_text = None

    def handle_data(self, data: str) -> None:
        if self.raw_text is not None and "{{" in data:
            raise _fail(f"a placeholder may not appear inside <{self.raw_text}>")

    def handle_comment(self, data: str) -> None:
        if "{{" in data:
            raise _fail("a placeholder may not appear inside a comment")

    def _declaration(self, data: str) -> None:
        if "{{" in data:
            raise _fail("a placeholder may not appear inside a declaration")

    handle_decl = _declaration
    handle_pi = _declaration
    unknown_decl = _declaration


def lint(template: str) -> None:
    """Check ``template`` against every rule without substituting anything (the template lint of
    the kit's own tests and the first step of :func:`render`)."""

    if not isinstance(template, str) or len(template) > MAX_TEMPLATE_CHARS:
        raise _fail(f"a template must be text of at most {MAX_TEMPLATE_CHARS} characters")
    blocks = _blocks(template)
    unmarked, position = [], 0
    for start, end, _ in blocks:
        unmarked.append(template[position:start])
        position = end
    unmarked.append(template[position:])
    text = "".join(unmarked)
    for match in _PLACEHOLDER.finditer(text):
        if match.group(1) not in PLACEHOLDERS:
            raise _fail(f"unknown template placeholder {match.group(1)!r}")
    remainder = _PLACEHOLDER.sub("", text)
    if "{{" in remainder or "}}" in remainder:
        raise _fail("a template holds a leftover '{{' or '}}' that is not a placeholder")
    parser = _Placement()
    parser.feed(text)
    parser.close()


def _whole_line(template: str, start: int, end: int) -> tuple[int, int]:
    """Widen a directive that stands alone on its line to the whole line, so a resolved block
    leaves no blank indented line behind."""

    line_start = template.rfind("\n", 0, start) + 1
    if template[line_start:start].strip(" \t") or template[end:end + 1] != "\n":
        return start, end
    return line_start, end + 1


def render(template: str, values: Mapping[str, str], conditions: Mapping[str, bool]) -> str:
    """Substitute ``values`` (exactly :data:`PLACEHOLDERS`) and resolve ``conditions`` (exactly
    :data:`CONDITIONS`); raise :class:`mod_base.errors.MbError` on any template violation."""

    _check_inputs(values, conditions)
    lint(template)
    kept, position, keep = [], 0, True
    for start, end, condition in _blocks(template):
        start, end = _whole_line(template, start, end)
        if keep:
            kept.append(template[position:start])
        keep = True if condition is None else conditions[condition]
        position = end
    kept.append(template[position:])
    resolved = "".join(kept)
    rendered = _PLACEHOLDER.sub(lambda match: html.escape(values[match.group(1)], quote=True), resolved)
    if "{{" in rendered or _DIRECTIVE_START in rendered:
        raise _fail("template syntax survived rendering")
    return rendered


# -- Theme -------------------------------------------------------------------------------------------


def _palette(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != set(THEME_KEYS):
        raise _fail(f"{label} must hold exactly the colours {list(THEME_KEYS)}")
    for key in THEME_KEYS:
        if not isinstance(value[key], str) or not _COLOUR.fullmatch(value[key]):
            raise _fail(f"{label}.{key} must match ^#[0-9a-f]{{6}}$")
    return {key: value[key] for key in THEME_KEYS}


def _rgb(colour: str) -> tuple[int, int, int]:
    return int(colour[1:3], 16), int(colour[3:5], 16), int(colour[5:7], 16)


def _scaled(colour: str, factor: float) -> tuple[int, int, int]:
    red, green, blue = _rgb(colour)
    return tuple(min(255, int(channel * factor + 0.5)) for channel in (red, green, blue))  # type: ignore[return-value]


def _hex(channels: tuple[int, int, int]) -> str:
    return "#" + "".join(f"{channel:02x}" for channel in channels)


def _triple(channels: tuple[int, int, int]) -> str:
    return " ".join(str(channel) for channel in channels)


def _luminance(colour: str) -> float:
    red, green, blue = _rgb(colour)
    return (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255


def _tokens(palette: Mapping[str, str], scheme: str) -> str:
    """One ``:root`` rule: the palette plus the derived tokens of ``styles.css``."""

    strong = palette["accent_strong"]
    on_accent = _hex(_scaled(strong, _ON_ACCENT_SCALE)) if _luminance(strong) >= 0.5 else palette["bg"]
    declarations = [f"color-scheme:{scheme}"]
    declarations += [f"--{key.replace('_', '-')}:{palette[key]}" for key in THEME_KEYS]
    declarations += [
        f"--accent-rgb:{_triple(_rgb(palette['accent']))}",
        f"--accent-strong-rgb:{_triple(_rgb(strong))}",
        f"--surface-overlay:rgb({_triple(_rgb(palette['surface']))} / {_SURFACE_OVERLAY_ALPHA})",
        f"--on-accent:{on_accent}",
        f"--code-well:{_hex(_scaled(palette['bg'], _CODE_WELL_SCALE))}",
        f"--backdrop:rgb({_triple(_scaled(palette['accent'], _BACKDROP_SCALE))} / {_BACKDROP_ALPHA})",
        "--shadow-rgb:0 0 0",
    ]
    return ":root{" + ";".join(declarations) + "}"


def theme_color(theme: Mapping[str, Any]) -> str:
    """The ``theme-color`` meta value: the midpoint of ``theme.dark.bg`` and ``theme.dark.surface``
    rounded half up (Quick Skin's #0d120f and #141b16 give its hand-written #111713)."""

    if not isinstance(theme, Mapping) or "dark" not in theme:
        raise _fail("theme must hold 'dark'")
    dark = _palette(theme["dark"], "theme.dark")
    background, surface = _rgb(dark["bg"]), _rgb(dark["surface"])
    middle = tuple((first + second + 1) // 2 for first, second in zip(background, surface))
    return _hex(middle)  # type: ignore[arg-type]


def theme_css(theme: Mapping[str, Any]) -> str:
    """``:root{--bg:...}`` from ``theme.dark`` plus, when ``theme.light`` is set, a
    ``@media (prefers-color-scheme: light){:root{...}}`` block; values are validated colours."""

    if not isinstance(theme, Mapping) or set(theme) != {"dark", "light"}:
        raise _fail("theme must hold exactly 'dark' and 'light'")
    dark = _palette(theme["dark"], "theme.dark")
    lines = ["/* Generated by mod-base from site/mod-base.json (theme); do not edit. */", _tokens(dark, "dark")]
    if theme["light"] is not None:
        light = _palette(theme["light"], "theme.light")
        lines.append("@media (prefers-color-scheme: light){" + _tokens(light, "light") + "}")
    return "\n".join(lines) + "\n"
