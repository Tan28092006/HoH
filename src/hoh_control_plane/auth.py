"""Credential resolution for an MCP endpoint that listens on a network.

Over stdio the operating system is the authentication: the operator owns the
process, so the environment it sets is trustworthy. On a socket nothing about
the caller is known until it proves who it is, so role and project scope must
come from a presented credential rather than from this process's environment.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass

from hoh_control_plane.domain import Actor, AuthorizationError, Role, ValidationError


TOKENS_VARIABLE = "HOH_MCP_TOKENS_JSON"
TRANSPORT_VARIABLE = "HOH_MCP_TRANSPORT"
STDIO_TRANSPORT = "stdio"
NETWORK_TRANSPORTS = ("streamable-http", "sse")

# Long enough that guessing is not a strategy. Short tokens are the failure that
# makes every other control here decorative.
_MINIMUM_TOKEN_CHARS = 32


#: A credential map key with this prefix holds sha256(token) as hex, not the
#: token itself (wave 8A, R4). A generated token never contains ':'.
HASHED_TOKEN_PREFIX = "sha256:"
_HEX_DIGEST_CHARS = 64


def token_digest(token: str) -> str:
    """The stored form of a token: ``sha256:<hex>``."""
    return HASHED_TOKEN_PREFIX + hashlib.sha256(token.encode("ascii")).hexdigest()


def _is_token_digest(key: str) -> bool:
    digest = key[len(HASHED_TOKEN_PREFIX):]
    return (key.startswith(HASHED_TOKEN_PREFIX) and len(digest) == _HEX_DIGEST_CHARS
            and all(char in "0123456789abcdef" for char in digest))


@dataclass(frozen=True)
class AgentCredential:
    """One bearer token, or its sha256 digest, and the identity it authenticates as."""

    token: str
    actor: Actor

    @property
    def hashed(self) -> bool:
        return _is_token_digest(self.token)

    @property
    def digest(self) -> str:
        """The digest form, whichever form is stored; comparable across both."""
        return self.token if self.hashed else token_digest(self.token)


def credentials_from_environment(raw: str | None = None) -> tuple[AgentCredential, ...]:
    """Read the configured agent credentials.

    The mapping is token to identity, so the token is the secret and the identity
    is server-side data the caller never supplies.
    """
    source = raw if raw is not None else os.getenv(TOKENS_VARIABLE)
    if not source:
        return ()
    try:
        parsed = json.loads(source)
    except json.JSONDecodeError as error:
        raise ValidationError(f"{TOKENS_VARIABLE} is not valid JSON") from error
    if not isinstance(parsed, dict) or not parsed:
        raise ValidationError(f"{TOKENS_VARIABLE} must be a non-empty token map")

    credentials: list[AgentCredential] = []
    for token, value in parsed.items():
        if (not isinstance(token, str) or len(token) < _MINIMUM_TOKEN_CHARS
                or not token.isascii() or any(char.isspace() for char in token)):
            raise ValidationError(
                f"{TOKENS_VARIABLE} tokens must be at least "
                f"{_MINIMUM_TOKEN_CHARS} ASCII characters without whitespace"
            )
        if not isinstance(value, dict):
            raise ValidationError(f"{TOKENS_VARIABLE} entries must be objects")
        agent_id = value.get("agent_id")
        role_name = value.get("role")
        project_ids = value.get("project_ids") or []
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise ValidationError(f"{TOKENS_VARIABLE} entries require an agent_id")
        try:
            role = Role(role_name)
        except ValueError as error:
            raise ValidationError(f"Unknown role in {TOKENS_VARIABLE}: {role_name}") from error
        if not isinstance(project_ids, list) or not all(
            isinstance(item, str) for item in project_ids
        ):
            raise ValidationError(f"{TOKENS_VARIABLE} project_ids must be a list of strings")
        credentials.append(
            AgentCredential(token, Actor(agent_id, role, frozenset(project_ids)))
        )
    return tuple(credentials)


def resolve_actor(
    credentials: tuple[AgentCredential, ...], authorization: str | None
) -> Actor:
    """Authenticate a presented Authorization header, or refuse.

    Every configured credential is compared even after a match, so the work done
    does not depend on which token was presented or on how many are configured.
    """
    prefix = "Bearer "
    if not authorization or not authorization.startswith(prefix):
        raise AuthorizationError("A bearer credential is required")
    presented = authorization[len(prefix) :]
    if not presented.isascii():
        raise AuthorizationError("Unknown credential")
    presented_digest = token_digest(presented)
    matched: Actor | None = None
    for credential in credentials:
        # A stored digest is compared with the presented token's digest; a
        # plaintext entry (static map, or a file written before 8A) as before.
        expected = presented_digest if credential.hashed else presented
        if hmac.compare_digest(credential.token, expected) and matched is None:
            matched = credential.actor
    if matched is None:
        raise AuthorizationError("Unknown credential")
    return matched


def requested_transport(raw: str | None = None) -> str:
    """Which transport this process should serve; stdio unless told otherwise.

    Defaulting to stdio keeps an existing local install unchanged, and makes
    listening on a network an explicit decision rather than something a
    misconfiguration can turn on.
    """
    configured = raw if raw is not None else os.getenv(TRANSPORT_VARIABLE)
    # Strip before defaulting: a variable set to whitespace is unset in intent,
    # and should land on stdio rather than on an "unknown transport" error.
    transport = (configured or "").strip().lower() or STDIO_TRANSPORT
    if transport not in (STDIO_TRANSPORT, *NETWORK_TRANSPORTS):
        raise ValidationError(
            f"Unknown {TRANSPORT_VARIABLE}: {transport}. "
            f"Expected one of {STDIO_TRANSPORT}, {', '.join(NETWORK_TRANSPORTS)}"
        )
    return transport
