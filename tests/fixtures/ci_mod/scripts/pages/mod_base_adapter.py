"""Synthetic mod: the Pages adapter slot.

This fixture exercises the Build and packaged-runtime pipeline only. Every ``ci`` command still
loads ``site/mod-base.json``, whose repository facts require an adapter module at this path; it
defines no hook, so every Pages operation that needs one fails closed.
"""

ADAPTER_API = 1
