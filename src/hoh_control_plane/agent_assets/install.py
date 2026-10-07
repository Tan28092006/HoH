"""Install this kit's skills and MCP config using Python's standard library."""
import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
import tomllib
from contextlib import contextmanager
from pathlib import Path


_LOCK_WAIT_SECONDS = 30
_REPLACE_ATTEMPTS = 5


def _read_config(config: Path) -> str:
    return config.read_text(encoding="utf-8") if config.exists() else ""


@contextmanager
def _config_lock(config: Path):
    """One installer at a time per config file; the OS releases it if we die.

    The lock file is empty and sits beside the config; it holds no settings.
    """
    lock_path = config.parent / f".{config.name}.hoh-install.lock"
    with lock_path.open("a+b") as stream:
        deadline = time.monotonic() + _LOCK_WAIT_SECONDS
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise ValueError(
                        f"Another HoH install is updating {config.name}; try again"
                    ) from None
                time.sleep(0.1)
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _replace_atomically(config: Path, text: str) -> None:
    """Write ``text`` to a private temp file beside ``config``, then rename it over.

    A reader sees the old file or the new one, never a half-written one, and
    the temp file is removed on any failure. No backup copy is kept: the
    config holds tokens, and a stray copy of it in the home directory is a
    credential leak that the atomic replace makes unnecessary.
    """
    descriptor, temporary = tempfile.mkstemp(dir=config.parent, prefix=f".{config.name}.hoh-tmp-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if config.exists():
            os.chmod(temporary, stat.S_IMODE(config.stat().st_mode))
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                os.replace(temporary, config)
                return
            except PermissionError:
                # Windows refuses the rename while another process has the file
                # open for a moment (the client itself saving its state).
                if attempt == _REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(0.2 * (attempt + 1))
    finally:
        Path(temporary).unlink(missing_ok=True)


def _write_config(config: Path, kit: Path, manifest: dict, client: str,
                  original_text: str, new_text: str) -> None:
    """Write the merged config under the lock, re-merging if it changed meanwhile.

    The client rewrites its own config while it runs. Writing back the text
    merged at the start would silently drop whatever the client saved in
    between, so the file is read again under the lock and the kit's server is
    merged into what is there now.
    """
    config.parent.mkdir(parents=True, exist_ok=True)
    with _config_lock(config):
        current = _read_config(config)
        if current != original_text:
            new_text = _merged_config(kit, manifest, client, current)
        if new_text != current:
            _replace_atomically(config, new_text)
        # Earlier installers left full copies of the config, tokens included.
        for stale in config.parent.glob(config.name + ".hoh-backup-*"):
            if stale.is_file():
                stale.unlink(missing_ok=True)


def _merged_config(kit: Path, manifest: dict, client: str, text: str) -> str:
    """The client config with this kit's MCP server merged in; raises on a conflict."""
    name = manifest["server_name"]
    if client == "codex":
        parsed = tomllib.loads(text)
        old = parsed.get("mcp_servers", {}).get(name)
        previous = manifest.get('upgrade_from', {}).get('codex')
        if previous is not None and old is not None and {**old, 'tool_timeout_sec': 86400} == previous:
            # Only an exact generated stdio profile is eligible. Preserve all
            # unrelated tables and settings, including their comments.
            block = re.compile(r'(?ms)^\[mcp_servers\.' + re.escape(name) + r'\][ \t]*\n.*?(?=^\[|\Z)')
            if len(block.findall(text)) != 1:
                raise ValueError('Cannot locate generated profile for transport upgrade')
            text = block.sub('', text)
            old = None
        if old is not None and 'tool_timeout_sec' not in old and {**old, 'tool_timeout_sec': 86400} == manifest['codex']:
            # Only the generated, otherwise identical profile receives this
            # additive timeout upgrade. Keep comments and other profiles intact.
            header = re.compile(r'(?m)^\[mcp_servers\.' + re.escape(name) + r'\][ \t]*$')
            if len(header.findall(text)) != 1:
                raise ValueError('Cannot safely locate the generated HoH profile to upgrade its timeout')
            text = header.sub(lambda match: match.group(0) + '\ntool_timeout_sec = 86400', text)
            old = {**old, 'tool_timeout_sec': 86400}
        if old is not None and old != manifest["codex"]:
            raise ValueError("Existing HoH MCP configuration differs; review before replacing it")
        new_text = text if old is not None else text.rstrip() + "\n\n" + (kit / "codex.toml").read_text(encoding="utf-8")
        profile = manifest.get("codex_profile")
        if profile is not None:
            # A model is pinned through a profile named after the server.
            existing = parsed.get("profiles", {}).get(profile["name"])
            if existing is None:
                new_text = (new_text.rstrip() + f'\n\n[profiles.{profile["name"]}]\n'
                            + f'model = {json.dumps(profile["model"])}\n')
            elif existing != {"model": profile["model"]}:
                raise ValueError("Existing Codex profile differs; review before replacing it")
    else:
        parsed = json.loads(text or "{}")
        supplied = json.loads((kit / "mcp.json").read_text(encoding="utf-8"))["mcpServers"][name]
        old = parsed.setdefault("mcpServers", {}).get(name)
        previous = manifest.get('upgrade_from', {}).get('claude-code')
        if previous is not None and old is not None:
            if {k: v for k, v in old.items() if k != 'timeout'} == {k: v for k, v in previous.items() if k != 'timeout'}:
                timeout = old.get('timeout', 86400000)
                if type(timeout) is int and timeout > 0:
                    supplied['timeout'] = max(supplied['timeout'], timeout)
                    old = None
        if old is not None and 'timeout' in supplied:
            required_timeout = supplied['timeout']
            old_timeout = old.get('timeout', required_timeout)
            if (type(old_timeout) is int and old_timeout > 0
                    and {key: value for key, value in old.items() if key != 'timeout'}
                    == {key: value for key, value in supplied.items() if key != 'timeout'}):
                supplied = {**supplied, 'timeout': max(required_timeout, old_timeout)}
                old = supplied
        if old is not None and old != supplied:
            raise ValueError("Existing HoH MCP configuration differs; review before replacing it")
        parsed["mcpServers"][name] = supplied
        new_text = json.dumps(parsed, indent=2) + "\n"
    return new_text


def install(kit: Path, home: Path, client: str) -> None:
    manifest = json.loads((kit / "manifest.json").read_text(encoding="utf-8"))
    for relative, expected in manifest["files"].items():
        source = (kit / relative).resolve()
        if not source.is_relative_to(kit.resolve()) or hashlib.sha256(source.read_bytes()).hexdigest() != expected:
            raise ValueError("Kit checksum validation failed")
    config = home / (".codex/config.toml" if client == "codex" else ".claude.json")
    skill_root = home / (".codex/skills" if client == "codex" else ".claude/skills")
    # Every conflict check runs before the first write, and the config is
    # written last, atomically, under a lock (see _write_config).
    original_text = _read_config(config)
    new_text = _merged_config(kit, manifest, client, original_text)
    agent = manifest.get("claude_agent")
    agent_destination = None
    if agent is not None:
        # A Claude Code worker subagent pins the model a role's tickets run on.
        if not agent.startswith("agents/") or not (kit / agent).resolve().is_relative_to(kit.resolve()):
            raise ValueError("Kit agent path is invalid")
        agent_destination = home / ".claude" / agent
        if agent_destination.exists() and agent_destination.read_bytes() != (kit / agent).read_bytes():
            raise ValueError(f"Existing agent differs: {agent_destination.name}; review before replacing it")
    sources = list((kit / "skills").iterdir())
    for source in sources:
        destination = skill_root / source.name
        if destination.exists():
            source_files = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
            dest_files = {p.relative_to(destination): p.read_bytes() for p in destination.rglob("*") if p.is_file()}
            if source_files != dest_files:
                raise ValueError(f"Existing skill differs: {source.name}; review before upgrading")
    # All conflicts checked before the first write.
    for source in sources:
        destination = skill_root / source.name
        if not destination.exists():
            shutil.copytree(source, destination)
    if agent_destination is not None and not agent_destination.exists():
        agent_destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(kit / agent, agent_destination)
    _write_config(config, kit, manifest, client, original_text, new_text)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", required=True, choices=["codex", "claude-code"])
    parser.add_argument("--home", type=Path, default=Path.home())
    args = parser.parse_args()
    install(Path(__file__).resolve().parent, args.home, args.client)
    print("Installed. Set the token environment variable and restart your client. See START_HERE.md.")
