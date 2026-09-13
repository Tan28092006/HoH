"""Private, atomically updated credentials for local shared-host MCP profiles."""
import json
import os
from pathlib import Path
import secrets
import tempfile

from hoh_control_plane.auth import credentials_from_environment
from hoh_control_plane.resource_lock import exclusive_file_lock


def register_local_credential(root, actor):
    path = Path(root) / 'config/agent-credentials.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_file_lock(path.with_suffix('.lock')):
        raw = path.read_text(encoding='utf-8') if path.exists() else ''
        credentials = credentials_from_environment(raw)
        for credential in credentials:
            if credential.actor == actor:
                return credential.token
        records = json.loads(raw) if raw else {}
        token = secrets.token_urlsafe(48)
        records[token] = {'agent_id': actor.agent_id, 'role': actor.role.value,
                          'project_ids': sorted(actor.project_ids)}
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                             prefix='.credentials-', delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(records, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return token


def credential_provider(base, path):
    """Reload the private file on each request, including revocations; fail closed."""
    path = Path(path)
    def current():
        extra = credentials_from_environment(path.read_text(encoding='utf-8')) if path.exists() else ()
        # Reject ambiguous tokens, including collisions with the static map.
        merged = {credential.token: credential for credential in base}
        for credential in extra:
            previous = merged.get(credential.token)
            if previous is not None and previous.actor != credential.actor:
                raise ValueError('Conflicting local credential')
            merged[credential.token] = credential
        return tuple(merged.values())
    return current
