"""The board reacts to change signals without the stream becoming the state (HOH-221)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hoh_control_plane.api import create_app


STREAM = "/api/v1/projects/demo/events/stream"


def headers(role: str = "planner", agent: str = "ui-observer") -> dict[str, str]:
    return {"X-HOH-Agent-Id": agent, "X-HOH-Role": role}


def _app(tmp_path: Path):
    """A stream that drains what is available and then closes.

    max_idle_polls=0 ends the generator at the first idle poll, so a test reads a
    complete, finite stream instead of racing a timer.
    """
    return create_app(
        database_path=tmp_path / "stream.db",
        local_mode=True,
        seed_demo=True,
        event_stream_max_idle_polls=0,
    )


def _frames(body: str) -> list[dict[str, str]]:
    """Parse an SSE body into its frames."""
    frames = []
    for block in body.split("\n\n"):
        block = block.strip("\n")
        if not block or block.startswith(":"):
            continue
        frame: dict[str, str] = {}
        for line in block.splitlines():
            if line.startswith("retry:"):
                frame["retry"] = line.split(":", 1)[1].strip()
            elif ":" in line:
                key, value = line.split(":", 1)
                frame[key.strip()] = value.strip()
        if frame:
            frames.append(frame)
    return frames


def _read(client: TestClient, url: str, **kwargs) -> list[dict[str, str]]:
    with client.stream("GET", url, **kwargs) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        return _frames("".join(response.iter_text()))


def test_the_stream_emits_change_signals_after_a_cursor(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path)) as client:
        frames = _read(client, f"{STREAM}?after=0", headers=headers())

    changes = [f for f in frames if f.get("event") == "change"]
    assert changes, "seeded project has audit events to report"
    assert all("id" in frame for frame in changes)
    sequences = [int(frame["id"]) for frame in changes]
    assert sequences == sorted(sequences)


def test_native_eventsource_can_read_the_explicit_local_demo(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        frames = _read(client, STREAM)
        assert any(frame.get("event") == "change" for frame in frames)


def test_a_signal_carries_identity_not_content(tmp_path: Path) -> None:
    """payload is free-form text written by whichever call emitted the event."""
    with TestClient(_app(tmp_path)) as client:
        frames = _read(client, f"{STREAM}?after=0", headers=headers())

    for frame in (f for f in frames if f.get("event") == "change"):
        signal = json.loads(frame["data"])
        assert set(signal) == {
            "sequence",
            "event_id",
            "event_type",
            "aggregate_type",
            "aggregate_id",
            "aggregate_version",
            "actor_role",
            "occurred_at",
        }
        assert "payload" not in signal
        assert "actor_id" not in signal


def test_a_reconnect_resumes_from_its_last_id_without_repeating(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as client:
        first = _read(client, f"{STREAM}?after=0", headers=headers())
        seen = [int(f["id"]) for f in first if f.get("event") == "change"]
        assert seen
        resumed = _read(
            client,
            STREAM,
            headers={**headers(), "Last-Event-ID": str(seen[-1])},
        )

    again = [int(f["id"]) for f in resumed if f.get("event") == "change"]
    assert not set(again) & set(seen), "a reconnect must not replay what was delivered"


def test_events_written_between_reads_are_all_delivered(tmp_path: Path) -> None:
    """A cursor gap must close, not silently swallow the events inside it."""
    app = _app(tmp_path)
    with TestClient(app) as client:
        first = _read(client, f"{STREAM}?after=0", headers=headers())
        cursor = max(int(f["id"]) for f in first if f.get("event") == "change")

        ticket = client.get(
            "/api/v1/tickets/DEV-142/context", headers=headers("developer", "dev-stream")
        ).json()["data"]["ticket"]
        claim = client.post(
            "/api/v1/tickets/DEV-142/leases",
            headers=headers("developer", "dev-stream"),
            json={"expected_version": ticket["version"], "lease_seconds": 900},
        )
        assert claim.status_code == 201

        after_write = _read(client, f"{STREAM}?after={cursor}", headers=headers())

    delivered = [int(f["id"]) for f in after_write if f.get("event") == "change"]
    assert delivered, "the events written after the cursor must arrive"
    assert min(delivered) == cursor + 1, "no event between the cursor and now is skipped"


def test_a_cursor_this_history_cannot_contain_asks_for_a_reload(tmp_path: Path) -> None:
    """Otherwise a client with a stale cursor waits forever for events that never come."""
    with TestClient(_app(tmp_path)) as client:
        frames = _read(client, f"{STREAM}?after=999999", headers=headers())

    assert any(frame.get("event") == "reset" for frame in frames)


def test_the_stream_advertises_a_reconnect_delay(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path)) as client:
        frames = _read(client, f"{STREAM}?after=0", headers=headers())

    assert any("retry" in frame for frame in frames)


def test_the_stream_is_scoped_to_one_project(tmp_path: Path) -> None:
    app = create_app(
        database_path=tmp_path / "stream.db",
        local_mode=False,
        seed_demo=True,
        token_identities={},
        event_stream_max_idle_polls=0,
    )
    with TestClient(app) as client:
        refused = client.get(f"{STREAM}?after=0")

    assert refused.status_code in (401, 403)


def test_a_refused_project_fails_as_a_response_not_inside_an_open_stream(
    tmp_path: Path,
) -> None:
    """Authorization is checked before the stream opens, so the client sees an error."""
    from hoh_control_plane.domain import Actor, Role

    token = "stream-token-that-is-long-enough-here"
    app = create_app(
        database_path=tmp_path / "stream.db",
        local_mode=False,
        seed_demo=True,
        token_identities={
            token: Actor("outsider-01", Role.PLANNER, frozenset({"other-project"}))
        },
        event_stream_max_idle_polls=0,
    )
    with TestClient(app) as client:
        response = client.get(
            f"{STREAM}?after=0", headers={"Authorization": f"Bearer {token}"}
        )

    assert response.status_code >= 400
    assert not response.headers["content-type"].startswith("text/event-stream")


def test_the_rest_snapshot_still_serves_when_a_client_cannot_stream(
    tmp_path: Path,
) -> None:
    """The stream is an accelerator; losing it must not cost the board its state."""
    with TestClient(_app(tmp_path)) as client:
        board = client.get("/api/v1/projects/demo/board")
        events = client.get("/api/v1/projects/demo/events?after=0", headers=headers())

    assert board.status_code == 200
    assert board.json()["data"]["project"]["project_id"] == "demo"
    assert events.status_code == 200
    assert events.json()["data"]["events"]


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_the_stream_cannot_be_written_through(tmp_path: Path, method: str) -> None:
    """One-way by construction: it is a GET that produces, never a mutation channel."""
    with TestClient(_app(tmp_path)) as client:
        response = client.request(method, STREAM, headers=headers())

    assert response.status_code == 405
