"""Contraseñas, tokens y segundo factor."""

from __future__ import annotations

import hashlib
import hmac
import secrets

import bcrypt
import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from .config import Config, get_config

_argon2 = PasswordHasher()


# --------------------------------------------------------------------------- #
# Contraseñas
# --------------------------------------------------------------------------- #


def hash_password(password: str, config: Config | None = None) -> str:
    config = config or get_config()
    if config.security.password_hash == "bcrypt":
        return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    return _argon2.hash(password)


def verify_password(password: str, stored: str) -> bool:
    """Comprueba una contraseña contra cualquiera de los formatos soportados.

    El formato se deduce del propio hash, no de la configuración, para que
    cambiar ``security.password_hash`` no invalide las cuentas existentes.
    """
    if not stored:
        return False
    try:
        if stored.startswith("$argon2"):
            return _argon2.verify(stored, password)
        if stored.startswith("$2"):
            return bcrypt.checkpw(password.encode(), stored.encode())
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False
    return False


def needs_rehash(stored: str, config: Config | None = None) -> bool:
    """Indica si el hash guardado debería regenerarse al próximo login."""
    config = config or get_config()
    wanted = config.security.password_hash
    if wanted == "argon2":
        if not stored.startswith("$argon2"):
            return True
        try:
            return _argon2.check_needs_rehash(stored)
        except InvalidHashError:
            return True
    return not stored.startswith("$2")


def check_password_policy(
    password: str,
    username: str | None = None,
    email: str | None = None,
    config: Config | None = None,
) -> list[str]:
    """Devuelve la lista de incumplimientos; vacía si la contraseña vale."""
    policy = (config or get_config()).security.password_policy
    errors: list[str] = []

    if len(password) < policy.min_length:
        errors.append(f"Debe tener al menos {policy.min_length} caracteres.")
    if policy.require_uppercase and not any(c.isupper() for c in password):
        errors.append("Debe incluir al menos una mayúscula.")
    if policy.require_lowercase and not any(c.islower() for c in password):
        errors.append("Debe incluir al menos una minúscula.")
    if policy.require_digit and not any(c.isdigit() for c in password):
        errors.append("Debe incluir al menos un dígito.")
    if policy.require_symbol and password.isalnum():
        errors.append("Debe incluir al menos un símbolo.")

    if policy.reject_username_in_password:
        lowered = password.lower()
        if username and len(username) >= 3 and username.lower() in lowered:
            errors.append("No puede contener el nombre de usuario.")
        if email:
            local = email.split("@")[0]
            if len(local) >= 3 and local.lower() in lowered:
                errors.append("No puede contener la dirección de correo.")

    return errors


# --------------------------------------------------------------------------- #
# Tokens
# --------------------------------------------------------------------------- #


def generate_token(length: int = 32) -> str:
    """Token urlsafe de exactamente ``length`` caracteres (mínimo 16)."""
    length = max(16, length)
    # token_urlsafe devuelve ~1.33 caracteres por byte; pedimos de sobra.
    return secrets.token_urlsafe(length)[:length]


def hash_token(token: str) -> str:
    """Los tokens se guardan hasheados: una copia de la BD no da acceso."""
    return hashlib.sha256(token.encode()).hexdigest()


def tokens_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)


# --------------------------------------------------------------------------- #
# Segundo factor
# --------------------------------------------------------------------------- #


def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str, username: str, config: Config | None = None) -> str:
    config = config or get_config()
    return pyotp.TOTP(secret).provisioning_uri(
        name=username, issuer_name=config.security.totp.issuer
    )


def verify_totp(secret: str, code: str) -> bool:
    """Valida un código TOTP admitiendo un paso de desfase de reloj."""
    if not secret or not code:
        return False
    return pyotp.TOTP(secret).verify(code.strip().replace(" ", ""), valid_window=1)


def totp_required(role: str, config: Config | None = None) -> bool:
    config = config or get_config()
    totp = config.security.totp
    return totp.enabled and role in totp.required_for_roles
