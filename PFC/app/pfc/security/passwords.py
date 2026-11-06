from importlib.resources import files

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError

from pfc.config import Settings
from pfc.security.problems import Problem


def hasher(settings: Settings) -> PasswordHasher:
    """REQ-AUTH-04: Argon2id RFC 9106 low-memory profile."""
    return PasswordHasher(
        memory_cost=settings.argon_memory_cost,
        time_cost=settings.argon_time_cost,
        parallelism=settings.argon_parallelism,
        type=Type.ID,
    )


def verify(encoded: str, password: str, settings: Settings) -> bool:
    try:
        return hasher(settings).verify(encoded, password)
    except (VerificationError, InvalidHashError):
        return False


def validate_password(password: str, email: str, role: str) -> None:
    """REQ-AUTH-03: role-specific length, complexity and common-password policy."""
    blocked = files("pfc.security").joinpath("common_passwords.txt").read_text().splitlines()
    valid = len(password) >= (14 if role == "Admin" else 10)
    valid = valid and password.casefold() not in blocked
    if role == "Admin":
        classes = sum(
            [
                any(c.islower() for c in password),
                any(c.isupper() for c in password),
                any(c.isdigit() for c in password),
                any(not c.isalnum() for c in password),
            ]
        )
        valid = valid and classes >= 3 and email.split("@")[0].casefold() not in password.casefold()
    if not valid:
        raise Problem(422, "PASSWORD_POLICY", "Password does not meet policy")
