"""Manual CPU-only model validation; no downloads and no pytest/CI integration."""

import argparse
import resource
import sys
from pathlib import Path
from time import perf_counter

import numpy as np
import onnxruntime as ort  # type: ignore[import-untyped]
from numpy.typing import NDArray

from pfc.adapters.vector.embeddings import OnnxEmbedder, Task, Vectors

PAIRS = (
    (
        "Python backend engineer",
        "Hire a backend software engineer for Python, FastAPI REST services, "
        "PostgreSQL and pytest.",
        "Backend developer building Python FastAPI APIs, PostgreSQL schemas "
        "and pytest test suites.",
    ),
    (
        "Cardiac intensive-care nurse",
        "Seek a registered cardiac ICU nurse with ventilator care, ECG monitoring and ECMO skills.",
        "Registered intensive-care nurse caring for cardiac patients, ventilators, ECG and ECMO.",
    ),
    (
        "Bridge structural engineer",
        "Need a civil structural engineer to design reinforced-concrete highway bridges using FEM.",
        "Civil engineer designing reinforced-concrete bridges, structural loads and FEM analysis.",
    ),
    (
        "Financial auditor",
        "Recruit a financial auditor for IFRS reporting, SOX controls "
        "and statutory accounts audits.",
        "Chartered accountant performing statutory financial audits, "
        "IFRS reporting and SOX reviews.",
    ),
    (
        "Kindergarten teacher",
        "Hire a kindergarten teacher for early-childhood education, "
        "phonics and play-based learning.",
        "Early-childhood teacher teaching kindergarten phonics, literacy and play-based lessons.",
    ),
    (
        "Solar installation electrician",
        "Need an electrician installing rooftop solar photovoltaic panels, "
        "inverters and NEC wiring.",
        "Licensed electrician installing rooftop photovoltaic arrays, "
        "solar inverters and NEC wiring.",
    ),
    (
        "Pastry chef",
        "Seek a pastry chef specializing in croissants, sourdough fermentation and artisan baking.",
        "Professional pastry chef making croissants, viennoiserie and fermented sourdough bread.",
    ),
    (
        "Web penetration tester",
        "Recruit a cybersecurity penetration tester for OWASP web exploits and Burp Suite testing.",
        "Security penetration tester finding OWASP web vulnerabilities using Burp Suite exploits.",
    ),
    (
        "Vineyard horticulturist",
        "Hire a vineyard horticulturist for grape cultivation, viticulture, "
        "soil health and irrigation.",
        "Vineyard horticulturist managing grape vines, viticulture, "
        "soil nutrients and drip irrigation.",
    ),
    (
        "Aircraft engine mechanic",
        "Need an FAA A&P aircraft mechanic for turbine engine maintenance "
        "and airworthiness checks.",
        "FAA A&P certified aviation mechanic maintaining aircraft turbines "
        "and checking airworthiness.",
    ),
)


def check(label: str, passed: bool) -> bool:
    print(f"{'PASS' if passed else 'FAIL'}: {label}", flush=True)
    return passed


def print_matrix(label: str, matrix: NDArray[np.float32] | NDArray[np.int64]) -> None:
    print(f"\n{label} (rows=JD queries, columns=CV documents)")
    print("JD/CV " + " ".join(f"{i + 1:>6}" for i in range(len(PAIRS))))
    for i, row in enumerate(matrix):
        cells = [
            f"{value:6.3f}" if matrix.dtype.kind == "f" else f"{int(value):6d}" for value in row
        ]
        print(f"{i + 1:>5} " + " ".join(cells))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model-dir",
        type=Path,
        required=True,
        help="Local directory containing model_int8.onnx and tokenizer.json",
    )
    args = parser.parse_args()
    directory = args.model_dir.expanduser().resolve()
    print(f"Model directory: {directory}", flush=True)
    print(f"onnxruntime: {ort.__version__} (CPUExecutionProvider)", flush=True)
    latencies: list[float] = []
    batch_seconds = 0.0
    batch_chunks = 0
    passed = True
    try:
        started = perf_counter()
        embedder = OnnxEmbedder(
            directory / "model_int8.onnx",
            directory / "tokenizer.json",
            dimensions=768,
        )
        print(f"Model load: {perf_counter() - started:.3f} s", flush=True)
        print(f"Model version: {embedder.model_version}", flush=True)
        probe = "Python backend engineer building FastAPI services with PostgreSQL."
        document = embedder.embed([probe], task="document")
        query = embedder.embed([probe], task="query")
        prefixed = embedder.embed(["search_document: " + probe], task="document")
        passed &= check(
            "document and query prefixes produce different vectors",
            not np.allclose(document, query, atol=1e-5),
        )
        if np.allclose(document, prefixed, atol=1e-5):
            check("pre-prefixed document is idempotent", True)
        else:
            print(
                "INFO: pre-prefixed input is not idempotent; the adapter unconditionally "
                "adds its task prefix. Supply raw text.",
                flush=True,
            )
        single_vectors: list[Vectors] = []
        tasks: tuple[Task, ...] = ("query", "document")
        for task in tasks:
            for _, jd, cv in PAIRS:
                text = jd if task == "query" else cv
                started = perf_counter()
                single_vectors.append(embedder.embed([text], task=task))
                latencies.append(perf_counter() - started)
        batches: dict[Task, Vectors] = {}
        for task in tasks:
            texts = [jd if task == "query" else cv for _, jd, cv in PAIRS]
            started = perf_counter()
            batches[task] = embedder.embed(texts, task=task)
            batch_seconds += perf_counter() - started
            batch_chunks += len(texts)
        vectors = np.concatenate([document, query, prefixed, *single_vectors, *batches.values()])
        passed &= check("output dimension is 768", vectors.shape[1] == 768)
        norms = np.linalg.norm(vectors, axis=1)
        passed &= check(
            "vectors are finite and L2-normalized",
            bool(np.isfinite(vectors).all() and np.allclose(norms, 1, atol=1e-5)),
        )
        print(f"L2 norm range: {norms.min():.7f} .. {norms.max():.7f}")
        print("\nSynthetic pair labels:")
        for i, (role, _, _) in enumerate(PAIRS, 1):
            print(f"{i:2d}: {role}")
        scores = batches["query"] @ batches["document"].T
        ranks = np.argsort(np.argsort(-scores, axis=1, kind="stable"), axis=1, kind="stable") + 1
        print_matrix("Cosine similarity matrix", scores)
        print_matrix("Rank matrix (1=best)", ranks)
        matching_ranks = np.diag(ranks)
        print("Matching CV ranks: " + ", ".join(str(int(rank)) for rank in matching_ranks))
        passed &= check(
            f"retrieval top-1: {int((matching_ranks == 1).sum())}/{len(PAIRS)}",
            bool((matching_ranks == 1).all()),
        )
    except Exception as exc:
        passed = False
        print(f"FAIL: {type(exc).__name__}: {exc}", flush=True)
    finally:
        if latencies:
            milliseconds = np.asarray(latencies) * 1000
            print(
                f"\nPer-chunk latency ({len(latencies)} warmed single-chunk calls): "
                f"mean={milliseconds.mean():.3f} ms, "
                f"p50={np.percentile(milliseconds, 50):.3f} ms, "
                f"p95={np.percentile(milliseconds, 95):.3f} ms"
            )
        if batch_chunks:
            print(
                f"Batch throughput ({batch_chunks} chunks, two {len(PAIRS)}-chunk batches): "
                f"{batch_chunks / batch_seconds:.3f} chunks/s ({batch_seconds:.3f} s)"
            )
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        mib = peak / (1024 * 1024 if sys.platform == "darwin" else 1024)
        print(f"Peak RSS (resource.getrusage): {mib:.3f} MiB", flush=True)
    print(f"\nValidation {'PASSED' if passed else 'FAILED'}", flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
