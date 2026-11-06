"""Start the real FastAPI backend for the Playwright smoke test.

Wipes ``e2e/.data``, writes synthetic DL/CT workbooks to ``e2e/.data/fixtures`` and
serves the built SPA (``dist``) plus ``/api/v1`` on 127.0.0.1:$PFC_PORT.
"""

import os
import shutil
from pathlib import Path

import uvicorn
from openpyxl import Workbook
from pfc.services.ingestion.workbooks import CT, DL

HERE = Path(__file__).resolve().parent
DATA = HERE / ".data"


def workbook(path: Path, headers: list[str], rows: list[dict[str, str]]) -> None:
    book = Workbook()
    sheet = book.active
    sheet.append(["Synthetic smoke-test data"])
    sheet.append(headers)
    for row in rows:
        sheet.append([row.get(h) for h in headers])
    book.save(path)


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
    workbook(
        root / "ct.xlsx",
        list(CT),
        [
            {
                "ShortName": "SmokeAlex",
                "Email Address": "alex@example.com",
                "Grade": "B1",
                "Bench Status": "Available",
                "Skills": "SQL",
                "Group": "RG1",
            },
            {
                "ShortName": "SmokeTaylor",
                "Email Address": "taylor@example.com",
                "Grade": "C1",
                "Bench Status": "Available",
                "Skills": "Java",
                "Group": "RG1",
            },
        ],
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
    )
    port = int(os.environ.get("PFC_PORT", "8799"))
    uvicorn.run(
        "pfc.main:create_app", factory=True, host="127.0.0.1", port=port, log_level="warning"
    )


if __name__ == "__main__":
    main()
