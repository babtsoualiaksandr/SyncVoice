"""PBX password storage in the OS credential store.

Windows: Credential Manager, macOS: Keychain (via the `keyring` package).
Keeps the password out of the database, settings files and git.
"""
import keyring
from keyring.errors import KeyringError

SERVICE = 'SyncVoice PBX'


def get_password(username: str) -> str | None:
    if not username:
        return None
    try:
        return keyring.get_password(SERVICE, username)
    except KeyringError:
        return None


def set_password(username: str, password: str) -> None:
    keyring.set_password(SERVICE, username, password)
