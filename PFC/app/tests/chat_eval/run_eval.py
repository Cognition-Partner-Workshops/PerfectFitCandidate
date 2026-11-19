"""Run with ../../.venv/bin/python tests/chat_eval/run_eval.py --base-url URL --out results.json."""

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

# Support direct invocation from PFC/app without installing test utilities.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from chat_eval.fixture import build_fixture  # noqa: E402
from chat_eval.harness import evaluate  # noqa: E402
from pfc.adapters.llm.providers import LlamaCppProvider  # noqa: E402


def print_report(report: dict[str, Any]) -> None:
    for name, summary in [("overall", report["overall"]), *report["categories"].items()]:
        print(f"{name}: {summary['correct']}/{summary['total']} ({summary['accuracy']:.1%})")
    print(f"Median: {report['median_seconds']:.3f}s; p95: {report['p95_seconds']:.3f}s")
    for failure in report["failures"]:
        print(
            f"FAIL {failure['id']}: {failure['question']}\n"
            f"  SQL: {failure['generated_sql']}\n  Error: {failure['error']}"
        )


async def run(base_url: str, out: Path, api_key: str = "") -> dict[str, Any]:
    fixture_dir = Path(tempfile.mkdtemp(prefix=out.stem + "-fixture-", dir=out.resolve().parent))
    settings = build_fixture(fixture_dir)
    provider = LlamaCppProvider(base_url, model="local", api_key=api_key or "local")
    try:
        report = await evaluate(settings, provider)
    finally:
        await provider.aclose()
    report["fixture_dir"] = str(fixture_dir)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print_report(report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url", required=True, help="Loopback llama-server URL, e.g. http://127.0.0.1:8080"
    )
    parser.add_argument("--out", type=Path, default=Path("results.json"))
    parser.add_argument(
        "--api-key", default=os.environ.get("LLAMA_API_KEY", ""), help="llama-server API key"
    )
    args = parser.parse_args()
    asyncio.run(run(args.base_url, args.out, args.api_key))


if __name__ == "__main__":
    main()
