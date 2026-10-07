"""Edges of the two bearer-token maps (audit D: api.py, auth.py).

The MCP map (``auth.credentials_from_environment``) already refused short and
blank tokens; the REST map (``api._token_identities_from_environment``) did
not. An empty key there matched the header ``Authorization: Bearer `` and a
string ``project_ids`` became a set of single characters.
"""
from __future__ import annotations

import json

import pytest

from hoh_control_plane import api
from hoh_control_plane.auth import credentials_from_environment
from hoh_control_plane.domain import Role, ValidationError

GOOD = "t" * 32


def _rest(monkeypatch, mapping) -> dict:
    monkeypatch.setenv("HOH_AGENT_TOKENS_JSON", json.dumps(mapping))
    return api._token_identities_from_environment()


def test_rest_tokens_map_to_their_identity(monkeypatch) -> None:
    identities = _rest(monkeypatch, {GOOD: {"agent_id": "dev-01", "role": "developer",
                                            "project_ids": ["project-1"]}})
    actor = identities[GOOD]
    assert (actor.agent_id, actor.role, actor.project_ids) == ("dev-01", Role.DEVELOPER,
                                                               frozenset({"project-1"}))


def test_no_rest_token_map_means_no_identities(monkeypatch) -> None:
    monkeypatch.delenv("HOH_AGENT_TOKENS_JSON", raising=False)
    assert api._token_identities_from_environment() == {}


@pytest.mark.parametrize("token", ["", " ", "\t"])
def test_a_blank_rest_token_is_refused_at_startup(monkeypatch, token) -> None:
    with pytest.raises(RuntimeError, match="HOH_AGENT_TOKENS_JSON"):
        _rest(monkeypatch, {token: {"agent_id": "dev-01", "role": "developer",
                                    "project_ids": ["project-1"]}})


def test_rest_project_ids_must_be_a_list_not_a_string(monkeypatch) -> None:
    with pytest.raises(RuntimeError, match="HOH_AGENT_TOKENS_JSON"):
        _rest(monkeypatch, {GOOD: {"agent_id": "dev-01", "role": "developer",
                                   "project_ids": "project-1"}})


@pytest.mark.parametrize("raw", ["[]", "\"text\"", "{bad json", json.dumps({GOOD: "dev-01"}),
                                 json.dumps({GOOD: {"agent_id": "dev-01", "role": "owner"}})])
def test_malformed_rest_token_maps_are_refused(monkeypatch, raw) -> None:
    monkeypatch.setenv("HOH_AGENT_TOKENS_JSON", raw)
    with pytest.raises(RuntimeError, match="HOH_AGENT_TOKENS_JSON"):
        api._token_identities_from_environment()


def _mcp(token: str, **value) -> str:
    return json.dumps({token: {"agent_id": "dev-01", "role": "developer",
                               "project_ids": ["project-1"], **value}})


def test_mcp_tokens_have_a_32_character_floor() -> None:
    assert len(credentials_from_environment(_mcp("t" * 32))) == 1
    with pytest.raises(ValidationError, match="at least 32"):
        credentials_from_environment(_mcp("t" * 31))


@pytest.mark.parametrize("token", ["t" * 31 + " ", "t" * 31 + "é"])
def test_mcp_tokens_are_ascii_without_whitespace(token) -> None:
    with pytest.raises(ValidationError, match="ASCII"):
        credentials_from_environment(_mcp(token))


@pytest.mark.parametrize("raw", ["{}", "[]", "{bad json"])
def test_an_empty_or_malformed_mcp_map_is_a_validation_error(raw) -> None:
    with pytest.raises(ValidationError):
        credentials_from_environment(raw)


def test_no_mcp_map_means_no_credentials() -> None:
    assert credentials_from_environment("") == ()
