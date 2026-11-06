"""Start the real FastAPI backend for the Playwright smoke test.

Wipes ``e2e/.data``, writes synthetic DL/CT workbooks to ``e2e/.data/fixtures``, installs
the backend tests' tiny stand-in ONNX embedder as the active embedding model, and serves the
built SPA (``dist``) plus ``/api/v1`` on 127.0.0.1:$PFC_PORT.
"""

import hashlib
import os
import shutil
import sys
from pathlib import Path

import uvicorn
from openpyxl import Workbook
from pfc.config import Settings
from pfc.db.connection import database
from pfc.db.migrate import migrate
from pfc.services.embeddings.chunking import pipeline_version
from pfc.services.ingestion.workbooks import CT, DL

HERE = Path(__file__).resolve().parent
DATA = HERE / ".data"
APP = HERE.parents[1] / "app"


def workbook(path: Path, headers: list[str], rows: list[dict[str, str]]) -> None:
    book = Workbook()
    sheet = book.active
    sheet.append(["Synthetic smoke-test data"])
    sheet.append(headers)
    for row in rows:
        sheet.append([row.get(h) for h in headers])
    book.save(path)


CT_HEADERS = list(CT)
SMOKE_TAYLOR = {
    "ShortName": "SmokeTaylor",
    "Email Address": "taylor@example.com",
    "Grade": "C1",
    "Bench Status": "Available",
    "Skills": "Java",
    "Group": "RG1",
}


def smoke_alex(skills: str) -> dict[str, str]:
    return {
        "ShortName": "SmokeAlex",
        "Email Address": "alex@example.com",
        "Grade": "B1",
        "Bench Status": "Available",
        "Skills": skills,
        "Group": "RG1",
    }


def fixtures(root: Path) -> None:
    root.mkdir(parents=True)
    workbook(
        root / "dl.xlsx",
        list(DL),
        [
            {
                "Code Requisition": "T10001",
                "Code Local Grade": "B1",
                "Account Name": "Acme",
                "JD": "SQL, Python",
            },
            {
                "Code Requisition": "T10002",
                "Code Local Grade": "C1",
                "Account Name": "Globex",
                "JD": "Java",
            },
        ],
    )
    workbook(root / "ct.xlsx", CT_HEADERS, [smoke_alex("SQL"), SMOKE_TAYLOR])
    # Committed after the match run, so the run is flagged "DL/CT committed".
    workbook(root / "ct-update.xlsx", CT_HEADERS, [smoke_alex("SQL, Python"), SMOKE_TAYLOR])


def embedding_model(settings: Settings) -> None:
    """Activate the backend tests' 8-dim stand-in ONNX bundle where the runtime looks for it."""
    sys.path.insert(0, str(APP))
    from tests.unit.services.embeddings.conftest import tiny_bundle

    model = tiny_bundle(settings.bundled_models_dir / "nomic-embed-text-v1.5")
    migrate(settings)
    with database(settings) as db:
        db.execute(
            "INSERT INTO embedding_models(name,runtime,file_sha256,dim,pipeline_version,is_active)"
            " VALUES ('nomic-embed-text-v1.5','onnx',?,8,?,1)",
            (hashlib.sha256(model.read_bytes()).hexdigest(), pipeline_version("e2e-tiny-v1")),
        )


def main() -> None:
    shutil.rmtree(DATA, ignore_errors=True)
    fixtures(DATA / "fixtures")
    os.environ.update(
        PFC_DATA_DIR=str(DATA),
        PFC_FRONTEND_DIR=str(HERE.parent / "dist"),
        PFC_LLM_ENABLED="false",
        PFC_ARGON_MEMORY_COST="1024",
        PFC_ARGON_TIME_COST="1",
        PFC_ARGON_PARALLELISM="1",
        PFC_BUNDLED_MODELS_DIR=str(DATA / "bundled-models"),
    )
    embedding_model(Settings())
    port = int(os.environ.get("PFC_PORT", "8799"))
    uvicorn.run(
        "pfc.main:create_app", factory=True, host="127.0.0.1", port=port, log_level="warning"
    )


if __name__ == "__main__":
    main()
