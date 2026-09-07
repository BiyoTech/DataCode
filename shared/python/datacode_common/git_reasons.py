"""Stable reason codes for remote Git failures, shared by every layer.

A Git failure travels through four layers before a user reads it:

1. ``workspace-runtime`` classifies git/provider stderr into a reason code;
2. it raises a structured error carrying that code;
3. ``control-service`` relays the code untouched (it never re-classifies);
4. the frontend maps the code onto a localized, actionable sentence.

The code is therefore a CONTRACT literal, and a contract literal defined more
than once drifts silently: the producer keeps emitting ``repo_not_found`` while
a renamed consumer waits for something else, and the user falls back to the
generic "Git operation failed" — which is precisely the defect this module was
introduced to remove. The literals and their English fallback sentences live
here once; both Python services re-export them and a contract test asserts the
frontend's own copy agrees (no language boundary can be crossed).

The English sentences are not decoration: they are what CLI users, server logs
and any client without i18n see, so each one must be actionable on its own.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "GitRemoteReason",
    "GIT_REMOTE_REASONS",
    "REASON_MESSAGES",
    "message_for",
]


class GitRemoteReason:
    """Reason codes a rejected remote Git operation can carry.

    Plain ``str`` constants rather than a ``StrEnum`` on purpose: these values
    are used as DICT KEYS (the message table here, the frontend's i18n map) and
    ``Enum.__hash__`` hashes the member NAME, not its value — a ``StrEnum``
    member therefore misses a ``dict[str, ...]`` lookup keyed by its own value.
    """

    # No credential is configured for the remote's host at all.
    CREDENTIAL_MISSING: Final[str] = "credential_missing"
    # A credential exists but the provider rejected it (invalid/expired/revoked).
    INVALID_CREDENTIAL: Final[str] = "invalid_credential"
    # The credential authenticates but may not write to this repository.
    NO_WRITE_ACCESS: Final[str] = "no_write_access"
    # The repository does not exist, or the credential cannot see it. Providers
    # answer 404 for both cases on purpose (they do not leak existence).
    REPO_NOT_FOUND: Final[str] = "repo_not_found"
    # The push would overwrite remote commits the local branch does not have.
    NON_FAST_FORWARD: Final[str] = "non_fast_forward"
    # The requested branch exists neither locally nor on the remote.
    BRANCH_NOT_FOUND: Final[str] = "branch_not_found"
    # The Git host could not be reached (DNS, refused, timeout). Transient.
    HOST_UNREACHABLE: Final[str] = "host_unreachable"
    # Repository creation was refused: the token may not create a repository
    # under the requested owner (typically an organization it has no rights on).
    # Distinct from NO_WRITE_ACCESS, which is about an EXISTING repository — the
    # fix is different (grant org repo-creation vs grant contents:write).
    REPO_CREATION_DENIED: Final[str] = "repo_creation_denied"
    # Repository creation was attempted and failed for a non-authorization
    # reason (provider error, name already taken by a foreign object, ...).
    REPO_CREATION_FAILED: Final[str] = "repo_creation_failed"
    # The repository exists but carries no branch yet. Not always an error: the
    # workspace-creation path treats it as "reuse an empty repository".
    REMOTE_EMPTY: Final[str] = "remote_empty"
    # Classification failed. The redacted git output is then the whole
    # diagnosis, which is why every layer forwards it alongside the code.
    UNKNOWN: Final[str] = "unknown"


# Every reason code, in the order the classifier considers them (most specific
# first). Tests walk this tuple, so a code added to the class above without
# being added here — or without a message below — fails loudly.
GIT_REMOTE_REASONS: Final[tuple[str, ...]] = (
    GitRemoteReason.CREDENTIAL_MISSING,
    GitRemoteReason.INVALID_CREDENTIAL,
    GitRemoteReason.NO_WRITE_ACCESS,
    GitRemoteReason.REPO_NOT_FOUND,
    GitRemoteReason.NON_FAST_FORWARD,
    GitRemoteReason.BRANCH_NOT_FOUND,
    GitRemoteReason.HOST_UNREACHABLE,
    GitRemoteReason.REPO_CREATION_DENIED,
    GitRemoteReason.REPO_CREATION_FAILED,
    GitRemoteReason.REMOTE_EMPTY,
    GitRemoteReason.UNKNOWN,
)


# English fallback sentence per reason. ``{host}`` and ``{operation}`` are the
# only placeholders; both are always supplied by :func:`message_for`.
REASON_MESSAGES: Final[dict[str, str]] = {
    GitRemoteReason.CREDENTIAL_MISSING: (
        "No Git credential is configured for {host}. "
        "Add a credential for that host in Settings and retry."
    ),
    GitRemoteReason.INVALID_CREDENTIAL: (
        "The Git credential for {host} was rejected. "
        "The token is invalid, expired or revoked."
    ),
    GitRemoteReason.NO_WRITE_ACCESS: (
        "The Git credential for {host} has no write access to this repository. "
        "Grant the token write access to repository contents and make sure its "
        "resource owner is the account or organization that owns the repository."
    ),
    GitRemoteReason.REPO_NOT_FOUND: (
        "The repository was not found on {host}, or the credential cannot access it."
    ),
    GitRemoteReason.NON_FAST_FORWARD: (
        "The remote branch has commits your local branch does not. "
        "Pull first, then push again."
    ),
    GitRemoteReason.BRANCH_NOT_FOUND: "The requested branch does not exist on {host}.",
    GitRemoteReason.HOST_UNREACHABLE: "{host} could not be reached from the server.",
    GitRemoteReason.REPO_CREATION_DENIED: (
        "The Git credential for {host} may not create a repository under that "
        "owner. Grant the token permission to create repositories in the target "
        "organization (or point the URL at an owner it may write to) and retry."
    ),
    GitRemoteReason.REPO_CREATION_FAILED: (
        "Creating the repository on {host} failed. "
        "Check that the name is free and that the credential is still valid."
    ),
    GitRemoteReason.REMOTE_EMPTY: (
        "The repository on {host} exists but has no branches yet."
    ),
    GitRemoteReason.UNKNOWN: "Git {operation} against {host} failed.",
}


def message_for(reason: str, *, host: str = "", operation: str = "") -> str:
    """English sentence for ``reason``, falling back to the unknown template.

    ``host`` is displayed as "the Git server" when unresolved: an empty host in
    the middle of a sentence reads like a truncated message, and the reason
    itself is still worth stating.
    """
    template = REASON_MESSAGES.get(reason, REASON_MESSAGES[GitRemoteReason.UNKNOWN])
    return template.format(host=host or "the Git server", operation=operation or "operation")
