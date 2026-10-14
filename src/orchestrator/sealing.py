"""Text sealed at rest (ADR 0023): what patients write in WhatsApp, Instagram and
Facebook is stored only encrypted, so a copy of the database (a backup, the provider's
staff, a leaked dump) shows no message.

* AES-256-GCM: encryption and integrity in one; a changed byte fails to open.
* The key is derived from PSEUDONYM_KEY with HKDF-SHA256 and a purpose label, so no new
  secret has to be created and the pseudonym key itself never encrypts anything. Like
  PSEUDONYM_KEY, it lives outside the database and outside its backups.
* The tenant is the associated data: a sealed text moved to another practice's row does
  not open.
* Sealed values carry a version prefix (`v1:`), so the key or algorithm can change later
  without guessing which rows are which."""

from __future__ import annotations

import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

VERSION = "v1"
NONCE_BYTES = 12


class SealError(ValueError):
    """A sealed value that cannot be opened: wrong key, wrong tenant or altered."""


class Sealer:
    def __init__(self, secret: bytes, purpose: bytes = b"inbox-messages-v1") -> None:
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=purpose).derive(secret)
        self._aead = AESGCM(key)

    def seal(self, tenant: str, text: str) -> str:
        nonce = os.urandom(NONCE_BYTES)
        sealed = self._aead.encrypt(nonce, text.encode("utf-8"), tenant.encode("utf-8"))
        return f"{VERSION}:" + base64.b64encode(nonce + sealed).decode("ascii")

    def open(self, tenant: str, value: str) -> str:
        version, _, payload = value.partition(":")
        if version != VERSION or not payload:
            raise SealError("not a sealed value")
        try:
            raw = base64.b64decode(payload, validate=True)
            plain = self._aead.decrypt(raw[:NONCE_BYTES], raw[NONCE_BYTES:], tenant.encode())
        except (ValueError, InvalidTag) as exc:
            raise SealError("the value does not open with this key and tenant") from exc
        return plain.decode("utf-8")
