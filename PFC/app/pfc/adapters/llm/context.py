from typing import TypeGuard

ALLOWED_CONTEXT_SIZES = frozenset({4096, 8192, 16384})


def valid_context_size(value: object) -> TypeGuard[int]:
    return type(value) is int and value in ALLOWED_CONTEXT_SIZES
