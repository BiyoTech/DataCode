"""Isolation mechanics for this runtime (design 04, design 06 P3e/P6).

* :mod:`src.isolation.hardening` — process-level primitives (clean environment,
  resource limits, uid separation), shared by the persistent executor and the
  local task backend.
* :mod:`src.isolation.policy` — the runtime's view of
  ``conf/isolation-profiles.yaml``: which profiles this process can actually
  execute, and therefore where a given task kind lands here.
"""
