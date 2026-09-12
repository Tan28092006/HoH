"""Telling an agent that work is waiting, for every role rather than one.

The queue already decides who should act and picks the live agents to offer it
to. This is only the knock on the door: it carries no authority, changes no
state, and an agent that never receives it still finds its work by polling.

Delivery is an accelerator. Everything here is written so that losing it costs
freshness and nothing else.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4


# Loopback only until an operator has a reason to widen it. An endpoint is a
# place this service will make an outbound request to, so the allowlist is the
# whole of the protection.
ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# Agents on other machines need endpoints this list cannot contain, so an
# operator may add hosts here. Deliberately additive and deliberately explicit:
# the default survives a misconfigured variable, and a blank or absent value
# means loopback rather than "no restriction".
ALLOWED_HOSTS_VARIABLE = "HOH_WAKEUP_ALLOWED_HOSTS"

SECRET_VARIABLE = "HOH_WAKEUP_SECRET"

# A wakeup that cannot be delivered is retried this many times before the offer
# is left to polling. Bounded because an unreachable agent must not become an
# unbounded queue of outbound requests.
MAX_DELIVERY_ATTEMPTS = 3

_TIMEOUT_SECONDS = 5.0


class EndpointNotAllowed(ValueError):
    """The endpoint is not somewhere this service is permitted to call."""


class SigningSecretMissing(ValueError):
    """A delivery to a registered endpoint must be signed, and no key is set."""


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse any redirect rather than following it to an unvalidated host.

    Validating the configured URL is not enough on its own: urllib follows a
    redirect automatically, so an allowlisted loopback endpoint answering 302
    could otherwise send the wakeup to any host. A wakeup receiver has no reason
    to redirect, so the safe answer is to refuse instead of re-validating a hop.

    Raising rather than returning None matters for the operator: urllib turns a
    declined redirect into a bare "HTTP Error 302", which records a failure
    without recording that the endpoint tried to move the wakeup elsewhere.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        raise EndpointNotAllowed(
            f"A wakeup endpoint attempted a redirect to {newurl}"
        )


def allowed_hosts() -> frozenset[str]:
    """The hosts this service may make an outbound wakeup request to.

    Read on each call rather than captured at import, so an operator changing
    the variable does not need the meaning of "allowed" to depend on which
    process started first.
    """
    configured = os.getenv(ALLOWED_HOSTS_VARIABLE, "")
    extra = {host.strip().lower() for host in configured.split(",") if host.strip()}
    if "*" in extra:
        # A wildcard here would turn every registered endpoint into an outbound
        # request to anywhere, which is the one thing this list exists to stop.
        raise EndpointNotAllowed(
            f"{ALLOWED_HOSTS_VARIABLE} must name hosts; '*' is not a host"
        )
    return ALLOWED_HOSTS | frozenset(extra)


def validate_endpoint(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in allowed_hosts():
        raise EndpointNotAllowed(
            "A wakeup endpoint must be an http(s) address on the allowlist"
        )
    if host not in ALLOWED_HOSTS and parsed.scheme != "https":
        # Off the machine, plain HTTP puts the envelope on the wire in the clear.
        # It carries no credential by design, but it still names a project and a
        # ticket, and nothing about it survives an attacker rewriting it in
        # flight. Loopback keeps http because there is no wire to read.
        raise EndpointNotAllowed(
            "A wakeup endpoint outside loopback must use https"
        )
    return endpoint


def build_envelope(
    payload: dict[str, Any],
    nonce: str,
    secret: str | None,
    *,
    kind: str,
    require_signature: bool = False,
    secret_variable: str = SECRET_VARIABLE,
) -> dict[str, Any]:
    """Wrap a bounded payload so the receiver can reject replays and forgeries.

    The signature requirement lives here rather than at the call site, so a later
    caller cannot reintroduce an unsigned delivery by forgetting to ask for one.
    """
    if require_signature and not secret:
        # Names the variable the operator actually has to set for this path,
        # which is not the same one for every caller of a shared primitive.
        raise SigningSecretMissing(
            f"{secret_variable} must be set to deliver to a registered endpoint"
        )
    envelope: dict[str, Any] = {
        "kind": kind,
        "nonce": nonce,
        "issued_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "payload": payload,
    }
    if secret:
        material = json.dumps(
            {key: envelope[key] for key in ("kind", "nonce", "issued_at", "payload")},
            sort_keys=True,
            separators=(",", ":"),
        )
        envelope["signature"] = hmac.new(
            secret.encode("utf-8"), material.encode("utf-8"), hashlib.sha256
        ).hexdigest()
    return envelope


def offer_payload(offer: dict[str, Any], queue: dict[str, Any]) -> dict[str, Any]:
    """What a wakeup is allowed to say.

    Enough to go and look, and nothing more: no ticket body, no evidence, no
    lease identifier, no credential. The receiver resolves the typed reference
    through its own authenticated session, which is what keeps this envelope
    worthless to anyone who intercepts it.
    """
    return {
        "project_id": queue["project_id"],
        "target_role": queue["target_role"],
        "ticket_ref": f"ticket:{queue['ticket_id']}",
        "context_ref": f"ctx:ticket:{queue['ticket_id']}:v{queue['context_version']}",
        "offer_id": offer["offer_id"],
        "expires_at": offer["expires_at"],
    }


def post_envelope(endpoint: str, envelope: dict[str, Any]) -> None:
    request = urllib.request.Request(
        validate_endpoint(endpoint),
        data=json.dumps(envelope).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(_RefuseRedirects)
    with opener.open(request, timeout=_TIMEOUT_SECONDS) as response:
        if response.status >= 400:
            raise urllib.error.HTTPError(
                endpoint, response.status, "wakeup refused", response.headers, None
            )


def write_local_notification(root: Path, envelope: dict[str, Any]) -> Path:
    """The fallback when no endpoint is reachable: leave it somewhere findable."""
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{envelope['kind'].replace('.', '_')}_{envelope['nonce']}.json"
    target.write_text(json.dumps(envelope, indent=2, sort_keys=True), encoding="utf-8")
    return target


def deliver_pending_offers(
    service: Any,
    *,
    limit: int = 50,
    secret: str | None = None,
    sender: Any = None,
) -> dict[str, Any]:
    """Knock once for every undelivered offer that has somewhere to knock.

    Safe to call repeatedly: an offer is marked before the attempt is recorded,
    so a retry cannot re-notify an agent that already heard, and the offer itself
    is never altered — failing to deliver loses a notification, never the work.
    """
    post = sender or post_envelope
    if not secret:
        # Wakeup is off until an operator configures a key. Returning before the
        # loop leaves every offer pending rather than spending its bounded
        # attempts on a delivery that cannot be signed, so configuring the key
        # later starts knocking instead of finding the attempts already burnt.
        return {"delivered": [], "failed": [], "skipped": [], "unconfigured": True}

    delivered: list[str] = []
    failed: list[str] = []
    skipped: list[str] = []

    for offer in service.pending_wakeups(limit=limit):
        endpoint = offer.get("endpoint")
        if not endpoint:
            # Nothing to knock on. The offer stands and polling will find it.
            service.record_wakeup(offer["offer_id"], status="skipped")
            skipped.append(offer["offer_id"])
            continue
        try:
            envelope = build_envelope(
                offer_payload(offer, offer),
                uuid4().hex,
                secret,
                kind=f"work.offered.{offer['target_role']}",
                require_signature=True,
            )
            post(endpoint, envelope)
        except Exception:
            # Nothing a wakeup can do to itself may cost the caller its sweep:
            # this runs inside lease recovery, whose failure loses real work.
            service.record_wakeup(offer["offer_id"], status="failed")
            failed.append(offer["offer_id"])
            continue
        service.record_wakeup(offer["offer_id"], status="delivered")
        delivered.append(offer["offer_id"])

    return {
        "delivered": delivered,
        "failed": failed,
        "skipped": skipped,
        "unconfigured": False,
    }
