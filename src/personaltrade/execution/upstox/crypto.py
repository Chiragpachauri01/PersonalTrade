"""Fernet encryption for the Upstox access token at rest (CLAUDE.md Rule 15,
docs/architecture/06-config-security-ops.md "Upstox access token (rotates
daily) stored encrypted at rest (Fernet, key from .env)"). The plaintext
token exists only in memory and in the process's own TLS connections to
Upstox — never on disk unencrypted.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from personaltrade.core.config import Secrets
from personaltrade.core.errors import ConfigError, PersonalTradeError


class TokenDecryptionFailed(PersonalTradeError):
    """The stored ciphertext didn't decrypt with the configured key — wrong/
    rotated PT_TOKEN_ENCRYPTION_KEY, or corrupted data. Never silently
    treated as "no token" (that would mask a real problem as a routine
    "please log in again")."""


def _fernet(secrets: Secrets) -> Fernet:
    key = secrets.pt_token_encryption_key
    if key is None:
        raise ConfigError(
            "PT_TOKEN_ENCRYPTION_KEY is not set — generate one with "
            "`uv run python -c \"from cryptography.fernet import Fernet; "
            'print(Fernet.generate_key().decode())"` and add it to .env'
        )
    return Fernet(key.get_secret_value().encode())


def encrypt_token(secrets: Secrets, access_token: str) -> str:
    return _fernet(secrets).encrypt(access_token.encode()).decode()


def decrypt_token(secrets: Secrets, encrypted_access_token: str) -> str:
    try:
        return _fernet(secrets).decrypt(encrypted_access_token.encode()).decode()
    except InvalidToken as exc:
        raise TokenDecryptionFailed(
            "stored Upstox token could not be decrypted — PT_TOKEN_ENCRYPTION_KEY "
            "may have changed; run `pt auth upstox-login` again"
        ) from exc
