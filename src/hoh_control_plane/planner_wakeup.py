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
from hoh_control_plane.store_factory import store_from_environment


ENDPOINT_VARIABLE = "HOH_PLANNER_WAKEUP_URL"
from hoh_control_plane import agent_wakeup as _shared

DELIVERY_TIMEOUT_SECONDS = 5.0
SECRET_VARIABLE = "HOH_PLANNER_WAKEUP_SECRET"
# The allowlist, the envelope and the redirect refusal live in agent_wakeup and
# are imported rather than restated. Two copies of a security primitive drift,
# and the copy that does not get the fix is the one still in the path.
ALLOWED_HOSTS = _shared.ALLOWED_HOSTS
ALLOWED_HOSTS_VARIABLE = _shared.ALLOWED_HOSTS_VARIABLE
EndpointNotAllowed = _shared.EndpointNotAllowed
SigningSecretMissing = _shared.SigningSecretMissing
allowed_hosts = _shared.allowed_hosts
validate_endpoint = _shared.validate_endpoint


def build_envelope(
    payload: dict[str, Any],
    nonce: str,
    secret: str | None,
    *,
    require_signature: bool = False,
    kind: str = "planner.intervention",
) -> dict[str, Any]:
    """The shared envelope, defaulted to this module's one kind.

    kind is required on the shared primitive because it no longer has one
    caller; naming it here keeps that explicit without every planner call site
    repeating the only value it could have.
    """
    return _shared.build_envelope(
        payload, nonce, secret,
        require_signature=require_signature, kind=kind, secret_variable=SECRET_VARIABLE,
    )


write_local_notification = _shared.write_local_notification


def _open_endpoint(request: urllib.request.Request):
    """Single seam for endpoint delivery, so no caller can bypass the guard."""
    return urllib.request.build_opener(_shared._RefuseRedirects).open(
        request, timeout=DELIVERY_TIMEOUT_SECONDS
    )


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
        with _open_endpoint(request) as response:
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
    store = store_from_environment(arguments.database)
    result = deliver_pending(ControlPlaneService(store), limit=arguments.limit)
    print(
        f"Delivered {len(result['delivered'])} wakeup(s); {len(result['failed'])} still pending.",
        flush=True,
    )


if __name__ == "__main__":
    main()
