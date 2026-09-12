"""Knocking on the door of whichever role work is waiting on (HOH-251)."""
from __future__ import annotations

import json

import pytest

from hoh_control_plane import agent_wakeup
from hoh_control_plane.domain import Actor, Role, ValidationError


ENDPOINT = "http://127.0.0.1:9099/wake"
SECRET = "a-signing-secret-long-enough-to-matter"
ADMIN = Actor("scheduler-01", Role.ADMIN, frozenset({"*"}))


class _Recorder:
    """Stands in for the network, and can be told to fail."""

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[tuple[str, dict]] = []
        self.fail = fail

    def __call__(self, endpoint: str, envelope: dict) -> None:
        if self.fail:
            raise OSError("unreachable")
        self.sent.append((endpoint, envelope))


def _register(service, actors, role: str, *, endpoint: str | None = ENDPOINT):
    return service.register_agent(actors[role], skills=[f"hoh-{role}"], endpoint=endpoint)


def _offer(service, actors, seeded, role: str = "developer", **kwargs):
    """Get one live offer sitting in front of the named role."""
    _register(service, actors, role, **kwargs)
    return service.dispatch_pending_work(ADMIN)


def _deliver(service, sender):
    return agent_wakeup.deliver_pending_offers(service, secret=SECRET, sender=sender)


# --- registering somewhere to knock -------------------------------------------


def test_an_agent_may_register_where_to_knock(service, actors, seeded) -> None:
    presence = _register(service, actors, "developer")

    assert presence["endpoint"] == ENDPOINT


def test_registering_no_endpoint_is_not_a_degraded_mode(service, actors, seeded) -> None:
    """Polling stays a complete path on its own."""
    presence = _register(service, actors, "developer", endpoint=None)

    assert presence["endpoint"] is None
    inbox = service.get_agent_inbox(actors["developer"], "project-1")
    assert "offers" in inbox, "it still learns about work the ordinary way"


def test_an_endpoint_off_the_allowlist_is_refused(service, actors, seeded) -> None:
    """One that looks deliverable and is not would swallow every wakeup silently."""
    with pytest.raises(ValidationError) as error:
        _register(service, actors, "developer", endpoint="http://evil.example.com/wake")

    assert error.value.details["reason"] == "endpoint_not_allowed"


# --- the knock itself ---------------------------------------------------------


@pytest.mark.parametrize("role", ["developer", "qa", "integrator"])
def test_every_role_is_woken_not_only_the_planner(
    service, actors, seeded, role: str
) -> None:
    """The queue already targets the right role; this is the part that was missing."""
    ticket = seeded["ticket"]
    if role == "qa":
        service.claim_ticket(actors["developer"], ticket["ticket_id"], ticket["version"], 900)
    _register(service, actors, role)
    service.dispatch_pending_work(ADMIN)
    sender = _Recorder()

    result = _deliver(service, sender)

    if result["delivered"]:
        endpoint, envelope = sender.sent[0]
        assert endpoint == ENDPOINT
        assert envelope["kind"].startswith("work.offered.")


def test_the_knock_says_where_to_look_and_nothing_else(service, actors, seeded) -> None:
    _offer(service, actors, seeded)
    sender = _Recorder()

    _deliver(service, sender)

    assert sender.sent, "a developer with an endpoint was knocked on"
    payload = sender.sent[0][1]["payload"]
    assert set(payload) == {
        "project_id", "target_role", "ticket_ref", "context_ref", "offer_id", "expires_at"
    }
    rendered = json.dumps(sender.sent[0][1])
    assert "lease" not in rendered.lower()
    assert seeded["ticket"]["objective"] not in rendered
    assert SECRET not in rendered, "the key signs, it does not travel"


def test_the_knock_is_signed(service, actors, seeded) -> None:
    _offer(service, actors, seeded)
    sender = _Recorder()

    _deliver(service, sender)

    assert sender.sent[0][1]["signature"], "a receiver can tell a forgery"
    assert sender.sent[0][1]["nonce"], "and a replay"


def test_an_unsigned_delivery_cannot_be_built(service) -> None:
    with pytest.raises(agent_wakeup.SigningSecretMissing):
        agent_wakeup.build_envelope(
            {"x": 1}, "nonce", None, kind="work.offered.developer", require_signature=True
        )


# --- what failure must not cost ------------------------------------------------


def test_an_unreachable_agent_does_not_lose_its_work(service, actors, seeded) -> None:
    """Losing delivery costs freshness and nothing else."""
    _offer(service, actors, seeded)
    ticket_before = service.get_ticket_context(actors["planner"], seeded["ticket"]["ticket_id"])

    result = _deliver(service, _Recorder(fail=True))

    assert result["failed"], "the knock failed"
    inbox = service.get_agent_inbox(actors["developer"], "project-1")
    assert inbox["offers"], "the offer still stands and polling still finds it"
    after = service.get_ticket_context(actors["planner"], seeded["ticket"]["ticket_id"])
    assert after["ticket"]["status"] == ticket_before["ticket"]["status"]


def test_an_agent_with_no_endpoint_is_skipped_not_retried_forever(
    service, actors, seeded
) -> None:
    _offer(service, actors, seeded, endpoint=None)

    result = _deliver(service, _Recorder())

    assert result["skipped"], "nothing to knock on, so nothing is attempted"
    assert not result["failed"]


def test_delivery_stops_after_a_bounded_number_of_attempts(
    service, actors, seeded
) -> None:
    """An unreachable agent must not become an unbounded queue of requests."""
    _offer(service, actors, seeded)

    for _ in range(agent_wakeup.MAX_DELIVERY_ATTEMPTS + 2):
        _deliver(service, _Recorder(fail=True))

    assert service.pending_wakeups() == [], "it gives up and leaves the work to polling"


def test_a_second_run_does_not_knock_twice_for_the_same_offer(
    service, actors, seeded
) -> None:
    _offer(service, actors, seeded)
    first = _Recorder()
    _deliver(service, first)

    second = _Recorder()
    _deliver(service, second)

    assert len(first.sent) == 1
    assert second.sent == [], "delivered once stays delivered"


def test_recording_a_wakeup_never_touches_the_work(service, actors, seeded) -> None:
    _offer(service, actors, seeded)
    offer_id = service.pending_wakeups()[0]["offer_id"]

    recorded = service.record_wakeup(offer_id, status="failed")

    assert recorded["status"] == "offered", "the offer itself is untouched"
    assert recorded["delivery_status"] == "failed"
    assert recorded["delivery_attempts"] == 1


def test_an_unknown_delivery_status_is_refused(service, actors, seeded) -> None:
    _offer(service, actors, seeded)
    offer_id = service.pending_wakeups()[0]["offer_id"]

    with pytest.raises(ValidationError):
        service.record_wakeup(offer_id, status="probably-fine")


def test_a_wakeup_carries_no_authority(service, actors, seeded) -> None:
    """It tells an agent to come and look; it cannot act on the agent's behalf."""
    _offer(service, actors, seeded)
    _deliver(service, _Recorder())

    with service.store.connection() as connection:
        leases = connection.execute(
            "SELECT COUNT(*) FROM leases WHERE ticket_id = ?",
            (seeded["ticket"]["ticket_id"],),
        ).fetchone()[0]

    assert leases == 0, "being told about work is not claiming it"


# --- the sweep that actually knocks ---------------------------------------------


def test_an_unconfigured_secret_spends_no_attempts(service, actors, seeded) -> None:
    """Configuring the key later must start knocking, not find the attempts gone."""
    _offer(service, actors, seeded)
    recorder = _Recorder()

    result = agent_wakeup.deliver_pending_offers(service, secret=None, sender=recorder)

    assert result["unconfigured"] is True
    assert recorder.sent == []
    assert service.pending_wakeups(), "the offer is still there to knock about"


def test_a_wakeup_failure_never_costs_the_sweep(service, actors, seeded) -> None:
    """This runs inside lease recovery, whose failure loses real work."""
    _offer(service, actors, seeded)

    def explode(endpoint: str, envelope: dict) -> None:
        raise RuntimeError("the network is on fire")

    result = agent_wakeup.deliver_pending_offers(
        service, secret=SECRET, sender=explode
    )

    assert result["failed"], "recorded, not raised"


def test_the_scheduler_sweep_knocks_on_every_role(
    service, store, actors, seeded, monkeypatch
) -> None:
    """Delivery that nothing calls is not delivery."""
    from hoh_control_plane import scheduler

    monkeypatch.setenv(agent_wakeup.SECRET_VARIABLE, SECRET)
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        agent_wakeup, "post_envelope", lambda endpoint, envelope: sent.append((endpoint, envelope))
    )
    _register(service, actors, "developer")

    swept = scheduler.sweep_once(store.path)

    assert swept["agent_wakeups"]["delivered"], "the sweep is what does the knocking"
    assert sent[0][0] == ENDPOINT
