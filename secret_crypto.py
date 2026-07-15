import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

import config

PREFIX = "enc:v1:"


class SecretDecryptionError(RuntimeError):
    pass


def _fernet() -> Fernet:
    if len(config.TOKEN_ENCRYPTION_KEY) < 32:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY must be at least 32 characters")
    digest = hashlib.sha256(config.TOKEN_ENCRYPTION_KEY.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def is_encrypted(value: str) -> bool:
    return value.startswith(PREFIX)


def encrypt(value: str) -> str:
    if not value:
        return value
    if is_encrypted(value):
        return value
    token = _fernet().encrypt(value.encode("utf-8")).decode("ascii")
    return PREFIX + token


def decrypt(value: str) -> str:
    if not value:
        return value
    if not is_encrypted(value):
        return value
    try:
        return _fernet().decrypt(value[len(PREFIX) :].encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
        raise SecretDecryptionError("Stored secret cannot be decrypted") from exc
