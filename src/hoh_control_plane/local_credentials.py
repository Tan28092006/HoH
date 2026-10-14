"""Private, atomically updated credentials for local shared-host MCP profiles.

The file holds sha256(token), never the token (wave 8A, R4): reading it does not
let one agent present another's credential. The token itself goes once to the
operator's user environment variable that the generated client config names
(``bearer_token_env_var``), so it is in no file HoH or a client config holds.
A file written before 8A keeps working; its plaintext entries are re-keyed to
their digest the next time a profile for that identity is installed.
"""
import json
import os
from pathlib import Path
import re
import secrets
import tempfile

from hoh_control_plane.auth import credentials_from_environment, token_digest
from hoh_control_plane.resource_lock import exclusive_file_lock


def token_env_var(agent_id):
    """The environment variable a profile's client reads its bearer token from."""
    return 'HOH_MCP_TOKEN_' + re.sub(r'[^A-Z0-9]', '_', agent_id.upper())


def token_delivery_available():
    return os.name == 'nt'


def deliver_token(name, token):
    """Persist the token as the operator's user environment variable.

    Written through the registry rather than setx, so no command line, error
    message or log ever carries the value. New client processes see it.
    """
    if not token_delivery_available():
        raise RuntimeError('Token delivery to the user environment is implemented for Windows hosts only')
    import ctypes
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment', 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, token)
    # WM_SETTINGCHANGE to every top-level window, as setx does, so a client
    # started from Explorer afterwards inherits the variable.
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, 'Environment', 0x0002, 5000, None)
    os.environ[name] = token


def register_local_credential(root, actor, rotate=False, *, delivery=None, apply=None, recovery_token=None):
    """Issue or migrate the credential for ``actor``.

    Returns the plaintext token when the caller must deliver it: a new token, a
    rotated one, or a pre-8A plaintext entry being re-keyed to its digest.
    With delivery/apply callbacks, preflight the client before calling this
    function. Delivery and installation run under the credential lock before
    publication; failure leaves the previous server credential valid.
    Returns None for an existing digest; delivery then requires a recoverable
    token from a recognized legacy profile or the persistent user environment.
    """
    path = Path(root) / 'config/agent-credentials.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_file_lock(path.with_suffix('.lock')):
        raw = path.read_text(encoding='utf-8') if path.exists() else ''
        credentials = credentials_from_environment(raw)
        records = json.loads(raw) if raw else {}
        own = [credential for credential in credentials if credential.actor == actor]
        try:
            previous = recovery_token or (read_token(token_env_var(actor.agent_id)) if delivery and own else None)
        except Exception:
            raise RuntimeError('Credential recovery needed: user token variable could not be read') from None
        if not previous or not previous.isascii() or not any(c.digest == token_digest(previous) for c in own):
            previous = next((c.token for c in own if not c.hashed), None)
        token = None
        if own and not rotate:
            plaintext = [c for c in own if not c.hashed]
            if plaintext:
                token = plaintext[0].token
                for credential in plaintext:
                    records[credential.digest] = records.pop(credential.token)
            elif delivery and previous is None:
                raise RuntimeError('Credential recovery needed: restore the user token variable or explicitly rotate the token')
        else:
            for credential in own:
                records.pop(credential.token, None)
            token = secrets.token_urlsafe(48)
            records[token_digest(token)] = {'agent_id': actor.agent_id, 'role': actor.role.value,
                                            'project_ids': sorted(actor.project_ids)}
        # No credential is published/revoked until delivery and client installation
        # finish. Retain a usable previous token for rollback without a disk backup.
        temporary = None
        try:
            if delivery:
                delivery(token_env_var(actor.agent_id), token or previous)
            if apply:
                apply()
            if token is None:
                return None
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                             prefix='.credentials-', delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(records, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        except Exception:
            if delivery and previous:
                try:
                    delivery(token_env_var(actor.agent_id), previous)
                except Exception:
                    pass
            raise RuntimeError('Credential setup incomplete; previous credentials remain valid. Retry setup; explicit token recovery may be needed') from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return token


def read_token(name):
    """Read the persistent user environment, not a potentially stale parent copy."""
    if os.name == 'nt':
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
                value, kind = winreg.QueryValueEx(key, name)
                return value if kind == winreg.REG_SZ else None
        except FileNotFoundError:
            return None
    return os.environ.get(name)


def credential_provider(base, path):
    """Reload the private file on each request, including revocations; fail closed."""
    path = Path(path)
    def current():
        extra = credentials_from_environment(path.read_text(encoding='utf-8')) if path.exists() else ()
        # Reject ambiguous tokens, including collisions with the static map.
        # Keyed by digest, so a hashed entry and a plaintext one for the same
        # token are recognised as the same credential.
        merged = {credential.digest: credential for credential in base}
        for credential in extra:
            previous = merged.get(credential.digest)
            if previous is not None and previous.actor != credential.actor:
                raise ValueError('Conflicting local credential')
            merged[credential.digest] = credential
        return tuple(merged.values())
    return current
