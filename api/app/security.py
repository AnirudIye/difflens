import base64
import hashlib
import hmac
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta

import structlog
from cryptography.fernet import Fernet
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Session as SessionRow

log = structlog.get_logger()

STATE_TTL_SECONDS = 600
SESSION_TTL_SECONDS = 60 * 60 * 24 * 7


def _hmac_hex(payload: str) -> str:
    key = settings.session_secret.encode()
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def sign_state(state: str) -> str:
    expires = int(time.time()) + STATE_TTL_SECONDS
    payload = f"{state}.{expires}"
    return f"{payload}.{_hmac_hex(payload)}"


def verify_state(signed: str, state: str) -> bool:
    parts = signed.split(".")
    if len(parts) != 3:
        return False
    value, expires, signature = parts
    if not hmac.compare_digest(signature, _hmac_hex(f"{value}.{expires}")):
        return False
    if not expires.isdigit() or int(expires) < time.time():
        return False
    return hmac.compare_digest(value, state)


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        if settings.token_encryption_key:
            # Hash the configured value into a Fernet key so any non-empty string works
            digest = hashlib.sha256(settings.token_encryption_key.encode()).digest()
            _fernet = Fernet(base64.urlsafe_b64encode(digest))
        else:
            log.warning(
                "token_encryption_key is empty, using an ephemeral key: "
                "encrypted tokens will not survive a restart"
            )
            _fernet = Fernet(Fernet.generate_key())
    return _fernet


def encrypt_token(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str) -> str:
    return _get_fernet().decrypt(ciphertext.encode()).decode()


def mint_session(db: Session, user_id: uuid.UUID) -> str:
    token = secrets.token_urlsafe(32)
    db.add(
        SessionRow(
            user_id=user_id,
            token_hash=hash_session_token(token),
            expires_at=datetime.now(UTC) + timedelta(seconds=SESSION_TTL_SECONDS),
        )
    )
    return token
