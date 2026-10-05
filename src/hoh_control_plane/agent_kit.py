"""Build a portable skill + authenticated HTTP MCP kit, without host credentials."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path
from urllib.parse import urlsplit
from zipfile import ZIP_DEFLATED, ZipFile

ROLES = ("planner", "developer", "qa", "integrator")


def build_kit(output: Path, role: str, server_url: str, token_env: str = "HOH_AGENT_TOKEN") -> Path:
    if role not in ROLES:
        raise ValueError("Choose planner, developer, qa or integrator")
    url = urlsplit(server_url)
    if (not url.hostname or url.username or url.password or url.query or url.fragment
            or any(c.isspace() for c in server_url)
            or (url.scheme != "https" and not (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}))):
        raise ValueError("Use an HTTPS MCP URL without credentials/query/fragment, or loopback HTTP")
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", token_env):
        raise ValueError("token_env must be an uppercase environment variable name")
    output = output.resolve()
    if output.exists() or output.with_suffix(".zip").exists():
        raise FileExistsError("Choose a new output directory; existing kits are preserved")
    assets = Path(__file__).parent / "agent_assets"
    source = assets / "skills"
    if not source.is_dir():
        source = Path(__file__).resolve().parents[2] / "skills"
    names = ["hoh-control-plane", f"hoh-{role}"]
    for name in names:
        if not (source / name / "SKILL.md").is_file():
            raise FileNotFoundError(f"Packaged skill is missing: {name}")
    output.mkdir(parents=True)
    for name in names:
        shutil.copytree(source / name, output / "skills" / name)
    server_name = f"hoh-{role}"
    codex = {"url": server_url, "bearer_token_env_var": token_env, "tool_timeout_sec": 86400}
    (output / "codex.toml").write_text(
        f'[mcp_servers.{server_name}]\nurl = {json.dumps(server_url)}\n'
        f'bearer_token_env_var = {json.dumps(token_env)}\ntool_timeout_sec = 86400\n', encoding="utf-8"
    )
    claude = {"mcpServers": {server_name: {
        "type": "http", "url": server_url, "timeout": 86400000,
        "headers": {"Authorization": "Bearer ${" + token_env + "}"},
    }}}
    (output / "mcp.json").write_text(json.dumps(claude, indent=2) + "\n", encoding="utf-8")
    shutil.copyfile(assets / "install.py", output / "install.py")
    start = f"""# Start or resume HoH work

Requires Python 3.12+ to run this installer. The MCP service runs on the operator's
host; the client needs no HoH Python package or database access for HTTP MCP.

1. Run `python install.py --client codex` or `python install.py --client claude-code`.
   An optional `--home <client-home>` selects a different home directory.
   Existing unrelated configuration is preserved. Conflicting installations fail
   before writing; review and remove the old HoH entries explicitly to upgrade.
2. Supply your operator-issued credential in `{token_env}` in the environment of
   the client process, then restart the client. Do not paste tokens into chat.
   This server uses bearer auth; it does not provide an OAuth login flow.
3. Start any capable agent session with: "Use $hoh-control-plane and $hoh-{role}.
   Call session.bootstrap and continue my authorized project work. Resume any
   current lease/job before discovering or claiming another ticket."
   For Developer, QA and Integrator the session is a dispatcher: after each
   claim, pass the claim's `worker_brief` verbatim to a fresh subagent (Claude
   Code Agent/Task tool, Codex `spawn_agent`) and keep only its structured
   result. Without subagents, start a new session per ticket.

The authenticated role must match `{role}`. Each concurrently running worker
needs a distinct identity. Reuse an identity to resume only after its previous
session has stopped. Follow the returned workspace model: colocated requires
access to the server filesystem; agent_workspace needs Git access and a local
checkout. Auth does not grant shell, filesystem or browser capabilities.
For several projects, select the intended project in session.bootstrap.
Wait with work.wait_for_event as its tool description says; resume with
resume_wait_id. A stopped chat cannot be awakened by ordinary MCP.
For clients other than Codex/Claude Code, install the skill folders and configure
an HTTP MCP connection using the URL and token environment variable in this kit,
with a tool timeout longer than the wait the tool description states.
"""
    (output / "START_HERE.md").write_text(start, encoding="utf-8")
    checksums = {p.relative_to(output).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted(output.rglob("*")) if p.is_file()}
    manifest = {"format_version": 1, "protocol_version": "0.3.0", "role": role,
                "server_name": server_name, "codex": codex, "files": checksums}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    archive = output.with_suffix(".zip")
    with ZipFile(archive, "w", compression=ZIP_DEFLATED) as bundle:
        for path in sorted(output.rglob("*")):
            if path.is_file():
                bundle.write(path, path.relative_to(output))
    return archive


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--role", required=True, choices=ROLES)
    parser.add_argument("--server-url", required=True)
    parser.add_argument("--token-env", default="HOH_AGENT_TOKEN")
    args = parser.parse_args()
    print(build_kit(args.output, args.role, args.server_url, args.token_env))


if __name__ == "__main__":
    main()
