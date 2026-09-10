"""Validate the committed role skills as a shipped artifact.

These skills are installed into an agent client by scripts/install-agent-skill.ps1
and then read far from this repository, so a broken reference or a stale tool name
is only discoverable here.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPOSITORY_ROOT / "skills"
INSTALL_SCRIPT = REPOSITORY_ROOT / "scripts" / "install-agent-skill.ps1"
BASE_SKILL = "hoh-control-plane"
ROLE_SKILLS = ("hoh-planner", "hoh-developer", "hoh-qa", "hoh-integrator")
REQUIRED_FILES = (
    "hoh-control-plane/SKILL.md",
    "hoh-control-plane/references/wakeup.md",
    "hoh-planner/SKILL.md",
    "hoh-developer/SKILL.md",
    "hoh-qa/SKILL.md",
    "hoh-qa/references/test-design.md",
    "hoh-qa/references/ui-testing.md",
    "hoh-integrator/SKILL.md",
)
MARKDOWN_LINK = re.compile(r"\]\(([^)]+)\)")
TOOL_REFERENCE = re.compile(r"`([a-z_]+\.[a-z_]+)`")
BACKTICKED_PATH = re.compile(r"`([A-Za-z0-9_.][A-Za-z0-9_./-]*/[A-Za-z0-9_./-]*)`")
# Repository assets the base skill's wakeup reference names by path, which the
# installer therefore has to ship beside that skill.
COMPANION_PATHS = ("deploy/codex", "deploy/claude-code", "docs/ROLE_WAKEUPS.md")
# Directories holding material a skill tells an agent to open. A skill may also
# name a source path, but only as an example of a ticket's write scope, which is
# subject matter rather than something the installed agent has to be given.
ASSET_PREFIXES = ("deploy/", "docs/")
# Absolute local paths and credential material must never ship inside a skill.
FORBIDDEN_CONTENT = re.compile(
    r"(?:[A-Za-z]:\\Users\\|[A-Za-z]:\\HoH|/home/[a-z]|/Users/[a-z]|"
    r"(?:bearer|api[_-]?key|password)\s*[:=]\s*\S)",
    re.IGNORECASE,
)


def _skill_markdown() -> list[Path]:
    return sorted(SKILL_ROOT.rglob("*.md"))


def _registered_tool_names() -> set[str]:
    server = (REPOSITORY_ROOT / "src" / "hoh_control_plane" / "mcp_server.py").read_text(
        encoding="utf-8"
    )
    return set(re.findall(r'@server\.tool\(name="([^"]+)"', server))


def _installed_relative_paths(role_skill: str) -> set[str]:
    """The files install-agent-skill.ps1 copies for one role, relative to its target root."""
    paths: set[str] = set()
    for skill_name in (BASE_SKILL, role_skill):
        skill_directory = SKILL_ROOT / skill_name
        for path in skill_directory.rglob("*"):
            if path.is_file():
                paths.add(path.relative_to(SKILL_ROOT).as_posix())
    for companion in COMPANION_PATHS:
        source = REPOSITORY_ROOT / companion
        if source.is_dir():
            paths.update(
                f"{BASE_SKILL}/{companion}/{child.relative_to(source).as_posix()}"
                for child in source.rglob("*")
                if child.is_file()
            )
        else:
            paths.add(f"{BASE_SKILL}/{companion}")
    return paths


def test_every_required_skill_asset_is_committed() -> None:
    missing = [name for name in REQUIRED_FILES if not (SKILL_ROOT / name).is_file()]

    assert missing == []


@pytest.mark.parametrize("skill_name", (BASE_SKILL, *ROLE_SKILLS))
def test_each_skill_declares_matching_frontmatter(skill_name: str) -> None:
    text = (SKILL_ROOT / skill_name / "SKILL.md").read_text(encoding="utf-8")

    assert text.startswith("---\n")
    front_matter = text.split("---", 2)[1]
    assert f"name: {skill_name}" in front_matter
    description = re.search(r"^description: (.+)$", front_matter, re.MULTILINE)
    assert description is not None and description.group(1).strip()


def test_skill_references_resolve_inside_the_installed_tree() -> None:
    """A relative link must point at a file the install script also copies."""
    unresolved: list[str] = []
    for document in _skill_markdown():
        skill_name = document.relative_to(SKILL_ROOT).parts[0]
        installed = _installed_relative_paths(skill_name)
        for link in MARKDOWN_LINK.findall(document.read_text(encoding="utf-8")):
            target = link.split("#", 1)[0]
            if not target or target.startswith(("http://", "https://", "mailto:")):
                continue
            resolved = (document.parent / target).resolve()
            relative = resolved.relative_to(SKILL_ROOT.resolve()).as_posix()
            if not resolved.is_file() or relative not in installed:
                unresolved.append(f"{document.relative_to(REPOSITORY_ROOT)} -> {link}")

    assert unresolved == []


def test_backticked_repository_paths_are_shipped_with_the_skill() -> None:
    """A skill may name a repository path only when the installer also ships it.

    Markdown links are not the only way a skill sends an agent to a file: the
    wakeup reference names its templates as backticked paths. An installed agent
    has no repository, so every such path must arrive with the install.
    """
    unshipped: list[str] = []
    for document in _skill_markdown():
        skill_name = document.relative_to(SKILL_ROOT).parts[0]
        installed = _installed_relative_paths(
            skill_name if skill_name != BASE_SKILL else "hoh-developer"
        )
        for candidate in BACKTICKED_PATH.findall(document.read_text(encoding="utf-8")):
            relative = candidate.rstrip("/")
            if not relative.startswith(ASSET_PREFIXES):
                continue
            # A file the worker is told to create is not a repository asset.
            if not (REPOSITORY_ROOT / relative).exists():
                continue
            expected = f"{BASE_SKILL}/{relative}"
            if not any(
                path == expected or path.startswith(f"{expected}/") for path in installed
            ):
                unshipped.append(f"{document.relative_to(REPOSITORY_ROOT)} -> {candidate}")

    assert unshipped == []


@pytest.mark.parametrize("companion", COMPANION_PATHS)
def test_every_wakeup_asset_the_base_skill_names_exists(companion: str) -> None:
    assert (REPOSITORY_ROOT / companion).exists()
    assert companion.replace("/", "\\") in INSTALL_SCRIPT.read_text(encoding="utf-8")


def test_skills_only_name_registered_mcp_tools() -> None:
    """A skill must never instruct an agent to call a tool the server does not expose."""
    registered = _registered_tool_names()
    unavailable: dict[str, list[str]] = {}
    for document in _skill_markdown():
        named = set(TOOL_REFERENCE.findall(document.read_text(encoding="utf-8")))
        missing = sorted(name for name in named if name not in registered)
        if missing:
            unavailable[document.relative_to(REPOSITORY_ROOT).as_posix()] = missing

    assert registered
    assert unavailable == {}


def test_skills_do_not_promise_an_external_wake_capability() -> None:
    """The local pilot cannot wake a stopped chat; no skill may imply otherwise."""
    overclaims = re.compile(
        r"(?:will wake|wakes? (?:you|the agent|the chat) automatically|"
        r"automatically wakes|server (?:can|will) wake)",
        re.IGNORECASE,
    )
    offenders = [
        document.relative_to(REPOSITORY_ROOT).as_posix()
        for document in _skill_markdown()
        if overclaims.search(document.read_text(encoding="utf-8"))
    ]
    wakeup = (SKILL_ROOT / BASE_SKILL / "references" / "wakeup.md").read_text(encoding="utf-8")

    assert offenders == []
    assert "work.inbox" in wakeup


def test_skills_carry_no_local_paths_or_credentials() -> None:
    offenders = [
        document.relative_to(REPOSITORY_ROOT).as_posix()
        for document in _skill_markdown()
        if FORBIDDEN_CONTENT.search(document.read_text(encoding="utf-8"))
    ]

    assert offenders == []


def test_skills_stay_small_enough_to_load_on_demand() -> None:
    oversized = {
        document.relative_to(REPOSITORY_ROOT).as_posix(): len(
            document.read_text(encoding="utf-8")
        )
        for document in _skill_markdown()
        if len(document.read_text(encoding="utf-8")) > 12_000
    }

    assert oversized == {}


def test_the_install_script_covers_exactly_the_committed_roles() -> None:
    script = INSTALL_SCRIPT.read_text(encoding="utf-8")
    declared = set(
        re.search(r'ValidateSet\((("[a-z]+",?\s*)+)\)\]\s*\[string\]\$Role', script)
        .group(1)
        .replace('"', "")
        .replace(" ", "")
        .split(",")
    ) - {""}

    assert declared == {name.removeprefix("hoh-") for name in ROLE_SKILLS}
    assert 'Join-Path $repoRoot "skills"' in script
    # The base skill ships with every role, and the copy must be recursive so
    # each skill's references directory installs with it.
    assert '@("hoh-control-plane", "hoh-$Role")' in script
    assert "-Recurse" in script


@pytest.mark.parametrize("role_skill", ROLE_SKILLS)
def test_each_role_install_contains_the_expected_files(role_skill: str) -> None:
    installed = _installed_relative_paths(role_skill)

    assert f"{BASE_SKILL}/SKILL.md" in installed
    assert f"{BASE_SKILL}/references/wakeup.md" in installed
    assert f"{role_skill}/SKILL.md" in installed
    assert all(path.startswith((f"{BASE_SKILL}/", f"{role_skill}/")) for path in installed)
    if role_skill == "hoh-qa":
        assert {"hoh-qa/references/test-design.md", "hoh-qa/references/ui-testing.md"} <= installed


@pytest.mark.parametrize("role_skill", ROLE_SKILLS)
def test_project_scope_installation_copies_the_whole_tree(
    role_skill: str, tmp_path: Path, monkeypatch
) -> None:
    """Run the real installer when PowerShell is available on this host."""
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is unavailable on this host")
    staged_repository = tmp_path / "repo"
    (staged_repository / "scripts").mkdir(parents=True)
    shutil.copytree(SKILL_ROOT, staged_repository / "skills")
    shutil.copy(INSTALL_SCRIPT, staged_repository / "scripts" / INSTALL_SCRIPT.name)
    for companion in COMPANION_PATHS:
        source = REPOSITORY_ROOT / companion
        destination = staged_repository / companion
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            shutil.copy(source, destination)

    for target, client_directory in (("Codex", ".codex"), ("ClaudeCode", ".claude")):
        completed = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(staged_repository / "scripts" / INSTALL_SCRIPT.name),
                "-Target",
                target,
                "-Role",
                role_skill.removeprefix("hoh-"),
                "-Scope",
                "project",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert completed.returncode == 0, completed.stderr
        installed_root = staged_repository / client_directory / "skills"
        copied = {
            path.relative_to(installed_root).as_posix()
            for path in installed_root.rglob("*")
            if path.is_file()
        }
        assert copied == _installed_relative_paths(role_skill)
