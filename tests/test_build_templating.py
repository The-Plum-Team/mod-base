"""Build-time templating of the kit front end (SPEC §6.2): the closed placeholder set, their only
legal positions, the non-nesting condition blocks, escaping and the generated ``theme.css``."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from mod_base.errors import MbError
from mod_base.pages import templating
from mod_base.pages.build import SITE_ALLOWLIST, TEMPLATES
from mod_base.pages.templating import (
    ATTRIBUTES,
    CONDITIONS,
    PLACEHOLDERS,
    TemplateError,
    color_scheme,
    lint,
    render,
    theme_color,
    theme_css,
)

KIT = Path(__file__).resolve().parents[1]
CONFIGS = KIT / "tests" / "fixtures" / "documents" / "config"
QS_THEME = {"bg": "#0d120f", "surface": "#141b16", "surface_raised": "#1a241d", "surface_soft": "#202c24",
            "text": "#f3f7f3", "muted": "#a9b7ac", "line": "#304137", "accent": "#77e39b", "accent_strong": "#40c973",
            "highlight": "#e6c875", "danger": "#ff9b91", "image_well": "#080b09"}
BP_LIGHT = {"bg": "#f4f7fd", "surface": "#ffffff", "surface_raised": "#dbeaff", "surface_soft": "#eef3fb",
            "text": "#101828", "muted": "#475569", "line": "#cbd5e1", "accent": "#0369a1", "accent_strong": "#075985",
            "highlight": "#b45309", "danger": "#b42318", "image_well": "#000000"}


def values(**changes: str) -> dict[str, str]:
    base = {name: f"value of {name}" for name in PLACEHOLDERS}
    base.update(changes)
    return base


def conditions(**changes: bool) -> dict[str, bool]:
    return {**{name: True for name in CONDITIONS}, **changes}


class RenderTest(unittest.TestCase):
    def test_placeholders_are_escaped_in_text_and_allowed_attributes(self) -> None:
        template = ('<a href="{{mb:repository_url}}" aria-label="{{mb:name}} home">{{mb:name}}</a>'
                    '<meta name="description" content="{{mb:meta_description}}">')
        rendered = render(template, values(name='Q&S <b>"x"</b>', repository_url="https://example.org/a?b=1&c=2",
                                           meta_description="it's"), conditions())
        self.assertEqual(rendered, '<a href="https://example.org/a?b=1&amp;c=2" aria-label="Q&amp;S &lt;b&gt;&quot;x'
                                   '&quot;&lt;/b&gt; home">Q&amp;S &lt;b&gt;&quot;x&quot;&lt;/b&gt;</a>'
                                   '<meta name="description" content="it&#x27;s">')

    def test_blocks_are_kept_or_dropped_with_their_whole_lines(self) -> None:
        template = ("<p>\n  <!-- mb:if icon -->\n  <img src=\"i.png\" alt=\"\">\n  <!-- mb:endif -->\n"
                    "  <!-- mb:if families -->x<!-- mb:endif -->\n</p>\n")
        self.assertEqual(render(template, values(), conditions()), '<p>\n  <img src="i.png" alt="">\n  x\n</p>\n')
        self.assertEqual(render(template, values(), conditions(icon=False, families=False)), "<p>\n  \n</p>\n")

    def test_the_value_and_condition_sets_are_exact(self) -> None:
        cases = [
            (values(), {name: True for name in CONDITIONS - {"icon"}}),
            (values(), conditions(extra=True)),
            ({name: "v" for name in PLACEHOLDERS - {"name"}}, conditions()),
            ({**values(), "unknown": "v"}, conditions()),
            (values(), conditions(icon="yes")),  # type: ignore[arg-type]
            (values(name=""), conditions()),
            (values(name="{{mb:tagline}}"), conditions()),
            (values(name="a <!-- mb:if icon --> b"), conditions()),
        ]
        for given_values, given_conditions in cases:
            with self.subTest(values=sorted(given_values)[:2], conditions=given_conditions), \
                    self.assertRaises(TemplateError):
                render("<p>{{mb:name}}</p>", given_values, given_conditions)

    def test_every_template_violation_is_a_build_error(self) -> None:
        cases = {
            "unknown placeholder": "<p>{{mb:unknown}}</p>",
            "leftover braces": "<p>{{ name }}</p>",
            "closing braces": "<p>a }} b</p>",
            "unknown condition": "<!-- mb:if light -->x<!-- mb:endif -->",
            "malformed directive": "<!-- mb:if  icon -->x<!-- mb:endif -->",
            "else": "<!-- mb:if icon -->x<!-- mb:else -->y<!-- mb:endif -->",
            "nesting": "<!-- mb:if icon --><!-- mb:if families -->x<!-- mb:endif --><!-- mb:endif -->",
            "stray endif": "x<!-- mb:endif -->",
            "unclosed": "<!-- mb:if icon -->x",
            "forbidden attribute": '<img src="{{mb:repository_url}}" alt="">',
            "title attribute": '<a href="x" title="{{mb:name}}">x</a>',
            "data attribute": '<img class="brand-icon" data-rendering="{{mb:name}}" alt="">',
            "single-quoted value": "<a href='{{mb:repository_url}}'>x</a>",
            "unquoted value": "<a href={{mb:repository_url}}>x</a>",
            "attribute name": '<a {{mb:name}}="x">x</a>',
            "element name": "<{{mb:name}}>x</{{mb:name}}>",
            "comment": "<!-- {{mb:name}} -->",
            "script": "<script>const name = '{{mb:name}}';</script>",
            "style": "<style>.x::after{content:'{{mb:name}}'}</style>",
            "declaration": "<!DOCTYPE {{mb:name}}>",
            "hidden inside a false block": '<!-- mb:if icon --><img src="{{mb:name}}" alt=""><!-- mb:endif -->',
            "allowed name inside another value": "<a href=\"x\" title='href=\"{{mb:name}}\"'>x</a>",
        }
        for label, template in cases.items():
            with self.subTest(label), self.assertRaises(TemplateError):
                render(template, values(), conditions(icon=False))

    def test_errors_are_mb_errors_with_a_template_reason(self) -> None:
        with self.assertRaises(MbError) as caught:
            render("<p>{{mb:nope}}</p>", values(), conditions())
        self.assertEqual(caught.exception.reason, "template")


class KitTemplatesTest(unittest.TestCase):
    """Template lint of the kit's own pages (the ``Front end`` CI placeholder lint)."""

    def test_both_kit_pages_pass_the_lint_and_render_for_every_condition(self) -> None:
        self.assertEqual(TEMPLATES, ("index.html", "e2e/index.html"))
        self.assertTrue(set(TEMPLATES) <= set(SITE_ALLOWLIST))
        for relative in TEMPLATES:
            template = (KIT / "site" / relative).read_text(encoding="utf-8")
            lint(template)
            for icon in (True, False):
                for primary in (True, False):
                    for families in (True, False):
                        rendered = render(template, values(), conditions(icon=icon, primary_link=primary,
                                                                         families=families))
                        self.assertNotIn("{{", rendered)
                        self.assertNotIn("<!-- mb:", rendered)

    def test_the_pages_use_the_closed_set_only_where_allowed(self) -> None:
        used: set[str] = set()
        for relative in TEMPLATES:
            template = (KIT / "site" / relative).read_text(encoding="utf-8")
            used |= set(re.findall(r"\{\{mb:([a-z_]+)\}\}", template))
            for attribute, value in re.findall(r'([a-z-]+)="([^"]*\{\{mb:[^"]*)"', template):
                self.assertIn(attribute, ATTRIBUTES, (relative, value))
        self.assertEqual(used, PLACEHOLDERS)

    def test_both_pages_declare_the_color_scheme_through_its_placeholder(self) -> None:
        for relative in TEMPLATES:
            template = (KIT / "site" / relative).read_text(encoding="utf-8")
            self.assertEqual(template.count('<meta name="color-scheme" content="{{mb:color_scheme}}">'), 1, relative)
            for scheme in ("dark", "dark light"):
                rendered = render(template, values(color_scheme=scheme), conditions())
                self.assertEqual(re.findall(r'<meta name="color-scheme" content="([^"]*)">', rendered), [scheme])

    def test_the_icon_and_families_conditions_gate_the_icon_and_family_markup(self) -> None:
        landing = (KIT / "site" / "index.html").read_text(encoding="utf-8")
        gallery = (KIT / "site" / "e2e" / "index.html").read_text(encoding="utf-8")
        without = conditions(icon=False, primary_link=False, families=False)
        for page in (landing, gallery):
            self.assertIn("icon.png", render(page, values(), conditions()))
            self.assertNotIn("icon.png", render(page, values(), without))
        self.assertIn('id="family-views"', render(gallery, values(), conditions()))
        self.assertNotIn('id="family-views"', render(gallery, values(), without))
        self.assertNotIn("value of primary_link_url", render(landing, values(), without))
        self.assertIn('href="value of primary_link_url"', render(landing, values(), conditions()))


class ThemeTest(unittest.TestCase):
    @staticmethod
    def tokens(css: str, block: int = 0) -> dict[str, str]:
        bodies = re.findall(r":root\{([^}]*)\}", css)
        return dict(item.split(":", 1) for item in bodies[block].split(";"))

    def test_the_quick_skin_palette_reproduces_every_hand_written_value(self) -> None:
        css = theme_css({"dark": QS_THEME, "light": None})
        self.assertNotIn("@media", css)
        tokens = self.tokens(css)
        self.assertEqual(tokens["color-scheme"], "dark")
        for key, colour in QS_THEME.items():
            self.assertEqual(tokens["--" + key.replace("_", "-")], colour)
        self.assertEqual(tokens["--accent-rgb"], "119 227 155")
        self.assertEqual(tokens["--accent-strong-rgb"], "64 201 115")
        self.assertEqual(tokens["--surface-overlay"], "rgb(20 27 22 / 88%)")
        self.assertEqual(tokens["--on-accent"], "#07150c")
        self.assertEqual(tokens["--code-well"], "#0b100d")
        self.assertEqual(tokens["--backdrop"], "rgb(4 8 6 / 74%)")
        self.assertEqual(tokens["--shadow-rgb"], "0 0 0")
        self.assertEqual(theme_color({"dark": QS_THEME, "light": None}), "#111713")
        self.assertEqual(color_scheme({"dark": QS_THEME, "light": None}), "dark", "a dark-only site never paints light")
        self.assertTrue(css.endswith("}\n"))
        self.assertEqual(css, theme_css({"dark": dict(QS_THEME), "light": None}), "the output is deterministic")

    def test_a_light_palette_adds_a_preference_block_with_readable_accent_text(self) -> None:
        css = theme_css({"dark": QS_THEME, "light": BP_LIGHT})
        self.assertIn("@media (prefers-color-scheme: light){:root{", css)
        light = self.tokens(css, 1)
        self.assertEqual(light["color-scheme"], "light")
        self.assertEqual(light["--bg"], "#f4f7fd")
        self.assertEqual(light["--on-accent"], "#f4f7fd", "a dark accent carries light text")
        red, green, blue = (int(channel) for channel in light["--backdrop"].removeprefix("rgb(").split(" / ")[0].split())
        self.assertLessEqual(max(red, green, blue), 16, "the dialog backdrop stays dark on a light palette")
        self.assertEqual(theme_color({"dark": QS_THEME, "light": BP_LIGHT}), "#111713", "theme-color follows dark")
        self.assertEqual(color_scheme({"dark": QS_THEME, "light": BP_LIGHT}), "dark light")

    def test_the_fixture_configs_render_their_themes(self) -> None:
        for name in ("qs.json", "bp.json"):
            path = CONFIGS / name
            if not path.exists():
                continue
            config = json.loads(path.read_text(encoding="utf-8"))
            css = theme_css(config["theme"])
            self.assertEqual(css.count("@media"), 0 if config["theme"]["light"] is None else 1)
            self.assertEqual(color_scheme(config["theme"]), "dark" if config["theme"]["light"] is None else "dark light")

    def test_invalid_themes_are_rejected(self) -> None:
        cases = [
            {"dark": QS_THEME},
            {"dark": QS_THEME, "light": None, "extra": None},
            {"dark": {**QS_THEME, "bg": "#0D120F"}, "light": None},
            {"dark": {**QS_THEME, "bg": "red"}, "light": None},
            {"dark": {**QS_THEME, "bg": "#0d120f;x:y"}, "light": None},
            {"dark": {key: value for key, value in QS_THEME.items() if key != "bg"}, "light": None},
            {"dark": {**QS_THEME, "extra": "#000000"}, "light": None},
            {"dark": QS_THEME, "light": {**BP_LIGHT, "text": 3}},
        ]
        for theme in cases:
            for function in (theme_css, color_scheme):
                with self.subTest(theme=sorted(theme), function=function.__name__), self.assertRaises(TemplateError):
                    function(theme)  # type: ignore[arg-type]

    def test_the_theme_keys_are_the_config_keys(self) -> None:
        from mod_base.config import THEME_KEYS

        self.assertIs(templating.THEME_KEYS, THEME_KEYS)


if __name__ == "__main__":
    unittest.main()
