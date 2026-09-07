"""HTTP CONNECT parsing and responses.

Only CONNECT is implemented, deliberately. A forwarding proxy that also handled
plain ``GET http://...`` would have to parse and rewrite requests, which means an
HTTP parser exposed to untrusted input for no benefit: everything a sandbox needs
(warehouse drivers, ``git``, HTTPS) speaks CONNECT, and plain HTTP to an external
host is not something to encourage anyway.

Refusals carry a body explaining WHY. The message names only the tenant's own
destination and a stable reason code, never anything about the platform's
internals — a denied sandbox is untrusted, and "which internal range did I hit"
is a useful answer to give an attacker.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from datacode_common.egress import normalize_host

# Longest plausible request line. Anything beyond this is not a CONNECT request.
MAX_REQUEST_LINE = 8 * 1024


class ProtocolError(ValueError):
    """The client is not speaking HTTP CONNECT."""


@dataclass(frozen=True)
class ConnectRequest:
    """A parsed CONNECT request.

    Attributes:
        host: Target host, normalized (lower-cased, de-bracketed).
        port: Target port.
        task_token_header: Value of the dedicated task-token header, if present.
        proxy_authorization: Value of ``Proxy-Authorization``, if present.
    """

    host: str
    port: int
    task_token_header: str = ""
    proxy_authorization: str = ""


def parse_connect(raw: bytes) -> ConnectRequest:
    """Parse a CONNECT request head.

    Args:
        raw: Everything up to and including the blank line.

    Raises:
        ProtocolError: Not a CONNECT request, or its target is unusable.
    """
    try:
        text = raw.decode("latin-1")
    except (UnicodeDecodeError, AttributeError) as exc:
        raise ProtocolError(f"request head is not decodable: {exc}") from exc

    lines = text.split("\r\n")
    request_line = lines[0].strip() if lines else ""
    if not request_line:
        raise ProtocolError("empty request line")
    if len(request_line) > MAX_REQUEST_LINE:
        raise ProtocolError("request line is too long")

    parts = request_line.split()
    if len(parts) < 2:
        raise ProtocolError(f"malformed request line: {request_line[:80]!r}")
    method, target = parts[0].upper(), parts[1]
    if method != "CONNECT":
        raise ProtocolError(
            f"only CONNECT is supported, got {method}: this proxy tunnels TLS and "
            "does not forward plain HTTP"
        )

    host, port = _split_authority(target)

    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line.strip():
            continue
        name, separator, value = line.partition(":")
        if not separator:
            continue
        headers[name.strip().lower()] = value.strip()

    return ConnectRequest(
        host=host,
        port=port,
        task_token_header=headers.get("x-datacode-task-token", ""),
        proxy_authorization=headers.get("proxy-authorization", ""),
    )


def _split_authority(target: str) -> tuple[str, int]:
    """Split ``host:port``, handling bracketed IPv6 literals."""
    candidate = target.strip()
    if candidate.startswith("["):
        closing = candidate.find("]")
        if closing == -1:
            raise ProtocolError(f"unterminated IPv6 literal in {target!r}")
        host = candidate[: closing + 1]
        remainder = candidate[closing + 1 :]
        if not remainder.startswith(":"):
            raise ProtocolError(f"CONNECT target {target!r} has no port")
        raw_port = remainder[1:]
    else:
        host, separator, raw_port = candidate.rpartition(":")
        if not separator:
            raise ProtocolError(f"CONNECT target {target!r} has no port")

    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ProtocolError(f"CONNECT target {target!r} has a non-numeric port") from exc

    normalized = normalize_host(host)
    if not normalized:
        raise ProtocolError(f"CONNECT target {target!r} has no host")
    return normalized, port


def established() -> bytes:
    """The response that opens the tunnel."""
    return b"HTTP/1.1 200 Connection Established\r\n\r\n"


def refusal(
    status: int,
    code: str,
    message: str,
    *,
    extra_headers: Optional[dict] = None,
    extra_body: Optional[dict] = None,
) -> bytes:
    """Build a refusal response.

    The body is JSON so a client library surfaces something readable rather than a
    bare TLS handshake failure, which is what a silently closed connection looks
    like from inside SQLMesh.

    ``extra_body`` carries the contract's structured payload (design 08 §7):
    an ``EGRESS_DENIED`` refusal names its ``destination`` so the caller can
    show WHICH host was refused — "your egress was denied" without the host is
    an unsolvable riddle for the user who declared that host.
    """
    reason = {
        400: "Bad Request",
        403: "Forbidden",
        407: "Proxy Authentication Required",
        429: "Too Many Requests",
        502: "Bad Gateway",
        503: "Service Unavailable",
        504: "Gateway Timeout",
    }.get(status, "Error")

    import json

    payload = {"code": code, "message": message}
    for key, value in (extra_body or {}).items():
        if value not in (None, ""):
            payload[key] = value
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    head = [
        f"HTTP/1.1 {status} {reason}",
        "Content-Type: application/json",
        f"Content-Length: {len(body)}",
        "Connection: close",
    ]
    for name, value in (extra_headers or {}).items():
        head.append(f"{name}: {value}")
    return ("\r\n".join(head) + "\r\n\r\n").encode("ascii") + body


__all__ = [
    "MAX_REQUEST_LINE",
    "ConnectRequest",
    "ProtocolError",
    "established",
    "parse_connect",
    "refusal",
]
