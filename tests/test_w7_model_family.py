"""Wave 7C-3 edge cases: model families and the checker-distinct-from-maker rule."""
from __future__ import annotations

import json

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from helpers.w7 import events, finding, submit_fake, ticket, verdict

from hoh_control_plane.domain import AuthorizationError, ValidationError
from hoh_control_plane.local_agent import MODEL_FAMILIES, model_family_of, worker_agent_text
from hoh_control_plane.mcp_server import create_mcp_server
from hoh_control_plane.service import ControlPlaneService
from helpers.desktop import desktop  # noqa: F401  (fixture)


# --------------------------------------------------------- normalisation


@pytest.mark.parametrize(
    ("given", "stored"),
    [("anthropic", "anthropic"), ("Anthropic", "anthropic"), ("  OPENAI  ", "openai"),
     ("openai/gpt-5", "openai"), ("Google/Gemini-3", "google"), ("other", "other"),
     ("mistral", "mistral"), ("a" * 40, "a" * 40), (None, None), ("", None), ("   ", None)],
)
def test_model_family_is_normalised(given, stored) -> None:
    assert ControlPlaneService._normalized_model_family(given) == stored


@pytest.mark.parametrize("bad", ["a" * 41, "open ai", "-anthropic", "anthropic!", "/gpt-5", "élan"])
def test_an_unportable_model_family_is_refused(bad) -> None:
    with pytest.raises(ValidationError) as refused:
        ControlPlaneService._normalized_model_family(bad)
    assert refused.value.details["reason"] == "model_family_invalid"


def test_register_stores_the_normalised_family_and_trims_the_model(service, actors, seeded) -> None:
    registered = service.register_agent(
        actors["qa"], skills=["hoh-qa"], model_family="OpenAI/gpt-5", model="  gpt-5  "
    )
    with service.store.connection() as connection:
        row = connection.execute(
            "SELECT model_family, model FROM agent_presence WHERE agent_id = 'qa-01'"
        ).fetchone()
    assert row["model_family"] == "openai"
    assert row["model"] == "gpt-5"
    assert registered["agent_id"] == "qa-01"


def test_a_blank_or_long_model_name_is_refused(service, actors, seeded) -> None:
    with pytest.raises(ValidationError):
        service.register_agent(actors["qa"], skills=["hoh-qa"], model="   ")
    with pytest.raises(ValidationError):
        service.register_agent(actors["qa"], skills=["hoh-qa"], model="m" * 121)


def test_re_registering_without_a_family_forgets_it(service, actors, seeded) -> None:
    service.register_agent(actors["qa"], skills=["hoh-qa"], model_family="openai")
    service.register_agent(actors["qa"], skills=["hoh-qa"])
    with service.store.connection() as connection:
        assert connection.execute(
            "SELECT model_family FROM agent_presence WHERE agent_id = 'qa-01'"
        ).fetchone()["model_family"] is None


@pytest.mark.parametrize(
    ("model", "family"),
    [("claude-opus-5-5", "anthropic"), ("opus", "anthropic"), ("Sonnet-4", "anthropic"),
     ("haiku-3", "anthropic"), ("fable", "anthropic"), ("anthropic/some-model", "anthropic"),
     ("us.anthropic.claude-x", "anthropic"), ("gpt-5", "openai"), ("GPT-4o", "openai"),
     ("openai/gpt-5", "openai"), ("o1-preview", "openai"), ("o3", "openai"),
     ("o4-mini", "openai"), ("codex-mini", "openai"), ("chatgpt-4o-latest", "openai"),
     ("gemini-3-pro", "google"), ("google/gemma-2", "google"), ("llama-4", "other"),
     ("vendor/deep/path/claude-x", "anthropic"), ("x", "other"), ("", None), (None, None)],
)
def test_local_agent_derives_the_family_from_the_model_id(model, family) -> None:
    assert model_family_of(model) == family
    assert family is None or family in MODEL_FAMILIES


def test_the_worker_agent_text_names_the_family_only_when_known() -> None:
    assert 'model_family="openai"' in worker_agent_text("hoh-x", "qa", "gpt-5", "openai")
    assert "model_family" not in worker_agent_text("hoh-x", "qa", "gpt-5")


# ------------------------------------------------------ install_profile


def _setup_project(desktop):
    from helpers.desktop import headers

    client, root = desktop
    client.post("/local/projects", json={"project_id": "shop", "name": "Shop"},
                headers=headers(client))
    return root


def test_setup_with_a_model_records_its_family_for_the_stdio_server(desktop, tmp_path) -> None:
    from hoh_control_plane.local_agent import install_profile, model_profile_path

    root = _setup_project(desktop)
    result = install_profile(root, "shop", "qa", "primary", "claude-code", tmp_path / "home",
                             model="gpt-5")

    assert result["model_family"] == "openai"
    assert 'model_family="openai"' in result["register_hint"]
    recorded = json.loads(model_profile_path(root, result["agent_id"]).read_text(encoding="utf-8"))
    assert recorded == {"model_family": "openai", "model": "gpt-5"}


def test_an_explicit_family_overrides_the_derived_one(desktop, tmp_path) -> None:
    from hoh_control_plane.local_agent import install_profile

    root = _setup_project(desktop)
    result = install_profile(root, "shop", "developer", "primary", "claude-code",
                             tmp_path / "home", model="my-finetune", model_family="anthropic")
    assert result["model_family"] == "anthropic"


def test_an_unknown_explicit_family_is_refused_by_setup(desktop, tmp_path) -> None:
    from hoh_control_plane.local_agent import install_profile

    root = _setup_project(desktop)
    with pytest.raises(ValueError, match="Model family"):
        install_profile(root, "shop", "qa", "primary", "claude-code", tmp_path / "home",
                        model="gpt-5", model_family="OpenAI")


def test_setup_without_a_model_records_no_family(desktop, tmp_path) -> None:
    from hoh_control_plane.local_agent import install_profile, model_profile_path

    root = _setup_project(desktop)
    result = install_profile(root, "shop", "qa", "primary", "claude-code", tmp_path / "home")
    assert "model_family" not in result
    assert not model_profile_path(root, result["agent_id"]).exists()


# ------------------------------------------------------- stdio env fallback


@pytest.mark.anyio
async def test_the_stdio_environment_supplies_the_family_when_omitted(
    service, actors, seeded, monkeypatch
) -> None:
    monkeypatch.setenv("HOH_AGENT_MODEL_FAMILY", "google")
    monkeypatch.setenv("HOH_AGENT_MODEL", "gemini-3")
    server = create_mcp_server(service, actors["qa"])

    _, presence = await server.call_tool("agent.register", {"skills": ["hoh-qa"]})

    with service.store.connection() as connection:
        row = connection.execute(
            "SELECT model_family, model FROM agent_presence WHERE agent_id = 'qa-01'"
        ).fetchone()
    assert (row["model_family"], row["model"]) == ("google", "gemini-3")
    assert presence["agent_id"] == "qa-01"


@pytest.mark.anyio
async def test_an_explicit_family_wins_over_the_environment(
    service, actors, seeded, monkeypatch
) -> None:
    monkeypatch.setenv("HOH_AGENT_MODEL_FAMILY", "google")
    monkeypatch.setenv("HOH_AGENT_MODEL", "gemini-3")
    server = create_mcp_server(service, actors["qa"])

    await server.call_tool("agent.register", {"skills": ["hoh-qa"], "model_family": "openai"})

    with service.store.connection() as connection:
        row = connection.execute(
            "SELECT model_family, model FROM agent_presence WHERE agent_id = 'qa-01'"
        ).fetchone()
    # The environment's model belongs to the environment's family, so it is not
    # borrowed for an explicitly different family.
    assert (row["model_family"], row["model"]) == ("openai", None)


@pytest.mark.anyio
async def test_an_empty_environment_family_means_unknown(service, actors, seeded, monkeypatch) -> None:
    monkeypatch.setenv("HOH_AGENT_MODEL_FAMILY", "")
    server = create_mcp_server(service, actors["qa"])
    await server.call_tool("agent.register", {"skills": ["hoh-qa"]})
    with service.store.connection() as connection:
        assert connection.execute(
            "SELECT model_family FROM agent_presence WHERE agent_id = 'qa-01'"
        ).fetchone()["model_family"] is None


@pytest.mark.anyio
async def test_an_invalid_family_is_refused_through_mcp(service, actors, seeded) -> None:
    server = create_mcp_server(service, actors["qa"])
    with pytest.raises(ToolError):
        await server.call_tool("agent.register", {"skills": ["hoh-qa"], "model_family": "a b"})


# ------------------------------------------------------- checker vs maker


def _register(service, actors, role: str, family: str | None) -> None:
    service.register_agent(actors[role], skills=[f"hoh-{role}"], model_family=family)


def _submitted(service, actors, maker: str | None) -> dict:
    _register(service, actors, "developer", maker)
    return submit_fake(service, actors, "DEV-142")


def _claim_qa(service, actors):
    return service.claim_qa(actors["qa"], "DEV-142", ticket(service, "DEV-142")["version"], 900)


def test_a_same_family_checker_is_refused_at_claim(service, actors, seeded) -> None:
    _submitted(service, actors, "anthropic")
    _register(service, actors, "qa", "Anthropic")

    with pytest.raises(AuthorizationError) as refused:
        _claim_qa(service, actors)

    details = refused.value.details
    assert details["reason"] == "checker_same_family_as_maker"
    assert details["maker_model_family"] == details["checker_model_family"] == "anthropic"
    assert ticket(service, "DEV-142")["status"] == "QA_READY"
    with service.store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM leases WHERE ticket_id = 'DEV-142'").fetchone()[0] == 0


def test_a_distinct_family_is_recorded_on_the_claim(service, actors, seeded) -> None:
    _submitted(service, actors, "anthropic")
    _register(service, actors, "qa", "openai")

    claim = _claim_qa(service, actors)

    assert claim["family_check"]["status"] == "distinct"
    claimed = [e for e in events(service, "qa.started", "DEV-142")
               if "family_check" in e["payload"]]
    assert claimed[-1]["payload"]["family_check"]["status"] == "distinct"


@pytest.mark.parametrize(
    ("maker", "checker"),
    [(None, "openai"), ("anthropic", None), (None, None), ("other", "other"),
     ("other", "anthropic"), ("anthropic", "other")],
)
def test_an_unknown_or_other_family_is_allowed_but_unverified(
    service, actors, seeded, maker, checker
) -> None:
    _submitted(service, actors, maker)
    if checker is not None:
        _register(service, actors, "qa", checker)

    claim = _claim_qa(service, actors)

    assert claim["family_check"]["status"] == "family_unverified"


def test_with_the_policy_off_a_same_family_checker_may_claim(service, actors, seeded) -> None:
    service.configure_risk_policy(actors["planner"], "project-1", checker_distinct_from_maker=False)
    _submitted(service, actors, "openai")
    _register(service, actors, "qa", "openai")

    claim = _claim_qa(service, actors)

    assert claim["family_check"] == {"status": "disabled"}


def test_the_maker_family_is_frozen_with_the_candidate(service, actors, seeded) -> None:
    submitted = _submitted(service, actors, "anthropic")
    # The maker's session later re-registers under another family; the frozen
    # candidate still says who made it.
    _register(service, actors, "developer", "openai")
    _register(service, actors, "qa", "anthropic")

    with pytest.raises(AuthorizationError):
        _claim_qa(service, actors)
    with service.store.connection() as connection:
        stamped = connection.execute(
            "SELECT maker_model_family FROM candidates WHERE candidate_id = ?",
            (submitted["candidate"]["candidate_id"],),
        ).fetchone()["maker_model_family"]
    assert stamped == "anthropic"


def test_a_candidate_without_a_stamp_falls_back_to_its_submitter(service, actors, seeded) -> None:
    submitted = _submitted(service, actors, None)
    with service.store.connection(immediate=True) as connection:
        assert connection.execute(
            "SELECT maker_model_family FROM candidates WHERE candidate_id = ?",
            (submitted["candidate"]["candidate_id"],),
        ).fetchone()["maker_model_family"] is None
    _register(service, actors, "developer", "google")
    _register(service, actors, "qa", "google")

    with pytest.raises(AuthorizationError):
        _claim_qa(service, actors)


def test_the_family_is_re_checked_at_the_verdict(service, actors, seeded) -> None:
    _submitted(service, actors, "anthropic")
    _register(service, actors, "qa", "openai")
    claim = _claim_qa(service, actors)
    # A re-registration after the claim must not carry a same-family verdict.
    _register(service, actors, "qa", "anthropic")
    criterion = ticket(service, "DEV-142")["acceptance_criteria"][0]

    with pytest.raises(AuthorizationError) as refused:
        service.submit_qa_verdict(
            actors["qa"], "DEV-142", claim["lease"]["lease_id"],
            _candidate_id(service), claim["ticket"]["version"], "failed",
            [finding(criterion, "failed")],
            context_snapshot_id=claim["current_context_snapshot_id"],
        )
    assert refused.value.details["reason"] == "checker_same_family_as_maker"
    assert ticket(service, "DEV-142")["status"] == "QA_IN_PROGRESS"


def _candidate_id(service) -> str:
    with service.store.connection() as connection:
        return connection.execute(
            "SELECT candidate_id FROM candidates WHERE ticket_id = 'DEV-142' "
            "ORDER BY submitted_at DESC LIMIT 1"
        ).fetchone()["candidate_id"]


def test_the_verdict_records_the_family_check_status(service, actors, seeded) -> None:
    _submitted(service, actors, "anthropic")
    _register(service, actors, "qa", "openai")
    criteria = ticket(service, "DEV-142")["acceptance_criteria"]

    verdict(service, actors, "DEV-142", "passed", [finding(c) for c in criteria])

    [verified] = events(service, "ticket.verified", "DEV-142")
    assert verified["payload"]["family_check"] == "distinct"


def test_turning_the_policy_back_on_refuses_again(service, actors, seeded) -> None:
    service.configure_risk_policy(actors["planner"], "project-1", checker_distinct_from_maker=False)
    service.configure_risk_policy(actors["planner"], "project-1", checker_distinct_from_maker=True)
    _submitted(service, actors, "openai")
    _register(service, actors, "qa", "openai")
    with pytest.raises(AuthorizationError):
        _claim_qa(service, actors)


def test_developer_and_integrator_claims_are_not_family_checked(service, actors, seeded) -> None:
    _register(service, actors, "developer", "openai")
    claim = service.claim_ticket(actors["developer"], "DEV-142",
                                 ticket(service, "DEV-142")["version"], 900)
    assert "family_check" not in claim
