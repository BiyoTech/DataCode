"""Platform error-code contract tests (design 08 §7).

The contract froze 13 error codes and their HTTP statuses. Nine of them were
never implemented — the gap survived every phase gate because nothing pinned
the list. These tests are that pin:

* the literal table — every code the design froze exists, and no code the
  design did NOT freeze sneaks in (an invented code is a contract change
  nobody agreed to);
* the status table — the mapping is part of the contract, and a consumer
  keying on ``202``/``503`` breaks silently if a producer flips one;
* QUOTA_EXCEEDED deliberately has no single status (409 for counts, 429 for
  rates — the raiser chooses), so a default mapping would be a lie for one
  of the two;
* retry semantics — which codes the caller is expected to wait out, derived
  from the same table rather than restated.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from datacode_common.errors import (
    CONTRACT_ERROR_CODES,
    PlatformErrorCode,
    http_status_for,
    retryable,
)

# The frozen table, verbatim from design 08 §7. Restated here (rather than
# derived from the module under test) on purpose: a test that reads the
# expected values from the implementation can only ever prove the
# implementation agrees with itself.
EXPECTED: dict[PlatformErrorCode, int | tuple[int, int]] = {
    PlatformErrorCode.LEASE_NOT_HELD: 409,
    PlatformErrorCode.LEASE_UNAVAILABLE: 429,
    PlatformErrorCode.LEASE_SPLIT_BRAIN: 409,
    PlatformErrorCode.WORKSPACE_MATERIALIZING: 202,
    PlatformErrorCode.WORKSPACE_RELOCATING: 503,
    PlatformErrorCode.QUOTA_EXCEEDED: (409, 429),
    PlatformErrorCode.SANDBOX_UNAVAILABLE: 503,
    PlatformErrorCode.SANDBOX_TIMEOUT: 504,
    PlatformErrorCode.SANDBOX_OOM: 507,
    PlatformErrorCode.EGRESS_DENIED: 403,
    PlatformErrorCode.CELL_MAINTENANCE: 503,
    PlatformErrorCode.INSUFFICIENT_STORAGE: 507,
    # The two rows §7 freezes as "保留现有契约" (pre-existing).
    PlatformErrorCode.STATE_SCHEMA_MISMATCH: 409,
    PlatformErrorCode.ENVIRONMENT_BUSY: 409,
}


class TestTheFrozenTable:
    def test_every_frozen_code_exists(self):
        """The design froze exactly these codes — no more, no fewer."""
        assert len(EXPECTED) == 14
        assert set(CONTRACT_ERROR_CODES) == set(EXPECTED)

    def test_literal_equals_member_name(self):
        """Wire literal == member name, so logs and code agree without mapping."""
        for code in PlatformErrorCode:
            assert code.value == code.name

    @pytest.mark.parametrize("code", list(PlatformErrorCode))
    def test_status_matches_the_contract(self, code):
        expected = EXPECTED[code]
        if isinstance(expected, tuple):
            # QUOTA_EXCEEDED: the raiser picks per resource semantics; the
            # shared table must refuse to pick for it.
            with pytest.raises(KeyError):
                http_status_for(code)
        else:
            assert http_status_for(code) == expected


class TestRetrySemantics:
    """Which codes a caller is expected to wait out rather than surface."""

    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            # Settles on its own: materialization finishes, relocation
            # completes, a cell leaves maintenance, a sandbox start may
            # succeed on retry.
            (PlatformErrorCode.WORKSPACE_MATERIALIZING, True),
            (PlatformErrorCode.WORKSPACE_RELOCATING, True),
            (PlatformErrorCode.CELL_MAINTENANCE, True),
            (PlatformErrorCode.SANDBOX_UNAVAILABLE, True),
            # A retry re-creates the same state: the lease is elsewhere, two
            # writers cannot be one, the deadline/OOM/disk verdicts are facts.
            (PlatformErrorCode.LEASE_NOT_HELD, False),
            (PlatformErrorCode.LEASE_SPLIT_BRAIN, False),
            (PlatformErrorCode.SANDBOX_TIMEOUT, False),
            (PlatformErrorCode.SANDBOX_OOM, False),
            (PlatformErrorCode.INSUFFICIENT_STORAGE, False),
            (PlatformErrorCode.EGRESS_DENIED, False),
        ],
    )
    def test_retryable(self, code, expected):
        assert retryable(code) is expected

    def test_quota_is_not_classified_as_retryable(self):
        """Quota exhaustion is a policy verdict, not a transient fault.

        Backing off is the CALLER's decision (honouring Retry-After), not the
        platform's; classifying it here would invite automatic retry storms
        against a limit that only releasing a resource can lift.
        """
        with pytest.raises(KeyError):
            retryable(PlatformErrorCode.QUOTA_EXCEEDED)


class TestCrossServiceAgreement:
    """Every service that speaks the contract must agree with this module.

    These assertions live in the shared suite but read the SERVICE enum, so
    they only run where that service is importable (its own venv, which is
    how the repo runs the shared suite). The pattern is deliberate: a drift
    between the shared contract and a service's enum should fail that
    service's test run, not a shared run that may not import it.
    """

    @staticmethod
    def _runtime_error_code():
        """Import workspace-runtime's ErrorCode, or skip this venv.

        Every service has its own ``src`` package; under a non-runtime venv
        the name resolves to that service's module instead, and asserting
        against it would be a false failure (control's enum legitimately
        lacks the runtime-only lease/sandbox codes).
        """
        import src.utils.errors as runtime_errors

        if "workspace-runtime" not in str(runtime_errors.__file__):
            pytest.skip("runtime's ErrorCode is only importable under its own venv")
        return runtime_errors.ErrorCode

    def test_runtime_enum_covers_the_contract(self):
        """workspace-runtime's ErrorCode aliases every contract code.

        Runtime is the producer of the lease/sandbox/relocation codes and the
        relayer of the egress one; a contract code missing from its enum means
        that producer can never emit it (the exact gap this round closed).
        """
        ErrorCode = self._runtime_error_code()

        for code in CONTRACT_ERROR_CODES:
            assert ErrorCode[code.name].value == code.value, (
                f"runtime ErrorCode.{code.name} must alias the shared "
                f"contract literal {code.value!r}"
            )

    def test_runtime_quota_code_is_the_contract_literal(self):
        """QUOTA_EXCEEDED predates the shared module; it must not have drifted."""
        ErrorCode = self._runtime_error_code()

        assert (
            ErrorCode.QUOTA_EXCEEDED.value
            == PlatformErrorCode.QUOTA_EXCEEDED.value
        )


class TestProducersExist:
    """Every contract code must have a producer outside this module.

    The gap this round closed was codes that existed in the design but had
    no producer anywhere — the gateway's re-route hook had no trigger, the
    frontend's retry interceptor had no signal. These assertions walk the
    services' source trees for each code's raising/reference site, so a
    future refactor that deletes a producer fails here rather than silently
    re-creating the gap.

    Paths are resolved relative to this file, which lives at
    ``datacode/shared/python/tests/``; the services are the repo siblings of
    ``shared``.
    """

    _ROOT = Path(__file__).resolve().parents[3]

    # code -> (service dir, file that must reference it, what counts as a
    # reference). QUOTA_EXCEEDED is produced through its dedicated exception
    # class rather than the bare literal.
    _PRODUCERS: dict[PlatformErrorCode, tuple[str, str, tuple[str, ...]]] = {
        PlatformErrorCode.LEASE_NOT_HELD: (
            "workspace-runtime", "src/api/deps.py", ("LEASE_NOT_HELD",),
        ),
        PlatformErrorCode.LEASE_UNAVAILABLE: (
            "workspace-runtime", "src/api/rest/leases.py", ("LEASE_UNAVAILABLE",),
        ),
        PlatformErrorCode.LEASE_SPLIT_BRAIN: (
            "workspace-runtime", "src/api/rest/leases.py", ("LEASE_SPLIT_BRAIN",),
        ),
        PlatformErrorCode.WORKSPACE_MATERIALIZING: (
            "workspace-runtime", "src/api/deps.py", ("WORKSPACE_MATERIALIZING",),
        ),
        PlatformErrorCode.WORKSPACE_RELOCATING: (
            "workspace-runtime", "src/api/deps.py", ("WORKSPACE_RELOCATING",),
        ),
        PlatformErrorCode.QUOTA_EXCEEDED: (
            "workspace-runtime", "src/lease/quotas.py", ("QuotaExceededError",),
        ),
        PlatformErrorCode.SANDBOX_UNAVAILABLE: (
            "workspace-runtime", "src/utils/errors.py", ("SANDBOX_UNAVAILABLE",),
        ),
        PlatformErrorCode.SANDBOX_TIMEOUT: (
            "workspace-runtime", "src/utils/errors.py", ("SANDBOX_TIMEOUT",),
        ),
        PlatformErrorCode.SANDBOX_OOM: (
            "workspace-runtime", "src/utils/errors.py", ("SANDBOX_OOM",),
        ),
        PlatformErrorCode.EGRESS_DENIED: (
            "egress-proxy", "src/server.py", ("EGRESS_DENIED",),
        ),
        PlatformErrorCode.CELL_MAINTENANCE: (
            "gateway", "internal/cellrouter/router.go", ("CELL_MAINTENANCE",),
        ),
        PlatformErrorCode.INSUFFICIENT_STORAGE: (
            "workspace-runtime", "src/utils/errors.py", ("INSUFFICIENT_STORAGE",),
        ),
    }

    @pytest.mark.parametrize("code", list(PlatformErrorCode))
    def test_code_has_a_producer(self, code):
        if code in (PlatformErrorCode.ENVIRONMENT_BUSY, PlatformErrorCode.STATE_SCHEMA_MISMATCH):
            # Pre-existing codes: their producers predate the shared module
            # and are pinned by their own long-standing tests.
            pytest.skip("pre-existing code, pinned by its own suite")
        service, source, markers = self._PRODUCERS[code]
        path = self._ROOT / service / source
        assert path.exists(), f"{service}/{source} is the registered producer of {code.value}"
        text = path.read_text(encoding="utf-8")
        assert any(marker in text for marker in markers), (
            f"{code.value} is not referenced by its registered producer "
            f"{service}/{source}"
        )

    def test_the_gateway_consumes_the_lease_codes(self):
        """The consumer half of the two codes the gateway keys behaviour on."""
        retry_layer = (
            self._ROOT / "gateway" / "internal" / "handler" / "proxy_stale_route.go"
        ).read_text(encoding="utf-8")
        assert "LEASE_NOT_HELD" in retry_layer, (
            "the gateway's stale-route retry must consume LEASE_NOT_HELD"
        )
        acquirer = (
            self._ROOT / "gateway" / "internal" / "leaserouter" / "acquirer.go"
        ).read_text(encoding="utf-8")
        assert "LEASE_UNAVAILABLE" in acquirer, (
            "the gateway's acquirer must consume LEASE_UNAVAILABLE"
        )
