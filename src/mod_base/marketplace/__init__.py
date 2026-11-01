"""Bounded HTTPS clients of the release publication: Modrinth, CurseForge and GitHub Releases.

``transport`` is the one HTTPS client (closed origins, no proxy, no redirect, bounded bodies, a
retry policy that never replays a request whose acceptance an answer may hide); ``modrinth``,
``curseforge`` and ``github_releases`` hold each service's routes, name rules and reconciliation
by content; ``fake`` serves all three in memory for the tests; ``limits`` bounds the package.
"""
