"""Install this kit's skills and MCP config using Python's standard library."""
import argparse
import hashlib
import json
import re
import shutil
import tempfile
import tomllib
from pathlib import Path


def install(kit: Path, home: Path, client: str) -> None:
    manifest = json.loads((kit / "manifest.json").read_text(encoding="utf-8"))
    for relative, expected in manifest["files"].items():
        source = (kit / relative).resolve()
        if not source.is_relative_to(kit.resolve()) or hashlib.sha256(source.read_bytes()).hexdigest() != expected:
            raise ValueError("Kit checksum validation failed")
    config = home / (".codex/config.toml" if client == "codex" else ".claude.json")
    skill_root = home / (".codex/skills" if client == "codex" else ".claude/skills")
    text = config.read_text(encoding="utf-8") if config.exists() else ""
    original_text = text
    name = manifest["server_name"]
    if client == "codex":
        parsed = tomllib.loads(text)
        old = parsed.get("mcp_servers", {}).get(name)
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
    else:
        parsed = json.loads(text or "{}")
        supplied = json.loads((kit / "mcp.json").read_text(encoding="utf-8"))["mcpServers"][name]
        old = parsed.setdefault("mcpServers", {}).get(name)
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
    config.parent.mkdir(parents=True, exist_ok=True)
    if new_text != original_text:
        if config.exists():
            with tempfile.NamedTemporaryFile(dir=config.parent, prefix=config.name + ".hoh-backup-", delete=False) as stream:
                stream.write(config.read_bytes())
        config.write_text(new_text, encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", required=True, choices=["codex", "claude-code"])
    parser.add_argument("--home", type=Path, default=Path.home())
    args = parser.parse_args()
    install(Path(__file__).resolve().parent, args.home, args.client)
    print("Installed. Set the token environment variable and restart your client. See START_HERE.md.")
