"""Small strict validator combinators shared by every document kind, the config and the adapter.

A validator is ``validate(value, path) -> value``. It never coerces: a ``bool`` is never an
integer, an integer field rejects floats, and a number field accepts JSON integers or floats
(JSON has one number type) but never ``bool`` or a non-finite value. Objects reject unknown and
missing keys. Every failure raises :class:`DocumentError` whose message starts with a JSONPath-like
location (``$.frames[3].source.pixel.width``) so a rejection is actionable without echoing
attacker-sized payloads.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from mod_base.errors import MbError
from mod_base.model.canonical import has_surrogate

Validator = Callable[[Any, str], Any]


class DocumentError(MbError):
    """A document failed structural validation at ``path``."""

    default_reason = "invalid-document"

    def __init__(self, path: str, message: str) -> None:
        super().__init__(f"{path}: {message}")
        self.path = path


def fail(path: str, message: str) -> DocumentError:
    return DocumentError(path, message)


def _preview(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= 60 else text[:57] + "..."


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def is_evidence_text(value: Any, max_length: int) -> bool:
    """The QS runtime-evidence rule (V9) generalized to any bounded text field.

    A ``str`` of 1..``max_length`` characters that is non-empty after ``strip()`` and contains no
    character below U+0020, no DEL (U+007F) and no surrogate code point (U+D800..U+DFFF, which is
    not Unicode text and cannot be encoded as UTF-8). The rule is defined on code points only, so
    it is identical on every Python version.
    """

    return (
        isinstance(value, str)
        and 0 < len(value) <= max_length
        and bool(value.strip())
        and all(ord(character) >= 32 and ord(character) != 127 for character in value)
        and not has_surrogate(value)
    )


#: General categories (of the frozen Unicode 3.2 database) a display-text character may have:
#: letters, marks, numbers, punctuation and symbols. Controls (Cc), format characters such as bidi
#: overrides and zero-width characters (Cf), surrogates (Cs), private use (Co), unassigned code
#: points (Cn) and every separator except U+0020 (Zs, Zl, Zp) are refused.
DISPLAY_CATEGORIES = frozenset({
    "Lu", "Ll", "Lt", "Lm", "Lo", "Mn", "Mc", "Me", "Nd", "Nl", "No",
    "Pc", "Pd", "Ps", "Pe", "Pi", "Pf", "Po", "Sm", "Sc", "Sk", "So",
})


def _display_character(character: str) -> bool:
    return character == " " or unicodedata.ucd_3_2_0.category(character) in DISPLAY_CATEGORIES


def is_display_text(value: Any, max_length: int) -> bool:
    """Config/site copy: trimmed, non-empty, markup-free text of printable characters.

    Printability is decided by the frozen Unicode 3.2 database that ships with every CPython
    (``unicodedata.ucd_3_2_0``), never by the interpreter's own ``str.isprintable``: that one
    follows the running Python's Unicode version, so a character assigned after Unicode 14 would
    be valid on 3.13 and invalid on 3.11. Every character is either U+0020 or of a
    :data:`DISPLAY_CATEGORIES` category in Unicode 3.2 (characters assigned later are refused on
    every interpreter alike). Rejects ``<``, ``>``, ``{{`` and ``}}`` so no text can open a tag, a
    template placeholder or an ``<!-- mb:`` block.
    """

    return (
        isinstance(value, str)
        and 0 < len(value) <= max_length
        and value == value.strip()
        and all(_display_character(character) for character in value)
        and "<" not in value
        and ">" not in value
        and "{{" not in value
        and "}}" not in value
    )


def Any_() -> Validator:  # noqa: N802 - combinator naming
    def validate(value: Any, path: str) -> Any:
        return value

    return validate


def Str(  # noqa: N802
    pattern: re.Pattern[str] | None = None,
    *,
    max_len: int = 200,
    min_len: int = 1,
    text: str | None = None,
    choices: Iterable[str] | None = None,
) -> Validator:
    """A string. ``text="evidence"`` applies :func:`is_evidence_text`, ``text="display"``
    :func:`is_display_text`; ``pattern`` must fully match; ``choices`` restricts to a set."""

    allowed = frozenset(choices) if choices is not None else None

    def validate(value: Any, path: str) -> str:
        if not isinstance(value, str):
            raise fail(path, f"must be a string, not {_type_name(value)}")
        if not min_len <= len(value) <= max_len:
            raise fail(path, f"length must be between {min_len} and {max_len} characters")
        if text == "evidence" and not is_evidence_text(value, max_len):
            raise fail(path, "must be non-blank text without control characters (C0 or DEL)")
        if text == "display" and not is_display_text(value, max_len):
            raise fail(path, "must be trimmed printable text without markup or template syntax")
        if pattern is not None and pattern.fullmatch(value) is None:
            raise fail(path, f"does not match the required grammar: {_preview(value)}")
        if allowed is not None and value not in allowed:
            raise fail(path, f"must be one of {sorted(allowed)}, not {_preview(value)}")
        return value

    return validate


def Const(expected: Any) -> Validator:  # noqa: N802
    def validate(value: Any, path: str) -> Any:
        if type(value) is not type(expected) or value != expected:
            raise fail(path, f"must be {expected!r}")
        return value

    return validate


def Int(minimum: int, maximum: int) -> Validator:  # noqa: N802
    def validate(value: Any, path: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise fail(path, f"must be an integer, not {_type_name(value)}")
        if not minimum <= value <= maximum:
            raise fail(path, f"must be between {minimum} and {maximum}")
        return value

    return validate


def Num(minimum: float, maximum: float) -> Validator:  # noqa: N802
    def validate(value: Any, path: str) -> float | int:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise fail(path, f"must be a number, not {_type_name(value)}")
        if isinstance(value, float) and not math.isfinite(value):
            raise fail(path, "must be finite")
        if not minimum <= value <= maximum:
            raise fail(path, f"must be between {minimum} and {maximum}")
        return value

    return validate


def Bool() -> Validator:  # noqa: N802
    def validate(value: Any, path: str) -> bool:
        if not isinstance(value, bool):
            raise fail(path, f"must be a boolean, not {_type_name(value)}")
        return value

    return validate


def Null() -> Validator:  # noqa: N802
    def validate(value: Any, path: str) -> None:
        if value is not None:
            raise fail(path, "must be null")
        return None

    return validate


def Nullable(inner: Validator) -> Validator:  # noqa: N802
    def validate(value: Any, path: str) -> Any:
        return None if value is None else inner(value, path)

    return validate


def OneOf(*alternatives: Validator) -> Validator:  # noqa: N802
    """The first alternative that accepts the value wins; the last error is reported otherwise."""

    def validate(value: Any, path: str) -> Any:
        error = fail(path, "matches no alternative")
        for alternative in alternatives:
            try:
                return alternative(value, path)
            except DocumentError as exc:
                error = exc
        raise error

    return validate


def List(  # noqa: N802
    item: Validator,
    *,
    min_items: int = 0,
    max_items: int,
    unique: bool = False,
    unique_by: Callable[[Any], Any] | None = None,
    sorted_values: bool = False,
) -> Validator:
    """An array of ``item``. ``unique`` rejects equal items; ``unique_by`` rejects equal projections;
    ``sorted_values`` requires strictly ascending items (implies uniqueness)."""

    def validate(value: Any, path: str) -> list[Any]:
        if not isinstance(value, list):
            raise fail(path, f"must be an array, not {_type_name(value)}")
        if not min_items <= len(value) <= max_items:
            raise fail(path, f"must hold between {min_items} and {max_items} items")
        for index, element in enumerate(value):
            item(element, f"{path}[{index}]")
        if unique or unique_by is not None:
            seen: set[Any] = set()
            for index, element in enumerate(value):
                marker = unique_by(element) if unique_by is not None else _freeze(element)
                if marker in seen:
                    raise fail(f"{path}[{index}]", "duplicates an earlier item")
                seen.add(marker)
        if sorted_values:
            for index in range(1, len(value)):
                if not value[index - 1] < value[index]:
                    raise fail(f"{path}[{index}]", "items must be strictly ascending")
        return value

    return validate


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(sorted((key, _freeze(item)) for key, item in value.items()))
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return (type(value).__name__, value)


def Map(  # noqa: N802
    key_pattern: re.Pattern[str],
    item: Validator,
    *,
    max_items: int,
    min_items: int = 0,
    max_key_len: int = 80,
) -> Validator:
    """An object with arbitrary keys matching ``key_pattern`` and values validated by ``item``."""

    def validate(value: Any, path: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise fail(path, f"must be an object, not {_type_name(value)}")
        if not min_items <= len(value) <= max_items:
            raise fail(path, f"must hold between {min_items} and {max_items} entries")
        for key, element in value.items():
            if not isinstance(key, str) or len(key) > max_key_len or key_pattern.fullmatch(key) is None:
                raise fail(path, f"has an invalid key {_preview(key)}")
            item(element, f"{path}.{key}")
        return value

    return validate


def Obj(required: Mapping[str, Validator], optional: Mapping[str, Validator] | None = None) -> Validator:  # noqa: N802
    """An object with exactly the ``required`` keys plus any subset of ``optional`` keys."""

    optional = dict(optional or {})
    overlap = set(required) & set(optional)
    if overlap:
        raise ValueError(f"keys cannot be both required and optional: {sorted(overlap)}")

    def validate(value: Any, path: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise fail(path, f"must be an object, not {_type_name(value)}")
        if not all(isinstance(key, str) for key in value):
            raise fail(path, "has a non-string key")
        keys = set(value)
        missing = sorted(set(required) - keys)
        unknown = sorted(keys - set(required) - set(optional))
        if missing:
            raise fail(path, f"is missing required keys {missing}")
        if unknown:
            raise fail(path, f"has unknown keys {[_preview(key) for key in unknown[:8]]}")
        for key, validator in required.items():
            validator(value[key], f"{path}.{key}")
        for key, validator in optional.items():
            if key in value:
                validator(value[key], f"{path}.{key}")
        return value

    return validate


def Size(minimum: int, maximum: int) -> Validator:  # noqa: N802
    """A ``[width, height]`` pair of integers."""

    item = Int(minimum, maximum)

    def validate(value: Any, path: str) -> list[int]:
        if not isinstance(value, list) or len(value) != 2:
            raise fail(path, "must be a [width, height] pair")
        item(value[0], f"{path}[0]")
        item(value[1], f"{path}[1]")
        return value

    return validate


def Region() -> Validator:  # noqa: N802
    """A normalized ``[left, top, right, bottom]`` box with ``0<=l<r<=1`` and ``0<=t<b<=1``."""

    item = Num(0.0, 1.0)

    def validate(value: Any, path: str) -> list[float]:
        if not isinstance(value, list) or len(value) != 4:
            raise fail(path, "must be a [left, top, right, bottom] box")
        for index, coordinate in enumerate(value):
            item(coordinate, f"{path}[{index}]")
        left, top, right, bottom = value
        if not (left < right and top < bottom):
            raise fail(path, "must satisfy left < right and top < bottom")
        return value

    return validate


def check(condition: bool, path: str, message: str) -> None:
    """Raise :class:`DocumentError` at ``path`` unless ``condition`` holds."""

    if not condition:
        raise fail(path, message)
