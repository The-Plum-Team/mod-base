"""The kit's data model: grammar and artifact names, numeric limits, strict JSON, document validators.

``model`` is the bottom layer: it imports only ``mod_base`` (versions), ``mod_base.errors`` and the
standard library, so every other unit can depend on it.

* :mod:`mod_base.model.grammar`: identifier regexes and the only artifact-name builders/parsers;
* :mod:`mod_base.model.limits`: every numeric bound;
* :mod:`mod_base.model.canonical`: strict JSON decoding, canonical encoding, bounded file reads;
* :mod:`mod_base.model.validators`: validator combinators and :class:`DocumentError`;
* :mod:`mod_base.model.documents`: one strict validator per document kind (SPEC §3).
"""
