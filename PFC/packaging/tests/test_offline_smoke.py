from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


@pytest.fixture
def assert_health_ok() -> Callable[[Any], None]:
    source = Path(__file__).resolve().parents[1] / "offline_smoke.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    helper = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "assert_health_ok"
    )
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "assert_health_ok"
        for node in ast.walk(tree)
    )
    namespace: dict[str, Any] = {}
    exec(compile(ast.Module(body=[helper], type_ignores=[]), str(source), "exec"), namespace)
    return namespace["assert_health_ok"]


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "ok"},
        {"status": "ok", "details": {"dataVolumeEncryption": "Unknown"}},
        {"status": "ok", "details": {"dataVolumeEncryption": "Off"}, "future": True},
    ],
)
def test_health_accepts_additive_fields(
    assert_health_ok: Callable[[Any], None], payload: dict[str, Any]
) -> None:
    assert_health_ok(SimpleNamespace(status_code=200, json=lambda: payload))


@pytest.mark.parametrize(
    "payload", [{}, {"details": {}}, {"status": "degraded"}, {"status": None}, None, []]
)
def test_health_requires_ok_status(
    assert_health_ok: Callable[[Any], None], payload: dict[str, Any] | list[Any] | None
) -> None:
    with pytest.raises(AssertionError):
        assert_health_ok(SimpleNamespace(status_code=200, json=lambda: payload))


def test_health_requires_http_200(assert_health_ok: Callable[[Any], None]) -> None:
    with pytest.raises(AssertionError):
        assert_health_ok(SimpleNamespace(status_code=503, json=lambda: {"status": "ok"}))
