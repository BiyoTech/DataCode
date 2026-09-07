"""Scoped task token tests.

The properties asserted here are the ones a sandbox credential must have: it
cannot be forged, cannot outlive its task, cannot be widened, and an unset secret
denies rather than allows.
"""

from __future__ import annotations

import pytest

from datacode_common.security.task_token import (
    PROXY_AUTH_HEADER,
    TASK_TOKEN_HEADER,
    TASK_TOKEN_SECRET_ENV,
    TaskTokenClaims,
    TaskTokenError,
    extract_task_token,
    mint_task_token,
    task_token_headers,
    verify_task_token,
)

SECRET = "unit-test-signing-secret"
OTHER_SECRET = "a-different-secret"
NOW = 1_800_000_000


def _claims(**overrides) -> TaskTokenClaims:
    base = {
        "task_id": "abc123",
        "tenant_id": "tenant-1",
        "workspace_id": "ws-1",
        "workspace_path": "/data/workspaces/tenant-1/ws-1/dev",
        "isolation_profile": "L2_container",
        "actor_user_id": "user-9",
    }
    base.update(overrides)
    return TaskTokenClaims(**base)


class TestRoundTrip:
    def test_claims_survive_signing(self):
        token = mint_task_token(_claims(), secret=SECRET, ttl_seconds=600, now=NOW)

        claims = verify_task_token(token, secret=SECRET, now=NOW + 10)

        assert claims.task_id == "abc123"
        assert claims.tenant_id == "tenant-1"
        assert claims.workspace_path == "/data/workspaces/tenant-1/ws-1/dev"
        assert claims.isolation_profile == "L2_container"
        assert claims.expires_at == NOW + 600

    def test_token_is_header_safe(self):
        token = mint_task_token(_claims(), secret=SECRET, ttl_seconds=60, now=NOW)

        assert token.count(".") == 1
        assert all(32 < ord(c) < 127 for c in token)
        assert " " not in token


class TestForgery:
    def test_wrong_secret_is_rejected(self):
        token = mint_task_token(_claims(), secret=SECRET, ttl_seconds=60, now=NOW)

        with pytest.raises(TaskTokenError, match="signature is invalid"):
            verify_task_token(token, secret=OTHER_SECRET, now=NOW)

    def test_tampering_with_the_tenant_is_rejected(self):
        """The attack this design exists to stop: widening one's own scope."""
        import base64
        import json

        token = mint_task_token(_claims(), secret=SECRET, ttl_seconds=60, now=NOW)
        payload, _, signature = token.partition(".")
        data = json.loads(base64.urlsafe_b64decode(payload + "=="))
        data["ten"] = "tenant-victim"
        forged_payload = (
            base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode())
            .decode()
            .rstrip("=")
        )

        with pytest.raises(TaskTokenError, match="signature is invalid"):
            verify_task_token(f"{forged_payload}.{signature}", secret=SECRET, now=NOW)

    @pytest.mark.parametrize(
        "token", ["", "   ", "no-separator", ".onlysig", "onlypayload.", "a.b.c"]
    )
    def test_malformed_tokens_are_rejected(self, token):
        with pytest.raises(TaskTokenError):
            verify_task_token(token, secret=SECRET, now=NOW)

    def test_signature_is_checked_before_the_payload_is_trusted(self):
        """An unsigned but well-formed payload must not be interpreted."""
        import base64
        import json

        payload = (
            base64.urlsafe_b64encode(
                json.dumps({"v": 1, "tid": "x", "ten": "victim"}).encode()
            )
            .decode()
            .rstrip("=")
        )

        with pytest.raises(TaskTokenError, match="signature is invalid"):
            verify_task_token(f"{payload}.not-a-signature", secret=SECRET, now=NOW)


class TestExpiry:
    def test_expired_token_is_rejected(self):
        token = mint_task_token(_claims(), secret=SECRET, ttl_seconds=60, now=NOW)

        with pytest.raises(TaskTokenError, match="expired"):
            verify_task_token(token, secret=SECRET, now=NOW + 3600)

    def test_small_clock_skew_is_tolerated(self):
        """A denial caused by a few seconds of drift would be a false alarm."""
        token = mint_task_token(_claims(), secret=SECRET, ttl_seconds=60, now=NOW)

        claims = verify_task_token(token, secret=SECRET, now=NOW + 70)

        assert claims.task_id == "abc123"

    def test_future_dated_token_is_rejected(self):
        token = mint_task_token(_claims(), secret=SECRET, ttl_seconds=60, now=NOW)

        with pytest.raises(TaskTokenError, match="not valid yet"):
            verify_task_token(token, secret=SECRET, now=NOW - 3600)

    def test_a_token_must_expire(self):
        """A non-expiring sandbox credential is what this design avoids."""
        with pytest.raises(TaskTokenError, match="must expire"):
            mint_task_token(_claims(), secret=SECRET, ttl_seconds=0, now=NOW)

    def test_explicit_expiry_wins_over_ttl(self):
        token = mint_task_token(
            _claims(expires_at=NOW + 30), secret=SECRET, ttl_seconds=9999, now=NOW
        )

        assert verify_task_token(token, secret=SECRET, now=NOW).expires_at == NOW + 30


class TestFailClosed:
    def test_minting_without_a_secret_fails(self, monkeypatch):
        monkeypatch.delenv(TASK_TOKEN_SECRET_ENV, raising=False)

        with pytest.raises(TaskTokenError, match="is not set"):
            mint_task_token(_claims(), ttl_seconds=60)

    def test_verifying_without_a_secret_denies(self, monkeypatch):
        """An unconfigured verifier must not authenticate anybody."""
        monkeypatch.delenv(TASK_TOKEN_SECRET_ENV, raising=False)
        token = mint_task_token(_claims(), secret=SECRET, ttl_seconds=60, now=NOW)

        with pytest.raises(TaskTokenError, match="is not set"):
            verify_task_token(token, now=NOW)

    def test_secret_is_read_from_the_environment(self, monkeypatch):
        monkeypatch.setenv(TASK_TOKEN_SECRET_ENV, SECRET)

        token = mint_task_token(_claims(), ttl_seconds=60, now=NOW)

        assert verify_task_token(token, now=NOW).tenant_id == "tenant-1"

    def test_scopeless_claims_are_refused(self):
        with pytest.raises(TaskTokenError, match="task_id and tenant_id"):
            mint_task_token(
                TaskTokenClaims(task_id="", tenant_id="t"),
                secret=SECRET,
                ttl_seconds=60,
                now=NOW,
            )


class TestHeaderExtraction:
    def test_dedicated_header_wins(self):
        assert extract_task_token(task_header="tok", proxy_authorization="Bearer other") == "tok"

    def test_bearer_proxy_authorization(self):
        assert extract_task_token(proxy_authorization="Bearer tok") == "tok"

    def test_basic_proxy_authorization_uses_the_username_half(self):
        """What `https_proxy=http://<token>@proxy:3128` actually sends."""
        import base64

        encoded = base64.b64encode(b"tok:").decode()

        assert extract_task_token(proxy_authorization=f"Basic {encoded}") == "tok"

    @pytest.mark.parametrize(
        "value", ["", "   ", "Digest abc", "Basic !!!not-base64!!!", "Bearer has space"]
    )
    def test_unusable_values_yield_nothing(self, value):
        """No credential presented, as opposed to a bad one."""
        assert extract_task_token(proxy_authorization=value) == ""

    def test_headers_helper(self):
        assert task_token_headers("tok") == {TASK_TOKEN_HEADER: "tok"}
        assert task_token_headers("") == {}


class TestAuditFields:
    def test_audit_fields_never_include_the_token(self):
        claims = _claims()

        fields = claims.audit_fields()

        assert fields["tenant_id"] == "tenant-1"
        assert fields["isolation_profile"] == "L2_container"
        assert not any("token" in key for key in fields)


class TestConstants:
    def test_task_token_does_not_reuse_the_authorization_header(self):
        """Reusing it would let a scrubber treat the two as interchangeable."""
        assert TASK_TOKEN_HEADER.lower() != "authorization"
        assert PROXY_AUTH_HEADER.lower() == "proxy-authorization"
