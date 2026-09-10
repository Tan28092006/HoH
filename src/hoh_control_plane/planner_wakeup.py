"""Deliver bounded Planner intervention requests to a local Planner endpoint.

This adapter only asks a Planner to look. It carries identifiers and evidence
references, never the evidence itself, and it can neither claim a ticket nor
resolve a planning decision. Delivery is at-least-once: a request that cannot be
delivered stays PENDING so the Planner heartbeat still finds it by polling.

Trusted-local only. The endpoint must be a loopback address, because the envelope
is authenticated with a shared secret and this pilot has neither authenticated
HTTP MCP nor a multi-machine store.
"""

from __future__ import annotations

import argparse
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

from hoh_control_plane.service import ControlPlaneService
from hoh_control_plane.store import SQLiteStore


ENDPOINT_VARIABLE = "HOH_PLANNER_WAKEUP_URL"
SECRET_VARIABLE = "HOH_PLANNER_WAKEUP_SECRET"
ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
DELIVERY_TIMEOUT_SECONDS = 5


class EndpointNotAllowed(ValueError):
    """The configured endpoint is not a loopback address this pilot may call."""


class SigningSecretMissing(ValueError):
    """A registered endpoint was configured without the secret that authenticates it."""


def validate_endpoint(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in ALLOWED_HOSTS:
        raise EndpointNotAllowed(
            "Planner wakeup endpoint must be an http(s) loopback address"
        )
    return endpoint


def build_envelope(
    payload: dict[str, Any],
    nonce: str,
    secret: str | None,
    *,
    require_signature: bool = False,
) -> dict[str, Any]:
    """Wrap a bounded payload so the receiver can reject replays and forgeries.

    Anything sent to a registered endpoint must be signed, so that path sets
    require_signature and this refuses to build an unauthenticated envelope. The
    invariant lives here rather than at the call site, so a later caller cannot
    reintroduce an unsigned delivery by forgetting to check.
    """
    if require_signature and not secret:
        raise SigningSecretMissing(
            f"{SECRET_VARIABLE} must be set to deliver to a registered endpoint"
        )
    envelope: dict[str, Any] = {
        "kind": "planner.intervention",
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


def write_local_notification(root: Path, envelope: dict[str, Any]) -> Path:
    """Fallback when no endpoint is registered: leave a bounded note on disk.

    Codex Desktop exposes no external wake API, so an operator or a polling
    worker reads these instead. The file holds only the same bounded payload.
    """
    root.mkdir(parents=True, exist_ok=True)
    destination = root / f"{envelope['payload']['intervention_id']}.json"
    destination.write_text(json.dumps(envelope, indent=2, sort_keys=True), encoding="utf-8")
    return destination


def deliver_once(
    service: ControlPlaneService, intervention: dict[str, Any], *, endpoint: str | None,
    secret: str | None, notification_root: Path,
) -> dict[str, Any]:
    """Attempt one delivery and record its outcome without touching the ticket."""
    payload = service.intervention_payload(intervention)
    if endpoint is None:
        # The fallback writes to this machine's own disk, so there is no remote
        # party to authenticate; it is signed only when a secret happens to exist.
        write_local_notification(
            notification_root, build_envelope(payload, intervention["nonce"], secret)
        )
        return service.record_intervention_delivery(
            intervention["intervention_id"], delivered=True, detail=None
        )
    try:
        validate_endpoint(endpoint)
        envelope = build_envelope(
            payload, intervention["nonce"], secret, require_signature=True
        )
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(envelope).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=DELIVERY_TIMEOUT_SECONDS) as response:
            if response.status >= 400:
                raise urllib.error.HTTPError(
                    endpoint, response.status, "rejected", response.headers, None
                )
    except (
        EndpointNotAllowed,
        SigningSecretMissing,
        urllib.error.URLError,
        OSError,
        ValueError,
    ) as error:
        # The evidence and the Planner job survive; only delivery failed.
        return service.record_intervention_delivery(
            intervention["intervention_id"], delivered=False, detail=str(error)[:2000]
        )
    return service.record_intervention_delivery(
        intervention["intervention_id"], delivered=True, detail=None
    )


def deliver_pending(service: ControlPlaneService, *, limit: int = 20) -> dict[str, Any]:
    """Deliver every pending wakeup once; safe to call repeatedly."""
    endpoint = os.getenv(ENDPOINT_VARIABLE) or None
    secret = os.getenv(SECRET_VARIABLE) or None
    notification_root = service.store.path.parent / "notifications"
    delivered: list[str] = []
    failed: list[str] = []
    for intervention in service.pending_planner_interventions(limit=limit):
        result = deliver_once(
            service,
            intervention,
            endpoint=endpoint,
            secret=secret,
            notification_root=notification_root,
        )
        if result["status"] == "DELIVERED":
            delivered.append(result["intervention_id"])
        else:
            failed.append(result["intervention_id"])
    return {"delivered": delivered, "failed": failed, "endpoint_configured": endpoint is not None}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Deliver pending Planner intervention requests")
    parser.add_argument("--database", required=True)
    parser.add_argument("--limit", type=int, default=20)
    arguments = parser.parse_args(argv)
    store = SQLiteStore(Path(arguments.database))
    store.initialize()
    result = deliver_pending(ControlPlaneService(store), limit=arguments.limit)
    print(
        f"Delivered {len(result['delivered'])} wakeup(s); {len(result['failed'])} still pending.",
        flush=True,
    )


if __name__ == "__main__":
    main()
