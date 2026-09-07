"""Lease ownership CAS tests — the Lua scripts, executed as Lua.

Why this file exists separately from ``test_lease_contract.py``
--------------------------------------------------------------
The runtime's own tests drive a fake Redis whose ``eval`` implements the
scripts' INTENT (decode the payload, compare ``token``). That is the right shape
for testing the holder's logic, but it cannot catch a bug IN the script text —
and one such bug shipped: the scripts compared ``GET`` (the whole JSON payload)
against the bare token, so against real Redis every renewal and every release
silently returned 0. Leases would self-evict one renew interval after being
taken, and released leases would linger for a full TTL.

So these tests interpret the actual Lua source. The interpreter below is
deliberately minimal and mechanical: it implements only the handful of
constructs the two scripts use, and it does NOT know what a lease is. Anything
it cannot parse raises, so a future script that grows a new construct fails
loudly here instead of being silently approved.

What is pinned:

* the payload's ``token`` FIELD is what authorizes a mutation — not the whole
  value (the shipped bug), and not a substring of it (a token appearing inside
  another field must not authorize anything);
* a successor's claim survives a late writer's release and renew;
* a corrupt or absent value is a no-op rather than an error.
"""

from __future__ import annotations

import json
import re

import pytest

from datacode_common.lease import CAS_DELETE_SCRIPT, CAS_RENEW_SCRIPT


class LuaScriptError(AssertionError):
    """The script used a construct this interpreter does not implement."""


class TinyRedisLua:
    """Executes the CAS scripts against an in-memory keyspace.

    Not a Lua implementation: a line-oriented reader for the exact statements
    the two scripts contain. Its value is that it evaluates the SCRIPT TEXT, so
    the comparison the script actually performs is the one under test.
    """

    def __init__(self, values: dict[str, str], expiries: dict[str, int]) -> None:
        self._values = values
        self._expiries = expiries

    def eval(self, script: str, keys: list[str], argv: list[str]) -> int:
        locals_: dict[str, object] = {}
        for raw_line in self._statements(script):
            outcome = self._exec(raw_line, keys, argv, locals_)
            if outcome is not None:
                return outcome
        raise LuaScriptError(f"script fell through without returning: {script!r}")

    # ── statement dispatch ─────────────────────────────────────────────

    @staticmethod
    def _statements(script: str) -> list[str]:
        return [line.strip() for line in script.strip().splitlines() if line.strip()]

    def _exec(
        self,
        line: str,
        keys: list[str],
        argv: list[str],
        locals_: dict[str, object],
    ) -> int | None:
        """Execute one statement; return a value only for ``return``."""
        if line == "end":
            # Blocks are handled by the `if` branch itself: reaching a bare
            # `end` means the guarded body already ran or was skipped.
            return None

        if match := re.fullmatch(r"local (\w+) = (.+)", line):
            locals_[match.group(1)] = self._eval_expr(
                match.group(2), keys, argv, locals_
            )
            return None

        if match := re.fullmatch(r"local (\w+), (\w+) = (.+)", line):
            first, second = self._eval_multi(match.group(3), keys, argv, locals_)
            locals_[match.group(1)] = first
            locals_[match.group(2)] = second
            return None

        if match := re.fullmatch(r"if (.+) then", line):
            self._pending_guard = self._eval_cond(match.group(1), keys, argv, locals_)
            return None

        if match := re.fullmatch(r"return (.+)", line):
            if getattr(self, "_pending_guard", None) is False:
                # The guard that precedes this return was false: skip the body.
                self._pending_guard = None
                return None
            self._pending_guard = None
            value = self._eval_expr(match.group(1), keys, argv, locals_)
            return int(value)

        raise LuaScriptError(f"unsupported statement: {line!r}")

    # ── expressions ────────────────────────────────────────────────────

    def _eval_expr(
        self,
        expr: str,
        keys: list[str],
        argv: list[str],
        locals_: dict[str, object],
    ) -> object:
        expr = expr.strip()
        if expr.isdigit():
            return int(expr)
        if match := re.fullmatch(r"redis\.call\('GET', (.+)\)", expr):
            key = self._resolve(match.group(1), keys, argv, locals_)
            # Redis returns the boolean false for a missing key; the scripts
            # compare against `false`, so that distinction is load-bearing.
            return self._values.get(str(key), False)
        if match := re.fullmatch(r"redis\.call\('DEL', (.+)\)", expr):
            key = str(self._resolve(match.group(1), keys, argv, locals_))
            self._expiries.pop(key, None)
            return 1 if self._values.pop(key, None) is not None else 0
        if match := re.fullmatch(r"redis\.call\('PEXPIRE', (.+), (.+)\)", expr):
            key = str(self._resolve(match.group(1), keys, argv, locals_))
            ttl = int(str(self._resolve(match.group(2), keys, argv, locals_)))
            if key not in self._values:
                return 0
            self._expiries[key] = ttl
            return 1
        return self._resolve(expr, keys, argv, locals_)

    def _eval_multi(
        self,
        expr: str,
        keys: list[str],
        argv: list[str],
        locals_: dict[str, object],
    ) -> tuple[object, object]:
        """Evaluate the one multi-value expression the scripts use."""
        if match := re.fullmatch(r"pcall\(cjson\.decode, (\w+)\)", expr.strip()):
            value = locals_.get(match.group(1))
            if not isinstance(value, str):
                return False, None
            try:
                return True, json.loads(value)
            except ValueError:
                return False, None
        raise LuaScriptError(f"unsupported multi-value expression: {expr!r}")

    def _eval_cond(
        self,
        cond: str,
        keys: list[str],
        argv: list[str],
        locals_: dict[str, object],
    ) -> bool:
        for part in cond.split(" or "):
            if self._eval_single_cond(part.strip(), keys, argv, locals_):
                return True
        return False

    def _eval_single_cond(
        self,
        cond: str,
        keys: list[str],
        argv: list[str],
        locals_: dict[str, object],
    ) -> bool:
        if match := re.fullmatch(r"not (\w+)", cond):
            return not locals_.get(match.group(1))
        if match := re.fullmatch(r"type\((\w+)\) ~= 'table'", cond):
            return not isinstance(locals_.get(match.group(1)), dict)
        if match := re.fullmatch(r"(\w+)\['(\w+)'\] ~= (.+)", cond):
            record = locals_.get(match.group(1))
            expected = self._resolve(match.group(3), keys, argv, locals_)
            actual = record.get(match.group(2)) if isinstance(record, dict) else None
            return actual != expected
        if match := re.fullmatch(r"(\w+) == false", cond):
            return locals_.get(match.group(1)) is False
        # Generic equality, so the interpreter can also execute the PREVIOUS
        # (broken) form of these scripts — `GET == ARGV[1]`. Being able to run
        # the buggy version is what makes the falsification in this file's
        # docstring an actual experiment rather than a claim.
        if match := re.fullmatch(r"(.+?) == (.+)", cond):
            left = self._eval_expr(match.group(1), keys, argv, locals_)
            right = self._eval_expr(match.group(2), keys, argv, locals_)
            return left == right
        raise LuaScriptError(f"unsupported condition: {cond!r}")

    @staticmethod
    def _resolve(
        token: str,
        keys: list[str],
        argv: list[str],
        locals_: dict[str, object],
    ) -> object:
        token = token.strip()
        if match := re.fullmatch(r"KEYS\[(\d+)\]", token):
            return keys[int(match.group(1)) - 1]
        if match := re.fullmatch(r"ARGV\[(\d+)\]", token):
            return argv[int(match.group(1)) - 1]
        if token in locals_:
            return locals_[token]
        if token.isdigit():
            return int(token)
        if token == "false":
            return False
        raise LuaScriptError(f"unresolvable token: {token!r}")


_KEY = "datacode:cell-0:lease:t1/ws-t1-shop/u1/dev"


def _payload(token: str, **extra: object) -> str:
    return json.dumps(
        {
            "token": token,
            "runtime_id": "workspace-runtime-0",
            "runtime_addr": "10.1.2.3:8002",
            "acquired_at": 1_700_000_000.0,
            **extra,
        }
    )


@pytest.fixture()
def keyspace() -> tuple[dict[str, str], dict[str, int], TinyRedisLua]:
    values: dict[str, str] = {}
    expiries: dict[str, int] = {}
    return values, expiries, TinyRedisLua(values, expiries)


class TestDeleteScript:
    def test_owner_deletes_its_own_claim(self, keyspace):
        values, _, lua = keyspace
        values[_KEY] = _payload("owner-token")

        assert lua.eval(CAS_DELETE_SCRIPT, [_KEY], ["owner-token"]) == 1
        assert _KEY not in values

    def test_whole_payload_is_not_the_token(self, keyspace):
        """The shipped bug: comparing GET against ARGV[1] never matches.

        A script that compared the whole value would only delete when the caller
        passed the entire JSON document — which no caller ever does, so every
        release became a silent no-op and every lease lingered for a full TTL.
        """
        values, _, lua = keyspace
        payload = _payload("owner-token")
        values[_KEY] = payload

        # Passing the payload itself must NOT authorize the delete: ownership is
        # the token field, and only the token field.
        assert lua.eval(CAS_DELETE_SCRIPT, [_KEY], [payload]) == 0
        assert _KEY in values

    def test_successor_claim_survives_a_late_release(self, keyspace):
        """The scenario the CAS exists for (see holder.py's module docstring)."""
        values, _, lua = keyspace
        values[_KEY] = _payload("successor-token")

        assert lua.eval(CAS_DELETE_SCRIPT, [_KEY], ["stale-token"]) == 0
        assert json.loads(values[_KEY])["token"] == "successor-token"

    def test_token_inside_another_field_does_not_authorize(self, keyspace):
        """A substring match would let an unrelated field hand over ownership."""
        values, _, lua = keyspace
        values[_KEY] = _payload("real-token", runtime_id="host-attacker-token")

        assert lua.eval(CAS_DELETE_SCRIPT, [_KEY], ["attacker-token"]) == 0
        assert _KEY in values

    def test_missing_key_is_a_no_op(self, keyspace):
        _, _, lua = keyspace
        assert lua.eval(CAS_DELETE_SCRIPT, [_KEY], ["any-token"]) == 0

    def test_corrupt_payload_is_a_no_op_not_an_error(self, keyspace):
        """A value that is not JSON must not error the script.

        An error would propagate to the holder as a failed release, which then
        logs and gives up — leaving a key nothing can ever clean up. Returning 0
        matches what ``decode_lease_record`` concludes about the same bytes.
        """
        values, _, lua = keyspace
        values[_KEY] = "not-json-at-all"

        assert lua.eval(CAS_DELETE_SCRIPT, [_KEY], ["any-token"]) == 0
        assert _KEY in values


class TestRegressionAgainstTheShippedBug:
    """Runs the PREVIOUS scripts to prove these tests would have caught them.

    A test suite that passes against both the broken and the fixed script proves
    nothing. So the broken form is kept here, executed by the same interpreter,
    and asserted to FAIL the ownership contract — which is what makes the rest of
    this file an experiment rather than a claim.
    """

    # Verbatim as they shipped: GET returns the JSON payload, ARGV[1] is the
    # bare token, so the comparison could never be true.
    BROKEN_DELETE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""

    BROKEN_RENEW = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

    def test_broken_delete_never_released_an_owned_lease(self, keyspace):
        values, _, lua = keyspace
        values[_KEY] = _payload("owner-token")

        # The owner asks to release its own lease and is refused: the released
        # key lingered until its TTL, keeping the working copy unroutable.
        assert lua.eval(self.BROKEN_DELETE, [_KEY], ["owner-token"]) == 0
        assert _KEY in values

    def test_broken_renew_evicted_healthy_holders(self, keyspace):
        values, expiries, lua = keyspace
        values[_KEY] = _payload("owner-token")

        # 0 is what the watchdog reads as "lease lost", so a healthy holder
        # self-evicted one renew interval (ttl/3 = 40s) after acquiring.
        assert lua.eval(self.BROKEN_RENEW, [_KEY], ["owner-token", "120000"]) == 0
        assert _KEY not in expiries

    def test_fixed_scripts_reverse_both_outcomes(self, keyspace):
        """The same two calls, against the current scripts, now succeed."""
        values, expiries, lua = keyspace
        values[_KEY] = _payload("owner-token")

        assert lua.eval(CAS_RENEW_SCRIPT, [_KEY], ["owner-token", "120000"]) == 1
        assert expiries[_KEY] == 120_000
        assert lua.eval(CAS_DELETE_SCRIPT, [_KEY], ["owner-token"]) == 1
        assert _KEY not in values


class TestRenewScript:
    def test_owner_extends_its_own_claim(self, keyspace):
        values, expiries, lua = keyspace
        values[_KEY] = _payload("owner-token")

        assert lua.eval(CAS_RENEW_SCRIPT, [_KEY], ["owner-token", "120000"]) == 1
        assert expiries[_KEY] == 120_000

    def test_late_renew_does_not_extend_a_successors_claim(self, keyspace):
        """The renew half of the same bug.

        With the broken comparison every renewal returned 0, which the watchdog
        reads as "lease lost" — so a healthy holder evicted itself one renew
        interval after acquiring, and the working copy bounced between replicas.
        """
        values, expiries, lua = keyspace
        values[_KEY] = _payload("successor-token")

        assert lua.eval(CAS_RENEW_SCRIPT, [_KEY], ["stale-token", "120000"]) == 0
        assert _KEY not in expiries

    def test_missing_key_is_a_no_op(self, keyspace):
        _, expiries, lua = keyspace
        assert lua.eval(CAS_RENEW_SCRIPT, [_KEY], ["owner-token", "120000"]) == 0
        assert not expiries
