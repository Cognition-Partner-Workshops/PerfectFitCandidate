import os
import re
from pathlib import Path

import pytest

from pfc.api.schemas.match import JdRequirements, RunView, WireModel
from pfc.api.schemas.results import ResultDetail, ResultRow
from pfc.config import Settings
from pfc.main import create_app

Route = tuple[str, str]

API_PREFIX = "/api/v1"
REPO_ROOT = Path(__file__).resolve().parents[4]
FRONTEND_OVERRIDE = os.environ.get("PFC_FRONTEND_DIR")
if FRONTEND_OVERRIDE and (Path(FRONTEND_OVERRIDE) / "src" / "api").is_dir():
    FRONTEND_ROOT = Path(FRONTEND_OVERRIDE)
else:
    FRONTEND_ROOT = REPO_ROOT / "PFC" / "frontend"

if not FRONTEND_ROOT.is_dir():
    pytest.skip("PFC/frontend is absent", allow_module_level=True)

REQUEST_PATTERN = re.compile(
    r"\bthis\.request(?:\s*<[^>]*>)?\s*\(\s*['\"]"
    r"(?P<method>GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)['\"]\s*,\s*"
    r"(?P<path>`[^`]*`|'[^']*'|\"[^\"]*\")",
    re.DOTALL,
)
UPLOAD_PATTERN = re.compile(
    r"\bthis\.upload\s*\(\s*(?P<path>`[^`]*`|'[^']*'|\"[^\"]*\")",
    re.DOTALL,
)
CONDITIONAL_PATH_PATTERN = re.compile(
    r"\$\{[^{}]*?\?\s*['\"](?P<first>/[^'\"]+)['\"]\s*:\s*['\"]"
    r"(?P<second>/[^'\"]+)['\"]\s*\}"
)
ALLOWED_PENDING_LABELS = (
    "W2-1 ingestion",
    "W2-2 matching #10",
    "W2-4b settings package",
    "W2-5 analytics/export",
    "LLM supervisor",
)

PENDING: dict[Route, str] = {}


def _frontend_paths(path_literal: str) -> set[str]:
    path = path_literal[1:-1]
    path = re.sub(r"\$\{this\.qs\([^}]*\)\}", "", path)
    paths = [path]
    while True:
        expanded_paths: list[str] = []
        did_expand = False
        for candidate in paths:
            conditional = CONDITIONAL_PATH_PATTERN.search(candidate)
            if conditional is None:
                expanded_paths.append(candidate)
                continue
            did_expand = True
            for branch in ("first", "second"):
                expanded_paths.append(
                    f"{candidate[: conditional.start()]}{conditional.group(branch)}"
                    f"{candidate[conditional.end() :]}"
                )
        paths = expanded_paths
        if not did_expand:
            break

    normalized_paths = set()
    for candidate in paths:
        normalized_path = re.sub(r"\$\{[^}]+\}", "{}", candidate).split("?", 1)[0]
        normalized_paths.add(f"{API_PREFIX}{normalized_path}")
    return normalized_paths


def _is_mock_or_test(source_path: Path, api_directory: Path) -> bool:
    relative_path = source_path.relative_to(api_directory)
    excluded_directories = {"mock", "mocks", "test", "tests", "__tests__"}
    return (
        bool(excluded_directories.intersection(relative_path.parts[:-1]))
        or source_path.name.startswith("test_")
        or source_path.name.endswith((".test.ts", ".spec.ts"))
    )


def _frontend_calls(frontend_root: Path) -> set[Route]:
    api_directory = frontend_root / "src" / "api"
    calls: set[Route] = set()
    for source_path in api_directory.rglob("*.ts"):
        if _is_mock_or_test(source_path, api_directory):
            continue
        source = source_path.read_text(encoding="utf-8")
        calls.update(
            (match.group("method"), path)
            for match in REQUEST_PATTERN.finditer(source)
            for path in _frontend_paths(match.group("path"))
        )
        calls.update(
            ("POST", path)
            for match in UPLOAD_PATTERN.finditer(source)
            for path in _frontend_paths(match.group("path"))
        )
    return calls


def _openapi_routes(document: dict[str, object]) -> set[Route]:
    paths = document["paths"]
    assert isinstance(paths, dict)
    routes: set[Route] = set()
    for path, operations in paths.items():
        assert isinstance(path, str)
        assert isinstance(operations, dict)
        normalized_path = re.sub(r"\{[^{}]+\}", "{}", path)
        routes.update(
            (method.upper(), normalized_path)
            for method in operations
            if method.lower() in {"get", "post", "put", "patch", "delete", "head", "options"}
        )
    return routes


def test_frontend_api_calls_match_backend_openapi(tmp_path: Path) -> None:
    frontend_calls = _frontend_calls(FRONTEND_ROOT)
    assert frontend_calls, f"No HTTP calls found under {FRONTEND_ROOT / 'src' / 'api'}"

    app = create_app(
        Settings(
            data_dir=tmp_path / "data",
            frontend_dir=tmp_path / "frontend",
        )
    )
    backend_routes = _openapi_routes(app.openapi())

    unlisted_calls = frontend_calls - backend_routes - PENDING.keys()
    assert not unlisted_calls, (
        f"Frontend calls missing from OpenAPI and PENDING: {sorted(unlisted_calls)}"
    )

    pending_now_served = PENDING.keys() & backend_routes
    assert not pending_now_served, (
        f"Remove newly served routes from PENDING: {sorted(pending_now_served)}"
    )

    unused_pending = PENDING.keys() - frontend_calls
    assert not unused_pending, (
        f"PENDING entries no longer called by the frontend: {sorted(unused_pending)}"
    )

    invalid_labels = {
        route: label
        for route, label in PENDING.items()
        if not label.startswith(ALLOWED_PENDING_LABELS) and not label.startswith("MISMATCH")
    }
    assert not invalid_labels, f"Unrecognized PENDING labels: {invalid_labels}"


def test_jd_extraction_metadata_contract(tmp_path: Path) -> None:
    schema = create_app(Settings(data_dir=tmp_path)).openapi()["components"]["schemas"][
        "JdRequirements"
    ]
    properties = schema["properties"]
    assert properties["extractionMethod"]["anyOf"][0]["enum"] == ["llm", "rule_based_fallback"]
    assert properties["fallbackReason"]["anyOf"][0]["type"] == "string"
    assert properties["warnings"]["items"]["type"] == "string"
    assert properties["items"]["items"]["$ref"] == "#/components/schemas/JdItemSource"
    assert JdRequirements(demand_id=1).extraction_method is None


@pytest.mark.parametrize("model", [RunView, ResultRow, ResultDetail])
def test_match_run_change_metadata_contract(model: type[WireModel]) -> None:
    schema = model.model_json_schema(mode="validation", by_alias=True)
    assert schema["properties"]["dataChangedSinceRun"]["type"] == "boolean"
    reasons = schema["properties"]["dataChangedReasons"]
    assert reasons["type"] == "array" and reasons["items"]["type"] == "string"
    assert {"dataChangedSinceRun", "dataChangedReasons"} <= set(schema["required"])
