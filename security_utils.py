import os
import re

import bcrypt

try:
    from cryptography.fernet import Fernet, InvalidToken
except Exception:  # pragma: no cover - optional dependency scaffold
    Fernet = None
    InvalidToken = Exception

ENCRYPTION_PREFIX = "__ENC__"
PROTECTED_FIELD_LABELS = [
    "medication notes",
    "lifestyle notes",
    "cycle metadata and symptoms",
    "appointment notes",
    "appointment prescriptions",
]


def load_local_env():
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_local_env()


def _field_encryption_key():
    return os.getenv("FIELD_ENCRYPTION_KEY", "").strip()


def _build_cipher():
    if Fernet is None:
        return None
    key = _field_encryption_key()
    if not key:
        return None
    try:
        return Fernet(key.encode("utf-8"))
    except (TypeError, ValueError):
        return None


def encryption_available():
    return _build_cipher() is not None


def field_encryption_status():
    key_configured = bool(_field_encryption_key())
    key_valid = encryption_available()
    return {
        "enabled": key_valid,
        "key_configured": key_configured,
        "key_valid": key_valid,
        "algorithm": "Fernet symmetric encryption" if key_valid else "",
        "protected_fields": PROTECTED_FIELD_LABELS,
        "message": (
            "Sensitive free-text health fields are encrypted before database storage."
            if key_valid
            else "FIELD_ENCRYPTION_KEY is missing or invalid, so sensitive free-text fields cannot be encrypted."
        ),
    }


def encrypt_text(value):
    if not value:
        return value or ""
    if not isinstance(value, str):
        value = str(value)
    if value.startswith(ENCRYPTION_PREFIX):
        return value
    cipher = _build_cipher()
    if not cipher:
        return value
    token = cipher.encrypt(value.encode("utf-8")).decode("utf-8")
    return ENCRYPTION_PREFIX + token


def decrypt_text(value):
    if not value:
        return value or ""
    if not isinstance(value, str):
        return ""
    if not value.startswith(ENCRYPTION_PREFIX):
        return value
    cipher = _build_cipher()
    if not cipher:
        # Do not leak encrypted tokens into the UI when a deploy is missing the field key.
        return ""
    token = value.replace(ENCRYPTION_PREFIX, "", 1).encode("utf-8")
    try:
        return cipher.decrypt(token).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        return ""


def generate_fernet_key():
    if Fernet is None:
        return None
    return Fernet.generate_key().decode("utf-8")


def hash_password(password):
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password, password_hash):
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (AttributeError, TypeError, ValueError):
        return False


def validate_strong_password(password):
    # The project keeps password validation simple and explainable for a capstone:
    # the password must be long enough and include mixed character types.
    password = password or ""
    errors = []
    if len(password) < 8:
        errors.append("Password must be at least 8 characters.")
    if not re.search(r"[A-Z]", password):
        errors.append("Password must include at least 1 uppercase letter.")
    if not re.search(r"[a-z]", password):
        errors.append("Password must include at least 1 lowercase letter.")
    if not re.search(r"\d", password):
        errors.append("Password must include at least 1 number.")
    if not re.search(r"[^A-Za-z0-9]", password):
        errors.append("Password must include at least 1 special character.")
    return errors
