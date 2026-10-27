"""Egress policy — what a sandbox may connect to (design 04 §3.4).

User code has legitimate outbound needs (the customer's warehouse, the Git
server) and illegitimate ones (the platform's own Postgres, Redis, internal
APIs, the cloud metadata service). This module is the single decision point for
that difference, so the rule set cannot drift between the proxy that enforces it
and the tests that assert it.

Two properties carry the security of the whole scheme:

1.  **Deny by default.** A destination is reachable only if a tenant explicitly
    declared it. An empty allow-list means "nothing", never "everything": the
    common failure of allow-list systems is a missing config that silently opens
    the gate, and a tenant with no declared destinations legitimately needs none.

2.  **The decision is made on RESOLVED addresses.** Checking a hostname against
    CIDR blocks is meaningless — ``warehouse.evil.example`` can resolve to
    ``10.0.0.5``. Every address a name resolves to must pass, and a name that
    resolves to several addresses is allowed only if ALL of them pass. This is
    what closes DNS rebinding (T3/T6/T7) rather than merely documenting it.

The denied ranges are a floor, not a ceiling: a profile may add to them, never
remove. ``169.254.169.254`` (cloud metadata, T6) and the private ranges (T3/T7)
are baked in because forgetting one of them is a full platform compromise, and
a deployment has no legitimate reason to reach them from user code.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

# Networks user code may never reach, regardless of configuration.
#
# The private ranges and loopback cover the platform's own data plane (T3) and
# the "connect back to my own node" case; link-local covers the cloud metadata
# service at 169.254.169.254 (T6), which hands out node credentials to anything
# that can issue a plain HTTP GET. The 9/11/21/30 blocks are additionally
# treated as internal per the platform's own network policy.
BASELINE_DENIED_CIDRS: tuple[str, ...] = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",
    "127.0.0.0/8",
    "0.0.0.0/8",
    "100.64.0.0/10",  # carrier-grade NAT, used by some clusters as pod space
    "9.0.0.0/8",
    "11.0.0.0/8",
    "21.0.0.0/8",
    "30.0.0.0/8",
    # IPv6 equivalents. Omitting them would leave the exact same holes open to
    # any resolver that returns an AAAA record first.
    "::1/128",
    "fc00::/7",  # unique local
    "fe80::/10",  # link-local
    "::ffff:0:0/96",  # IPv4-mapped: would otherwise bypass the v4 rules
)

# Ports a tunnel may target. A CONNECT proxy that forwards to any port is a
# generic TCP relay, which would let user code reach a database or an SSH server
# on an allow-listed host it was only meant to fetch HTTPS from.
DEFAULT_ALLOWED_PORTS: tuple[int, ...] = (443, 80)

# Ports the warehouse destinations commonly listen on. Only reachable on hosts a
# tenant declared as a warehouse, never on the generic allow-list.
WAREHOUSE_PORTS: tuple[int, ...] = (
    443,   # Snowflake / BigQuery / Databricks REST
    5432,  # PostgreSQL
    3306,  # MySQL
    1433,  # SQL Server
    9000,  # ClickHouse native
    8443,  # Hive / Trino over TLS
)

# Reasons a request was refused. Stable strings: they are audited, counted and
# asserted on, so they must survive refactors.
DENY_UNKNOWN_HOST = "host_not_allowed"
DENY_INTERNAL_ADDRESS = "internal_address"
DENY_UNRESOLVABLE = "host_unresolvable"
DENY_PORT = "port_not_allowed"
DENY_MALFORMED = "malformed_target"
DENY_QUOTA = "quota_exceeded"

ALLOW_REASON = "allowed"

# Resolver signature: hostname -> list of IP address strings. Injectable so the
# proxy can share a cache and tests need no network.
ResolverFn = Callable[[str], Sequence[str]]


class EgressConfigError(ValueError):
    """The egress configuration cannot be used as written."""


@dataclass(frozen=True)
class EgressDecision:
    """The outcome of one egress check.

    Attributes:
        allowed: Whether the connection may proceed.
        reason: Stable code — :data:`ALLOW_REASON` or one of the ``DENY_*``
            values. Audited and counted, so never free-form text.
        detail: Human-readable explanation for the log and the error body. Safe
            to show a tenant: it names only their own destination.
        host: Normalized hostname or IP literal that was evaluated.
        port: Port that was evaluated.
        resolved: Addresses the host resolved to, for the audit record. Empty
            for a decision made before resolution.
    """

    allowed: bool
    reason: str
    detail: str = ""
    host: str = ""
    port: int = 0
    resolved: tuple[str, ...] = ()

    def audit_fields(self) -> dict[str, object]:
        """Fields to attach to the structured egress audit line.

        Shaped for a log pipeline to consume later without reprocessing (the
        observability stack itself is deferred), which is why the outcome is a
        boolean plus a stable reason rather than a sentence.
        """
        return {
            "egress_allowed": self.allowed,
            "egress_reason": self.reason,
            "egress_host": self.host,
            "egress_port": self.port,
            "egress_resolved": list(self.resolved),
        }


def _default_resolver(host: str) -> Sequence[str]:
    """Resolve a hostname to every address it maps to.

    Both families are requested: a policy that only inspected A records would be
    bypassed by a name with an AAAA record pointing inside the cluster.
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise EgressResolutionError(str(exc)) from exc
    # Deduplicate while keeping order, so the audit record is stable.
    seen: dict[str, None] = {}
    for info in infos:
        seen.setdefault(str(info[4][0]), None)
    return tuple(seen)


class EgressResolutionError(RuntimeError):
    """A hostname could not be resolved. Treated as a denial, never as allowed."""


def _as_ip(value: str):
    """Parse ``value`` as an IP address, or return None when it is a name.

    The distinction matters for the ORDER of checks: an address can be judged
    immediately against the denied ranges, whereas a name must first pass the
    allow-list and then be resolved. Returning None rather than raising keeps
    "this is a hostname" an ordinary outcome, not an exception path.
    """
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _is_routable_destination(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    """Whether an address could plausibly be a real external destination.

    Asks the stdlib by CLASS instead of enumerating CIDRs, because a reserved
    range missing from a hand-written list fails OPEN. ``is_global`` alone is not
    enough: it answers False for private space (already covered by the baseline
    CIDRs) but True for some multicast addresses, so the categories that must
    never be a destination are named explicitly.
    """
    return not (
        address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or address.is_loopback
        or address.is_link_local
        or not address.is_global
    )


def normalize_host(host: str) -> str:
    """Lower-case a hostname and strip the forms that defeat naive matching.

    A trailing dot (``example.com.``) is a valid FQDN that string equality would
    miss, and bracketed IPv6 literals (``[::1]``) arrive that way from a CONNECT
    target. Both are normalized here so every comparison downstream sees one
    shape.
    """
    cleaned = (host or "").strip().lower().rstrip(".")
    if cleaned.startswith("[") and cleaned.endswith("]"):
        cleaned = cleaned[1:-1]
    return cleaned


def host_matches(host: str, pattern: str) -> bool:
    """Match a host against an allow-list entry.

    Supports an exact host and a single leading ``*.`` wildcard. Deliberately
    nothing more: substring or regex matching is how ``evil-snowflakecomputing.com``
    ends up matching a rule meant for ``*.snowflakecomputing.com``.

    A wildcard covers sub-domains only, not the bare domain — ``*.example.com``
    does not match ``example.com``, because the two are frequently operated by
    different parties.
    """
    host = normalize_host(host)
    pattern = normalize_host(pattern)
    if not host or not pattern:
        return False
    if pattern.startswith("*."):
        suffix = pattern[1:]  # ".example.com"
        return host.endswith(suffix) and len(host) > len(suffix)
    return host == pattern


@dataclass(frozen=True)
class EgressDestination:
    """One destination a tenant declared.

    Attributes:
        host: Exact host or ``*.suffix`` pattern.
        ports: Ports allowed on this host. Empty means
            :data:`DEFAULT_ALLOWED_PORTS`.
        kind: Why it is allowed (``warehouse``, ``git``, ``package_index``).
            Audit and policy grouping only — the profile's
            ``allowed_destinations`` names these kinds, so a profile can permit
            warehouse traffic while refusing package downloads.
        note: Free-form provenance, e.g. the workspace that declared it.
    """

    host: str
    ports: tuple[int, ...] = ()
    kind: str = ""
    note: str = ""

    def allows_port(self, port: int) -> bool:
        return port in (self.ports or DEFAULT_ALLOWED_PORTS)


@dataclass(frozen=True)
class EgressPolicy:
    """The effective egress rules for one sandbox.

    Built per task: the allow-list is the tenant's own declared destinations, so
    two tasks of different tenants never share one.
    """

    destinations: tuple[EgressDestination, ...] = ()
    denied_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()
    allowed_kinds: frozenset[str] = frozenset()
    resolver: ResolverFn = field(default=_default_resolver, compare=False)

    # ── construction ───────────────────────────────────────────────────

    @classmethod
    def build(
        cls,
        destinations: Iterable[EgressDestination | Mapping[str, object]] = (),
        *,
        extra_denied_cidrs: Iterable[str] = (),
        allowed_kinds: Iterable[str] = (),
        resolver: Optional[ResolverFn] = None,
    ) -> "EgressPolicy":
        """Assemble a policy.

        Args:
            destinations: The tenant's declared destinations.
            extra_denied_cidrs: Additional networks to refuse, from the isolation
                profile. ADDED to :data:`BASELINE_DENIED_CIDRS`; the baseline
                cannot be removed, because a configuration able to shrink it
                could re-open the metadata service.
            allowed_kinds: Destination kinds this profile permits. Empty means
                every kind the tenant declared.
            resolver: Name resolution function. Injected by the proxy (shared
                cache) and by tests (no network).

        Raises:
            EgressConfigError: A CIDR could not be parsed. Fatal rather than
                skipped: a typo would otherwise silently widen the policy.
        """
        parsed: list[EgressDestination] = []
        for item in destinations:
            parsed.append(
                item
                if isinstance(item, EgressDestination)
                else _destination_from_mapping(item)
            )

        networks = []
        for cidr in (*BASELINE_DENIED_CIDRS, *extra_denied_cidrs):
            try:
                networks.append(ipaddress.ip_network(str(cidr), strict=False))
            except ValueError as exc:
                raise EgressConfigError(f"invalid denied CIDR {cidr!r}: {exc}") from exc

        return cls(
            destinations=tuple(parsed),
            denied_networks=tuple(networks),
            allowed_kinds=frozenset(str(k) for k in allowed_kinds if k),
            resolver=resolver or _default_resolver,
        )

    @classmethod
    def from_profile(
        cls,
        profile_settings: Mapping[str, object],
        destinations: Iterable[EgressDestination | Mapping[str, object]] = (),
        *,
        resolver: Optional[ResolverFn] = None,
    ) -> "EgressPolicy":
        """Build from an isolation profile's ``network`` block plus a tenant's list.

        The profile states the SHAPE of the policy (deny by default, which kinds
        of destination are conceivable, which ranges are off-limits); the tenant
        states the concrete hosts. Keeping those apart is what lets one profile
        serve every tenant.
        """
        network = profile_settings.get("network") or {}
        if not isinstance(network, Mapping):
            raise EgressConfigError("profile 'network' must be a mapping")

        default = str(network.get("default", "deny")).lower()
        if default != "deny":
            # An allow-by-default profile would make every rule below decorative.
            raise EgressConfigError(
                f"network.default must be 'deny', got {default!r}: "
                "an allow-by-default sandbox has no egress policy at all"
            )

        raw_cidrs = network.get("denied_cidrs") or ()
        if isinstance(raw_cidrs, (str, bytes)):
            raise EgressConfigError("network.denied_cidrs must be a list")

        return cls.build(
            destinations,
            extra_denied_cidrs=[str(c) for c in raw_cidrs],
            allowed_kinds=[str(k) for k in (network.get("allowed_destinations") or ())],
            resolver=resolver,
        )

    # ── decision ───────────────────────────────────────────────────────

    def evaluate(self, host: str, port: int) -> EgressDecision:
        """Decide whether a sandbox may connect to ``host:port``.

        Order matters and is chosen so the cheapest refusals happen first and the
        strongest one happens last: the resolved-address check runs AFTER the
        host allow-list, so that even an allow-listed name pointing into the
        cluster is refused.
        """
        normalized = normalize_host(host)
        if not normalized or "/" in normalized or " " in normalized:
            return EgressDecision(
                False,
                DENY_MALFORMED,
                f"{host!r} is not a usable destination host",
                host=normalized,
                port=port,
            )
        if port <= 0 or port > 65535:
            return EgressDecision(
                False, DENY_MALFORMED, f"port {port} is out of range",
                host=normalized, port=port,
            )

        literal = _as_ip(normalized)
        if literal is not None:
            # An IP literal skips the name allow-list only if a destination names
            # that exact address; otherwise it is judged on the address alone.
            return self._evaluate_literal(literal, normalized, port)

        destination = self._match_destination(normalized, port)
        if destination is None:
            return self._reject_unmatched(normalized, port)

        try:
            resolved = tuple(self.resolver(normalized))
        except (EgressResolutionError, OSError) as exc:
            return EgressDecision(
                False,
                DENY_UNRESOLVABLE,
                f"could not resolve {normalized}: {exc}",
                host=normalized,
                port=port,
            )
        if not resolved:
            return EgressDecision(
                False,
                DENY_UNRESOLVABLE,
                f"{normalized} resolved to no addresses",
                host=normalized,
                port=port,
            )

        # EVERY address must pass. A name resolving to one public and one
        # internal address is the rebinding attack, not a partial success.
        for address in resolved:
            parsed = _as_ip(address)
            if parsed is None:
                return EgressDecision(
                    False,
                    DENY_UNRESOLVABLE,
                    f"{normalized} resolved to an unparsable address {address!r}",
                    host=normalized,
                    port=port,
                    resolved=resolved,
                )
            blocked = self._blocking_network(parsed)
            if blocked is not None:
                return EgressDecision(
                    False,
                    DENY_INTERNAL_ADDRESS,
                    f"{normalized} resolves to {address}, inside the blocked "
                    f"range {blocked}",
                    host=normalized,
                    port=port,
                    resolved=resolved,
                )

        return EgressDecision(
            True,
            ALLOW_REASON,
            f"{normalized}:{port} matches {destination.host}"
            + (f" ({destination.kind})" if destination.kind else ""),
            host=normalized,
            port=port,
            resolved=resolved,
        )

    def _evaluate_literal(
        self, address: ipaddress.IPv4Address | ipaddress.IPv6Address,
        normalized: str, port: int,
    ) -> EgressDecision:
        """Judge a destination given as a bare IP address."""
        blocked = self._blocking_network(address)
        if blocked is not None:
            return EgressDecision(
                False,
                DENY_INTERNAL_ADDRESS,
                f"{normalized} is inside the blocked range {blocked}",
                host=normalized,
                port=port,
                resolved=(normalized,),
            )
        destination = self._match_destination(normalized, port)
        if destination is None:
            return self._reject_unmatched(normalized, port, resolved=(normalized,))
        return EgressDecision(
            True,
            ALLOW_REASON,
            f"{normalized}:{port} matches {destination.host}",
            host=normalized,
            port=port,
            resolved=(normalized,),
        )

    def _reject_unmatched(
        self, host: str, port: int, resolved: tuple[str, ...] = ()
    ) -> EgressDecision:
        """Explain a miss precisely enough to be actionable.

        A tenant staring at "denied" needs to know whether their host is unknown
        or merely their port, so the two cases are separated.
        """
        name_matched = any(host_matches(host, d.host) for d in self._visible())
        if name_matched:
            return EgressDecision(
                False,
                DENY_PORT,
                f"{host} is allowed, but port {port} is not",
                host=host,
                port=port,
                resolved=resolved,
            )
        return EgressDecision(
            False,
            DENY_UNKNOWN_HOST,
            f"{host} is not among this tenant's declared destinations",
            host=host,
            port=port,
            resolved=resolved,
        )

    def _visible(self) -> tuple[EgressDestination, ...]:
        """Destinations this profile permits at all.

        A tenant may have declared a package index while the profile allows only
        warehouse and git; filtering here means the profile's ``allowed_destinations``
        actually constrains rather than documents.
        """
        if not self.allowed_kinds:
            return self.destinations
        return tuple(
            d for d in self.destinations if not d.kind or d.kind in self.allowed_kinds
        )

    def _match_destination(self, host: str, port: int) -> Optional[EgressDestination]:
        for destination in self._visible():
            if host_matches(host, destination.host) and destination.allows_port(port):
                return destination
        return None

    def _blocking_network(
        self, address: ipaddress.IPv4Address | ipaddress.IPv6Address
    ):
        """Return the denied network containing ``address``, or None."""
        for network in self.denied_networks:
            if address.version != network.version:
                continue
            if address in network:
                return network
        if not _is_routable_destination(address):
            # Categories no legitimate warehouse lives in. Checked by class
            # rather than by CIDR because an explicit list of reserved ranges
            # ages badly, and a range missing from it fails OPEN.
            return ipaddress.ip_network(f"{address}/{address.max_prefixlen}")
        return None


def _destination_from_mapping(data: Mapping[str, object]) -> EgressDestination:
    """Build a destination from its serialized form."""
    host = normalize_host(str(data.get("host", "")))
    if not host:
        raise EgressConfigError("egress destination requires a 'host'")
    raw_ports = data.get("ports") or ()
    if isinstance(raw_ports, (str, bytes, int)):
        raise EgressConfigError(f"destination {host}: 'ports' must be a list")
    try:
        ports = tuple(int(p) for p in raw_ports)
    except (TypeError, ValueError) as exc:
        raise EgressConfigError(f"destination {host}: invalid port: {exc}") from exc
    for port in ports:
        if port <= 0 or port > 65535:
            raise EgressConfigError(f"destination {host}: port {port} out of range")
    return EgressDestination(
        host=host,
        ports=ports,
        kind=str(data.get("kind", "")),
        note=str(data.get("note", "")),
    )


__all__ = [
    "ALLOW_REASON",
    "BASELINE_DENIED_CIDRS",
    "DENY_INTERNAL_ADDRESS",
    "DENY_MALFORMED",
    "DENY_PORT",
    "DENY_QUOTA",
    "DENY_UNKNOWN_HOST",
    "DENY_UNRESOLVABLE",
    "DEFAULT_ALLOWED_PORTS",
    "WAREHOUSE_PORTS",
    "EgressConfigError",
    "EgressDecision",
    "EgressDestination",
    "EgressPolicy",
    "EgressResolutionError",
    "ResolverFn",
    "host_matches",
    "normalize_host",
]
