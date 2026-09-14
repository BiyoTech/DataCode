"""Working-copy Python dependencies — the runtime's half of the contract.

The shared library (:mod:`datacode_common.python_deps`) owns the FORMAT: what a
declaration is, how it is fingerprinted, where the overlay lives, which packages
may never be shadowed. This package owns the ORCHESTRATION:

* :mod:`src.deps.manifest` — read and write ``.datacode/deps/manifest.json``,
  and answer the one question every consumer asks before touching
  ``PYTHONPATH``: *may this overlay be injected here?*
* :mod:`src.deps.service` — read the declaration, submit the ``deps.sync`` task,
  stream its transcript, write the manifest, classify failures, audit.

Only ``manifest`` is on the hot path: it is consulted on every code-location
start, every sandbox launch and every asset-page open, so it does file IO and
comparisons only — never an install.
"""

from src.deps.manifest import (
    DepsStatus,
    overlay_for_injection,
    read_manifest,
    read_status,
    write_manifest,
)
from src.deps.service import DepsService, DepsSyncOutcome, get_deps_service

__all__ = [
    "DepsService",
    "DepsStatus",
    "DepsSyncOutcome",
    "get_deps_service",
    "overlay_for_injection",
    "read_manifest",
    "read_status",
    "write_manifest",
]
