import base64
import hashlib
import re

from cryptography.fernet import Fernet, InvalidToken


def _derive_fernet_key(secret: str) -> bytes:
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt_secret(plain: str, encryption_key: str) -> str:
    if not encryption_key:
        return plain
    f = Fernet(_derive_fernet_key(encryption_key))
    return f.encrypt(plain.encode("utf-8")).decode("utf-8")


def decrypt_secret(cipher: str, encryption_key: str) -> str:
    if not encryption_key:
        return cipher
    f = Fernet(_derive_fernet_key(encryption_key))
    try:
        return f.decrypt(cipher.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        return cipher


def mask_sensitive(text: str, patterns: list[str] | None = None) -> str:
    patterns = patterns or [r"(?i)(password|passwd|pwd)\s*=\s*\S+"]
    result = text
    for pat in patterns:
        result = re.sub(pat, r"\1=***", result)
    return result
