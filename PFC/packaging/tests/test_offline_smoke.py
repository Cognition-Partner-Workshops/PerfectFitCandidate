from __future__ import annotations

import ast
import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


@pytest.mark.parametrize("case", ["valid", "empty", "missing", "bad_hash"])
def test_bundled_models_skip_optional(tmp_path: Path, case: str) -> None:
    source = Path(__file__).resolve().parents[1] / "offline_smoke.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    helper = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "verify_bundled_models"
    )
    namespace: dict[str, Any] = {
        "Path": Path,
        "Any": Any,
        "json": json,
        "hashlib": hashlib,
    }
    exec(compile(ast.Module(body=[helper], type_ignores=[]), str(source), "exec"), namespace)
    models = [{"file": "thinking.gguf", "optional": True, "sha256": "not downloaded"}]
    if case != "empty":
        models.append({"file": "instruct.gguf", "sha256": hashlib.sha256(b"model").hexdigest()})
        (tmp_path / "models").mkdir()
        if case != "missing":
            (tmp_path / "models" / "instruct.gguf").write_bytes(
                b"corrupted" if case == "bad_hash" else b"model"
            )
    (tmp_path / "MODELS.lock").write_text(json.dumps(models))
    verify = namespace["verify_bundled_models"]
    if case in {"valid", "empty"}:
        assert verify(tmp_path) == models[1:]
    else:
        with pytest.raises(FileNotFoundError if case == "missing" else AssertionError):
            verify(tmp_path)


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


@pytest.mark.parametrize("text,flags", [("Tiny CV", []), ("", []), ("", ["timeout"])])
def test_legacy_conversion_requires_expected_text_and_no_flags(tmp_path, text, flags) -> None:
    source = Path(__file__).resolve().parents[1] / "offline_smoke.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    helper = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "smoke_cv_conversion"
    )
    bundled = tmp_path / "libreoffice" / "program" / "soffice.exe"
    bundled.parent.mkdir(parents=True)
    bundled.touch()
    fixtures = tmp_path / "smoke-fixtures" / "cv-formats"
    fixtures.mkdir(parents=True)
    for extension in ("doc", "ppt"):
        (fixtures / f"tiny.{extension}").write_bytes(b"fixture")
    calls = []

    def extract(path, reader, timeout, **kwargs):
        calls.append(path.suffix)
        kwargs["work_dir"].mkdir(parents=True, exist_ok=True)
        return text, flags

    namespace = {
        "Path": Path,
        "Settings": Any,
        "os": os,
        "find_soffice": lambda: bundled,
        "bounded_extract": extract,
        "extract_text": object(),
    }
    exec(compile(ast.Module(body=[helper], type_ignores=[]), str(source), "exec"), namespace)
    if text == "Tiny CV" and not flags:
        assert namespace["smoke_cv_conversion"](tmp_path, SimpleNamespace(data_dir=tmp_path)) == [
            "doc",
            "ppt",
        ]
        assert calls == [".doc", ".ppt"]
    else:
        with pytest.raises(AssertionError):
            namespace["smoke_cv_conversion"](tmp_path, SimpleNamespace(data_dir=tmp_path))
