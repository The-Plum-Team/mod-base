"""The shared release pipeline (docs/RELEASE-PROTOCOL.md, ADR 0008): the protected release config
(:mod:`mod_base.release.config`), the release plan derived from one tested generation and the
sealed directory of the exact bytes it publishes (:mod:`mod_base.release.plan`), and the per-tag
publication ledger embedded in each draft GitHub release (:mod:`mod_base.release.ledger`).
"""
