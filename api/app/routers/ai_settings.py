"""Bring-your-own AI key: stored encrypted, surfaced only as a hint.

Reviews the user triggers run on their key instead of the server's
provider. The plaintext key exists in a response exactly never.
"""

from typing import Any, Literal

from cryptography.fernet import InvalidToken
from fastapi import APIRouter
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.deps import CurrentUser, DbSession
from app.models import UserAIKey
from app.security import decrypt_token, encrypt_token

router = APIRouter(prefix="/settings")


class PutAIKeyRequest(BaseModel):
    provider: Literal["anthropic", "gemini"]
    api_key: str = Field(min_length=10, max_length=512)
    model: str | None = Field(default=None, max_length=100)

    @field_validator("api_key", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("model")
    @classmethod
    def _empty_model_is_none(cls, value: str | None) -> str | None:
        value = value.strip() if value else None
        return value or None


def _status(row: UserAIKey | None) -> dict[str, Any]:
    if row is None:
        return {
            "configured": False,
            "provider": None,
            "model": None,
            "key_hint": None,
            "key_invalid": False,
        }
    try:
        hint = decrypt_token(row.key_enc)[-4:]
        key_invalid = False
    except InvalidToken:
        # Encryption key rotated since this was stored; the row is unusable
        # and the UI must ask for the key again rather than show it as live
        hint = None
        key_invalid = True
    return {
        "configured": True,
        "provider": row.provider,
        "model": row.model,
        "key_hint": hint,
        "key_invalid": key_invalid,
    }


def _load(db: DbSession, user_id) -> UserAIKey | None:
    return db.execute(select(UserAIKey).where(UserAIKey.user_id == user_id)).scalar_one_or_none()


@router.get("/ai-key")
def get_ai_key(user: CurrentUser, db: DbSession) -> dict[str, Any]:
    return _status(_load(db, user.id))


def _apply(row: UserAIKey, body: PutAIKeyRequest) -> None:
    row.provider = body.provider
    row.key_enc = encrypt_token(body.api_key)
    row.model = body.model


@router.put("/ai-key")
def put_ai_key(body: PutAIKeyRequest, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    row = _load(db, user.id)
    if row is None:
        row = UserAIKey(user_id=user.id, provider=body.provider, key_enc="")
        db.add(row)
    _apply(row, body)
    try:
        db.commit()
    except IntegrityError:
        # Lost a race with a concurrent first-time save: last write wins
        db.rollback()
        row = _load(db, user.id)
        if row is None:
            row = UserAIKey(user_id=user.id, provider=body.provider, key_enc="")
            db.add(row)
        _apply(row, body)
        db.commit()
    return _status(row)


@router.delete("/ai-key")
def delete_ai_key(user: CurrentUser, db: DbSession) -> dict[str, Any]:
    row = _load(db, user.id)
    if row is not None:
        db.delete(row)
        db.commit()
    return _status(None)
