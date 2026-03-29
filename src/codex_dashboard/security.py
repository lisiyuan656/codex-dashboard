from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from .db import get_db
from .models import User


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1)
    return f"scrypt${salt.hex()}${derived.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    algorithm, salt_hex, digest_hex = encoded.split("$", 2)
    if algorithm != "scrypt":
        raise ValueError(f"Unsupported password format: {algorithm}")
    candidate = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=16384, r=8, p=1)
    return hmac.compare_digest(candidate.hex(), digest_hex)


def get_user_from_session(request: Request, db: Session) -> User | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.get(User, user_id)


def require_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = get_user_from_session(request, db)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Login required")
    return user


def get_websocket_user(scope: dict[str, Any], db: Session) -> User | None:
    session = scope.get("session") or {}
    user_id = session.get("user_id")
    if not user_id:
        return None
    return db.get(User, user_id)
