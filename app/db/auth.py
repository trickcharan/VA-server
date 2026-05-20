"""
Authentication helpers: password hashing and session cookie management.
"""

import os
import secrets
import bcrypt
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

# Secret key for signing session cookies
SECRET_KEY = os.environ.get("SECRET_KEY", secrets.token_hex(32))
SESSION_MAX_AGE = 60 * 60 * 24  # 24 hours

_serializer = URLSafeTimedSerializer(SECRET_KEY)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))


def create_session_token(user_id: int) -> str:
    """Create a signed session token containing the user ID."""
    return _serializer.dumps({"uid": user_id})


def decode_session_token(token: str) -> dict | None:
    """Decode and verify a session token. Returns payload or None."""
    try:
        return _serializer.loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
