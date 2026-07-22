"""execution/upstox/crypto.py: Fernet round-trip for the stored Upstox
access token (ROADMAP M17, ADR-027) — plaintext never touches the DB.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from personaltrade.core.config import Secrets
from personaltrade.core.errors import ConfigError
from personaltrade.execution.upstox.crypto import (
    TokenDecryptionFailed,
    decrypt_token,
    encrypt_token,
)


def _secrets(key: bytes | None) -> Secrets:
    return Secrets(_env_file=None, pt_token_encryption_key=key.decode() if key else None)


class TestEncryptDecryptRoundTrip:
    def test_round_trips(self) -> None:
        key = Fernet.generate_key()
        secrets = _secrets(key)
        encrypted = encrypt_token(secrets, "super-secret-access-token")
        assert encrypted != "super-secret-access-token"
        assert decrypt_token(secrets, encrypted) == "super-secret-access-token"

    def test_missing_key_raises_config_error_on_encrypt(self) -> None:
        with pytest.raises(ConfigError):
            encrypt_token(_secrets(None), "token")

    def test_missing_key_raises_config_error_on_decrypt(self) -> None:
        with pytest.raises(ConfigError):
            decrypt_token(_secrets(None), "whatever")

    def test_wrong_key_raises_token_decryption_failed(self) -> None:
        encrypted = encrypt_token(_secrets(Fernet.generate_key()), "token")
        with pytest.raises(TokenDecryptionFailed):
            decrypt_token(_secrets(Fernet.generate_key()), encrypted)

    def test_corrupted_ciphertext_raises_token_decryption_failed(self) -> None:
        key = Fernet.generate_key()
        with pytest.raises(TokenDecryptionFailed):
            decrypt_token(_secrets(key), "not-valid-fernet-ciphertext")
