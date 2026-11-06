"""Manual REQ-LLM-02 / REQ-MAT-02 / REQ-IN-23 probe; never run by default CI."""

import argparse
import asyncio
import hashlib
import json
import os
import platform
import re
import statistics
import threading
import unicodedata
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from time import perf_counter
from typing import Any

import anyio
import httpx
import numpy as np
from numpy.typing import NDArray
from pydantic import ValidationError
from starlette.datastructures import State

from pfc.adapters.llm.providers import (
    Capabilities,
    GenerateResult,
    Health,
    LLMProvider,
    PromptRequest,
    parse_output,
)
from pfc.adapters.llm.supervisor import LlamaServerSupervisor
from pfc.api.schemas.cv import CVFacts
from pfc.config import Settings
from pfc.domain.config_loader import load_domain_config
from pfc.domain.normalise.text import normalise_text
from pfc.jobs.runner import JobContext
from pfc.security.problems import Problem
from pfc.services import summaries
from pfc.services.ingestion.cv_facts import (
    KEYS,
    SCHEMA,
    LLMBridge,
    extract_facts,
    rule_facts,
    validate_facts,
)
from pfc.services.ingestion.masking import EMAIL, MASK_PHONE, YEAR_LIST
from pfc.services.matching.llm_bridge import JdLlmBridge
from pfc.services.matching.requirements import ExtractionFailure, Requirements, _fallback

FIXTURES = Path(__file__).resolve().parents[1] / "tests/fixtures/llm_validation.json"
HELD_OUT = FIXTURES.with_name("llm_validation_held_out.json")
MEMORY_LIMIT = 8_000_000_000


def normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def score_values(expected: list[str], actual: list[str]) -> dict[str, Any]:
    wanted, found = {normalized(v) for v in expected}, {normalized(v) for v in actual}
    hits = len(wanted & found)
    return {
        "expected": len(wanted),
        "predicted": len(found),
        "recovered": hits,
        "recall": hits / len(wanted) if wanted else None,
        "precision": hits / len(found) if found else None,
    }


def items(facts: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = facts.get(key)
    return ([value] if value else []) if key in ("name", "phone") else (value or [])


def cv_drops(text: str, raw: str | None) -> dict[str, Any]:
    """Reproduce quote location, then use the production schema and grounding validator."""
    empty = {
        key: {"raw": 0, "quote_located": 0, "validated": 0, "ungrounded": 0, "rule_precedence": 0}
        for key in KEYS
    }
    if raw is None:
        return {"schema_invalid": False, "response_missing": True, "categories": empty}
    try:
        output = parse_output(raw, SCHEMA)
    except Exception:
        return {"schema_invalid": True, "response_missing": False, "categories": empty}
    candidates: dict[str, Any] = {}
    for key in KEYS:
        located = []
        for fact in items(output, key):
            start = text.find(fact["evidence"])
            if start >= 0:
                located.append(
                    {**fact, "span": [start, start + len(fact["evidence"])], "source": "llm"}
                )
        candidates[key] = (located[0] if located else None) if key in ("name", "phone") else located
    validated = validate_facts(text, candidates).model_dump()
    rules = rule_facts(text).model_dump()
    for key in KEYS:
        raw_count = len(items(output, key))
        valid_count = len(items(validated, key))
        empty[key] = {
            "raw": raw_count,
            "quote_located": len(items(candidates, key)),
            "validated": valid_count,
            "ungrounded": raw_count - valid_count,
            "rule_precedence": valid_count if key in ("name", "phone") and rules[key] else 0,
        }
    return {"schema_invalid": False, "response_missing": False, "categories": empty}


def final_fact_failures(text: str, facts: CVFacts) -> int:
    before = facts.model_dump()
    after = validate_facts(text, before).model_dump()
    return sum(len(items(before, key)) - len(items(after, key)) for key in KEYS)


class RecordingProvider:
    """Delegate unchanged; optionally count responses without retaining completion text."""

    def __init__(self, provider: LLMProvider, *, retain_text: bool = True) -> None:
        self.provider = provider
        self.calls: list[dict[str, Any]] = []
        self.wire_texts: list[str] = []
        self.name, self.kind = provider.name, provider.kind
        self.retain_text = retain_text
        self.sentences_returned = 0

    def capabilities(self) -> Capabilities:
        return self.provider.capabilities()

    async def health(self) -> Health:
        return await self.provider.health()

    async def embed(self, texts: list[str]) -> NDArray[np.float32]:
        return await self.provider.embed(texts)

    async def observe_response(self, response: httpx.Response) -> None:
        await response.aread()
        try:
            content = response.json()["choices"][0]["message"]["content"]
            if isinstance(content, str):
                if self.retain_text:
                    self.wire_texts.append(content)
                else:
                    self.sentences_returned = sentence_count(content)
        except (ValueError, KeyError, IndexError, TypeError):
            pass

    async def generate(
        self, request: PromptRequest, *, api_version: str | None = None
    ) -> GenerateResult:
        started = perf_counter()
        self.wire_texts = []
        self.sentences_returned = 0
        call: dict[str, Any] = {"purpose": request.purpose, "raw": None, "error": None}
        try:
            result = await self.provider.generate(request, api_version=api_version)
            if self.retain_text:
                call["raw"] = result.text
            else:
                self.sentences_returned = sentence_count(result.text)
            return result
        except Exception as exc:
            call["error"] = type(exc).__name__
            raise
        finally:
            if call["raw"] is None and self.wire_texts:
                call["raw"] = self.wire_texts[-1]
            call["wire_responses"] = self.wire_texts.copy()
            call["latency_s"] = perf_counter() - started
            if not self.retain_text:
                call["sentences_returned"] = self.sentences_returned
            self.calls.append(call)


def sentence_count(raw: str) -> int:
    try:
        value = json.loads(raw)
    except ValueError:
        return 0
    sentences = value.get("sentences") if isinstance(value, dict) else None
    return len(sentences) if isinstance(sentences, list) else 0


def remaining_pii(text: str) -> int:
    phones = sum(
        not YEAR_LIST.fullmatch(match[0]) and len(re.sub(r"\D", "", match[0])) >= 7
        for match in MASK_PHONE.finditer(text)
    )
    return len(EMAIL.findall(text)) + phones


async def run_summary(provider: RecordingProvider, fixture: dict[str, Any]) -> dict[str, Any]:
    started = perf_counter()
    before = len(provider.calls)
    row: dict[str, Any] = {
        "id": fixture["id"],
        "schema_valid": False,
        "sentences_returned": 0,
        "sentences_kept": 0,
        "evidence_drops": 0,
        "digit_drops": 0,
        "remaining_pii_patterns": 0,
    }
    try:
        result = await summaries.summarise_text(provider, normalise_text(fixture["text"]))
        row.update(
            schema_valid=True,
            sentences_returned=result.returned,
            sentences_kept=len(result.sentences),
            evidence_drops=result.evidence_drops,
            digit_drops=result.digit_drops,
            remaining_pii_patterns=sum(
                remaining_pii(sentence.text) + remaining_pii(sentence.evidence)
                for sentence in result.sentences
            ),
            outcome="200" if result.sentences else "422 SUMMARY_UNGROUNDED",
        )
    except Problem as exc:
        row["outcome"] = f"{exc.status} {exc.code}"
        if len(provider.calls) > before:
            row["sentences_returned"] = provider.calls[-1]["sentences_returned"]
    row["latency_s"] = perf_counter() - started
    return row


def summary_totals(rows: list[dict[str, Any]], peak_bytes: int | None) -> dict[str, Any]:
    returned = sum(row["sentences_returned"] for row in rows)
    kept = sum(row["sentences_kept"] for row in rows)
    latencies = [row["latency_s"] for row in rows]
    return {
        "calls": len(rows),
        "schema_valid": sum(row["schema_valid"] for row in rows),
        "outcomes": dict(sorted(Counter(row["outcome"] for row in rows).items())),
        "sentences_returned": returned,
        "sentences_kept": kept,
        "evidence_drops": sum(row["evidence_drops"] for row in rows),
        "digit_drops": sum(row["digit_drops"] for row in rows),
        "remaining_pii_patterns": sum(row["remaining_pii_patterns"] for row in rows),
        "kept_share": kept / returned if returned else None,
        "share_422": sum(row["outcome"].startswith("422 ") for row in rows) / len(rows)
        if rows
        else None,
        "median_s": statistics.median(latencies) if rows else None,
        "max_s": max(latencies) if rows else None,
        "peak_server_bytes": peak_bytes,
        "memory_pass": peak_bytes is not None and peak_bytes <= MEMORY_LIMIT,
    }


async def summary_pass(
    provider: RecordingProvider,
    fixtures: dict[str, Any],
    held_out: dict[str, Any],
    memory_probe: Callable[[], int | None],
) -> dict[str, Any]:
    report: dict[str, Any] = {"summary_results": {}}
    for category, documents in (("jd", fixtures["jds"] + held_out["jds"]), ("cv", fixtures["cvs"])):
        rows = []
        for fixture in documents:
            row = await run_summary(provider, fixture)
            rows.append(row)
            print(f"Summary {fixture['id']}: {row['outcome']}", flush=True)
        report["summary_results"][category] = rows
        report[category] = summary_totals(rows, memory_probe())
    report["memory_pass"] = all(report[key]["memory_pass"] for key in ("jd", "cv"))
    return report


def render_summary_markdown(report: dict[str, Any]) -> str:
    lines = ["# Live summary validation", "", "Linux VM, not target laptop.", ""]
    for category in ("jd", "cv"):
        group = report[category]
        outcomes = group["outcomes"]
        peak = group["peak_server_bytes"]
        metrics = {
            "Calls": group["calls"],
            "Schema-valid calls": group["schema_valid"],
            "200": outcomes.get("200", 0),
            "422 SUMMARY_UNGROUNDED": outcomes.get("422 SUMMARY_UNGROUNDED", 0),
            "422 SUMMARY_INVALID": outcomes.get("422 SUMMARY_INVALID", 0),
            "503 LLM_UNAVAILABLE": outcomes.get("503 LLM_UNAVAILABLE", 0),
            "Sentences returned": group["sentences_returned"],
            "Sentences kept": group["sentences_kept"],
            "Evidence drops": group["evidence_drops"],
            "Digit-rule drops": group["digit_drops"],
            "Kept share": display(group["kept_share"], percent=True),
            "Calls ending in 422": display(group["share_422"], percent=True),
            "Median latency (s)": display(group["median_s"]),
            "Maximum latency (s)": display(group["max_s"]),
            "Remaining email/phone patterns": group["remaining_pii_patterns"],
            "Peak model RSS/HWM (bytes)": peak if peak is not None else "n/a",
            "Model ≤ 8 GB": "PASS" if group["memory_pass"] else "FAIL/unavailable",
        }
        lines += [f"## {category.upper()}", "", "| Metric | Value |", "|---|---:|"]
        lines += [f"| {key} | {value} |" for key, value in metrics.items()]
        lines += [
            "",
            "| Fixture | Schema valid | Returned | Kept | Evidence drops | "
            "Digit drops | Remaining PII | Outcome | Latency (s) |",
            "|---|---|---:|---:|---:|---:|---:|---|---:|",
        ]
        for row in report["summary_results"][category]:
            lines.append(
                f"| {row['id']} | {'yes' if row['schema_valid'] else 'no'} | "
                f"{row['sentences_returned']} | {row['sentences_kept']} | "
                f"{row['evidence_drops']} | {row['digit_drops']} | "
                f"{row['remaining_pii_patterns']} | {row['outcome']} | "
                f"{display(row['latency_s'])} |"
            )
        lines.append("")
    return "\n".join(lines)


def run_jd(provider: RecordingProvider, text: str) -> dict[str, Any]:
    bridge = JdLlmBridge(provider)
    reason = None
    try:
        requirements = Requirements.model_validate(bridge.extract_jd(text))
    except Exception as exc:
        reason = (
            str(exc)
            if isinstance(exc, ExtractionFailure)
            else "schema_invalid"
            if isinstance(exc, ValidationError)
            else "provider_error"
        )
        requirements = _fallback(text, load_domain_config())
    return {
        "fallback_reason": reason,
        "model": bridge.model,
        "requirements": requirements.model_dump(),
    }


def jd_schema_invalid(raw: str | None) -> bool:
    if raw is None:
        return False
    try:
        json.loads(raw)
    except ValueError:
        return False
    try:
        Requirements.model_validate(parse_output(raw, Requirements.model_json_schema()))
    except Exception:
        return True
    return False


def score_jd(
    fixture: dict[str, Any], result: dict[str, Any], raw: str | None = None
) -> dict[str, Any]:
    return {
        "id": fixture["id"],
        **result,
        "schema_invalid": jd_schema_invalid(raw) or result["fallback_reason"] == "schema_invalid",
        "scores": {
            key: score_values(
                fixture["expected"][key], [v["text"] for v in result["requirements"][key]]
            )
            for key in ("mandatory_skills", "preferred_skills")
        },
    }


def score_cv(fixture: dict[str, Any], facts: CVFacts, raw: str | None) -> dict[str, Any]:
    text = normalise_text(fixture["text"])
    return {
        "id": fixture["id"],
        "facts": facts.model_dump(),
        "scores": {
            key: score_values(
                fixture["expected"][key], [v["value"] for v in items(facts.model_dump(), key)]
            )
            for key in KEYS
        },
        "drops": cv_drops(text, raw),
        "final_fact_failures": final_fact_failures(text, facts),
    }


def totals(scores: list[dict[str, Any]]) -> dict[str, Any]:
    expected = sum(s["expected"] for s in scores)
    predicted = sum(s["predicted"] for s in scores)
    recovered = sum(s["recovered"] for s in scores)
    return {
        "expected": expected,
        "predicted": predicted,
        "recovered": recovered,
        "recall": recovered / expected if expected else None,
        "precision": recovered / predicted if predicted else None,
    }


def summarize(
    jds: list[dict[str, Any]],
    cvs: list[dict[str, Any]],
    calls: list[dict[str, Any]],
    *,
    load_s: float,
    peak_bytes: int | None,
    hardware: dict[str, Any],
) -> dict[str, Any]:
    reasons = Counter(j["fallback_reason"] for j in jds if j["fallback_reason"])
    latencies = [c["latency_s"] for c in calls]
    return {
        "hardware": hardware,
        "model_load_s": load_s,
        "peak_server_bytes": peak_bytes,
        "memory_limit_bytes": MEMORY_LIMIT,
        "memory_pass": peak_bytes is not None and peak_bytes <= MEMORY_LIMIT,
        "jd": {
            "total": len(jds),
            "llm_used": len(jds) - sum(reasons.values()),
            "llm_share": (len(jds) - sum(reasons.values())) / len(jds) if jds else None,
            "fallback_reasons": dict(sorted(reasons.items())),
            "schema_invalid": sum(j["schema_invalid"] for j in jds),
            "scores": {
                k: totals([j["scores"][k] for j in jds])
                for k in ("mandatory_skills", "preferred_skills")
            },
        },
        "cv": {
            "total": len(cvs),
            "final_fact_failures": sum(c["final_fact_failures"] for c in cvs),
            "schema_invalid": sum(c["drops"]["schema_invalid"] for c in cvs),
            "scores": {k: totals([c["scores"][k] for c in cvs]) for k in KEYS},
            "drops": {
                k: {
                    metric: sum(c["drops"]["categories"][k][metric] for c in cvs)
                    for metric in (
                        "raw",
                        "quote_located",
                        "validated",
                        "ungrounded",
                        "rule_precedence",
                    )
                }
                for k in KEYS
            },
        },
        "latency": {
            "calls": len(calls),
            "median_s": statistics.median(latencies) if latencies else None,
            "max_s": max(latencies) if latencies else None,
        },
        "jd_results": jds,
        "cv_results": cvs,
        "calls": calls,
    }


def display(value: float | None, *, percent: bool = False) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%" if percent else f"{value:.3f}"


def render_markdown(report: dict[str, Any]) -> str:
    if "summary_results" in report:
        return render_summary_markdown(report)
    jd, cv, latency = report["jd"], report["cv"], report["latency"]
    peak = report["peak_server_bytes"]
    peak_gb = display(peak / 1e9 if peak is not None else None)
    memory_status = "PASS" if report["memory_pass"] else "FAIL/unavailable"
    lines = [
        "# Live LLM extraction validation",
        "",
        "Synthetic fixtures only; no target-laptop acceptance claim.",
        "",
        f"Hardware: {report['hardware']}",
        f"Supervisor startup → healthy: {display(report['model_load_s'])} s",
        f"llama-server peak RSS/HWM: {peak_gb} GB / 8 GB — {memory_status}",
        f"Generation calls: {latency['calls']}; median {display(latency['median_s'])} s; "
        f"max {display(latency['max_s'])} s",
        "",
        "## JD",
        "",
        f"LLM used: {jd['llm_used']}/{jd['total']} ({display(jd['llm_share'], percent=True)}).",
        f"Fallback reasons: {json.dumps(jd['fallback_reasons'], sort_keys=True)}",
        f"Schema-invalid count: {jd['schema_invalid']}",
        "",
        "| Skills | Recovered / expected | Precision | Recall |",
        "|---|---:|---:|---:|",
    ]
    for key, score in jd["scores"].items():
        lines.append(
            f"| {key} | {score['recovered']}/{score['expected']} | "
            f"{display(score['precision'], percent=True)} | "
            f"{display(score['recall'], percent=True)} |"
        )
    lines += [
        "",
        "## CV",
        "",
        f"CVs: {cv['total']}; schema-invalid responses: {cv['schema_invalid']}; "
        f"final fact/span validation failures: {cv['final_fact_failures']} (required: 0).",
        "",
        "| Category | Recovered / expected | Raw → located → validated | "
        "Ungrounded drops | Rule precedence (not drops) |",
        "|---|---:|---:|---:|---:|",
    ]
    for key, score in cv["scores"].items():
        drop = cv["drops"][key]
        lines.append(
            f"| {key} | {score['recovered']}/{score['expected']} | "
            f"{drop['raw']} → {drop['quote_located']} → {drop['validated']} | "
            f"{drop['ungrounded']} | {drop['rule_precedence']} |"
        )
    lines += [
        "",
        "Scoring: exact NFKC + casefold + whitespace-collapsed set equality, per category; "
        "micro-averaged precision/recall. No aliases, substring matches or semantic credit. "
        "Empty denominators are n/a.",
        "Ungrounded = raw minus validated facts in schema-valid CV responses; "
        "schema-invalid responses are counted separately. "
        "Rule-precedence replacements are not validator drops.",
        "",
    ]
    return "\n".join(lines)


def process_memory(pid: int) -> int | None:
    try:
        status = Path(f"/proc/{pid}/status").read_text()
    except FileNotFoundError:
        return None
    values = [int(kb) * 1024 for kb in re.findall(r"^Vm(?:HWM|RSS):\s+(\d+) kB", status, re.M)]
    return max(values) if values else None


async def live(args: argparse.Namespace) -> dict[str, Any]:
    fixtures = json.loads(args.fixtures.read_text())
    gguf, binary = args.gguf.expanduser().resolve(), args.llama_server.expanduser().resolve()
    lock = json.loads((Path(__file__).resolve().parents[2] / "packaging/MODELS.lock").read_text())
    pin = next(item for item in lock if item["file"] == gguf.name)
    with gguf.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != pin["sha256"]:
            raise ValueError("GGUF SHA-256 differs from MODELS.lock")
    settings = Settings(
        data_dir=args.out.expanduser().resolve().parent / "supervisor-data",
        models_dir=gguf.parent,
        llama_dir=binary.parent,
    )
    if binary != settings.llama_dir / "llama-server":
        raise ValueError("Binary must be named llama-server")
    state = State()
    supervisor = LlamaServerSupervisor(settings, app_state=state)
    peak: int | None = None

    async def sample() -> None:
        nonlocal peak
        while True:
            if supervisor.proc is not None:
                value = process_memory(supervisor.proc.pid)
                if value is not None:
                    peak = max(peak or 0, value)
            await asyncio.sleep(0.05)

    sampler = asyncio.create_task(sample())
    try:
        started = perf_counter()
        await supervisor.start(pin["name"], gguf)
        load_s = perf_counter() - started
        if supervisor.state != "ready" or state.llm_provider is None or supervisor.provider is None:
            raise RuntimeError(
                f"Supervisor unavailable: {supervisor.state}: {supervisor.detail}; "
                f"see {settings.data_dir}/logs/llm.log"
            )
        if getattr(args, "summaries_only", False):
            provider = RecordingProvider(state.llm_provider, retain_text=False)
            supervisor.provider._client.event_hooks["response"].append(provider.observe_response)

            def memory_probe() -> int | None:
                measured = process_memory(supervisor.proc.pid) if supervisor.proc else None
                return max(peak or 0, measured or 0) or None

            report = await summary_pass(
                provider,
                fixtures,
                json.loads(args.held_out_fixtures.read_text(encoding="utf-8")),
                memory_probe,
            )
            report.update(
                model_pin=pin,
                binary=str(binary),
                model_load_s=load_s,
                hardware={
                    "platform": platform.platform(),
                    "python": platform.python_version(),
                    "cpus": os.cpu_count(),
                    "ram_bytes": os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"),
                    "context": settings.llm_ctx_size,
                    "threads": settings.llm_threads or max(1, (os.cpu_count() or 2) // 2 - 1),
                },
                memory_limit_bytes=MEMORY_LIMIT,
            )
            return report
        provider = RecordingProvider(state.llm_provider)
        # Observe before adapter schema validation; do not alter requests or validators.
        supervisor.provider._client.event_hooks["response"].append(provider.observe_response)
        bridge = LLMBridge(provider, asyncio.get_running_loop(), asyncio.Semaphore(1))
        # Standalone extraction uses cancellation only, never runner-backed persistence.
        context = JobContext(None, 0, threading.Event())  # type: ignore[arg-type]
        jds, cvs = [], []
        for fixture in fixtures["jds"]:
            before = len(provider.calls)
            result = await anyio.to_thread.run_sync(
                run_jd, provider, normalise_text(fixture["text"])
            )
            raw = provider.calls[-1]["raw"] if len(provider.calls) > before else None
            jds.append(score_jd(fixture, result, raw))
            print(f"JD {fixture['id']}: {result['fallback_reason'] or 'llm'}", flush=True)
        for fixture in fixtures["cvs"]:
            before = len(provider.calls)
            cv_result = await anyio.to_thread.run_sync(
                extract_facts, normalise_text(fixture["text"]), bridge, 180, context
            )
            raw = provider.calls[-1]["raw"] if len(provider.calls) > before else None
            scored = score_cv(fixture, cv_result.facts, raw)
            scored.update(extracted_by=cv_result.extracted_by, failure=cv_result.failure)
            cvs.append(scored)
            print(f"CV {fixture['id']}: {cv_result.extracted_by}; {cv_result.failure}", flush=True)
        hardware = {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpus": os.cpu_count(),
            "ram_bytes": os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"),
            "context": settings.llm_ctx_size,
            "threads": settings.llm_threads or max(1, (os.cpu_count() or 2) // 2 - 1),
        }
        if supervisor.proc is not None:
            measured = process_memory(supervisor.proc.pid)
            if measured is not None:
                peak = max(peak or 0, measured)
        report = summarize(
            jds, cvs, provider.calls, load_s=load_s, peak_bytes=peak, hardware=hardware
        )
        report["model_pin"] = pin
        report["binary"] = str(binary)
        return report
    finally:
        sampler.cancel()
        await asyncio.gather(sampler, return_exceptions=True)
        await supervisor.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gguf", type=Path, default=os.getenv("PFC_VALIDATION_GGUF"))
    parser.add_argument(
        "--llama-server", type=Path, default=os.getenv("PFC_VALIDATION_LLAMA_SERVER")
    )
    parser.add_argument("--fixtures", type=Path, default=FIXTURES)
    parser.add_argument("--held-out-fixtures", type=Path, default=HELD_OUT)
    parser.add_argument(
        "--summaries-only",
        action="store_true",
        help="Run count-only summaries for all fixtures and held-out JDs instead of extraction",
    )
    parser.add_argument(
        "--out", type=Path, required=True, help="Output prefix outside the repository"
    )
    args = parser.parse_args()
    if args.gguf is None or args.llama_server is None:
        parser.error("Supply --gguf and --llama-server (or PFC_VALIDATION_* environment variables)")
    args.out = args.out.expanduser().resolve()
    if args.out.is_relative_to(Path(__file__).resolve().parents[3]):
        parser.error("Output must be outside the repository")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    report = anyio.run(live, args)
    Path(f"{args.out}.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    markdown = render_markdown(report)
    Path(f"{args.out}.md").write_text(markdown, encoding="utf-8")
    print(markdown)
    if "summary_results" in report:
        return int(
            not report["memory_pass"]
            or any(report[key]["remaining_pii_patterns"] for key in ("jd", "cv"))
        )
    return int(not report["memory_pass"] or report["cv"]["final_fact_failures"] != 0)


if __name__ == "__main__":
    raise SystemExit(main())
