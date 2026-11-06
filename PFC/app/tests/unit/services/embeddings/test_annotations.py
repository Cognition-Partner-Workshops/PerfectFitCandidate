"""Keep embedding tests and their integration helpers explicitly annotated."""

import ast
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parents[3]
FILES = sorted(Path(__file__).parent.glob("*.py")) + [
    TESTS / "unit/adapters/vector/test_stores.py",
    TESTS / "unit/api/test_packages.py",
    TESTS / "unit/services/packages/test_models_inbox.py",
]


@pytest.mark.parametrize("path", FILES, ids=[path.name for path in FILES])
def test_test_functions_and_helpers_are_annotated(path: Path) -> None:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        location = f"{path.name}:{node.lineno}: {node.name}"
        assert node.returns is not None, f"{location} needs a return annotation"
        arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        arguments.extend(arg for arg in (node.args.vararg, node.args.kwarg) if arg is not None)
        for argument in arguments:
            if argument.arg not in ("self", "cls"):
                assert argument.annotation is not None, (
                    f"{location} needs an annotation for {argument.arg}"
                )
