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
    except (KeyringError, OSError):
        return None


def set_password(username: str, password: str) -> None:
    """Store the password and read it back; raise KeyringError if the store didn't keep it."""
    keyring.set_password(SERVICE, username, password)
    if keyring.get_password(SERVICE, username) != password:
        raise KeyringError(f'хранилище {store_name()} не сохранило пароль')


def store_name() -> str:
    """Human name of the credential store in use."""
    backend = type(keyring.get_keyring()).__module__
    if 'Windows' in backend:
        return '«Диспетчер учётных данных» Windows'
    if 'macOS' in backend or 'OS_X' in backend:
        return '«Связка ключей» macOS'
    return backend
