"""Write synthetic UAT inputs: DL and CT workbooks and a resume folder of .docx CVs.

Everything is synthetic: example.com emails, Ofcom drama phone numbers (07700 900000-900999),
and invented names, accounts and projects. The same seed always gives the same data.
"""

import argparse
import random
import re
import sys
from datetime import date, timedelta
from io import BytesIO
from pathlib import Path

from docx import Document
from openpyxl import Workbook  # type: ignore[import-untyped]

from pfc.services.ingestion.workbooks import CT, DL, GRADES

SEED = 19401
SKILLS = (
    "SQL",
    "Python",
    "Java",
    "AWS",
    "Azure",
    "Business Analysis",
    "Agile",
    "Scrum",
    "Power BI",
    "Excel",
)
LOCATIONS = ("Manchester", "London", "Birmingham", "Leeds")
BASE_DATE = date(2026, 11, 6)
GRADE_ORDER = sorted(GRADES)
ELIGIBLE_STATUSES = (
    "Available",
    "Overall upskilling (New bench)",
    "CV with client - Demand Proposed",
)
EXCLUDED_STATUS = "Selected"
UNMAPPED_STATUS = "On secondment"
UNPARSEABLE_DATE = "TBC"
MAX_ROWS = 9999
GENERATED_CV = re.compile(r"person\d{4}@example\.com\.(txt|docx)")


def workbook_bytes(headers: list[str], rows: list[dict[str, object]]) -> bytes:
    """A title row, the header row, then one row per dict (missing columns stay blank)."""
    book = Workbook()
    sheet = book.active
    sheet.append(["Synthetic UAT data - not real people"])
    sheet.append(headers)
    for row in rows:
        sheet.append([row.get(header) for header in headers])
    output = BytesIO()
    book.save(output)
    book.close()
    return output.getvalue()


def phone(index: int) -> str:
    return f"07700 900{index % 1000:03d}"


def demand_rows(size: int, rng: random.Random) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index in range(1, size + 1):
        skills = rng.sample(SKILLS, 3)
        location = rng.choice(LOCATIONS)
        onsite = " Onsite attendance is mandatory." if index % 4 == 0 else ""
        rows.append(
            {
                "Code Requisition": f"T{10000 + index}",
                "Position Start Date": BASE_DATE + timedelta(days=rng.randrange(-30, 91)),
                "Account Name": f"Synthetic Account {index % 8}",
                "Code Local Grade": GRADE_ORDER[(index - 1) % len(GRADE_ORDER)],
                "Demand Request Name": f"Synthetic role {index}",
                "Region": location,
                "BU": f"BU {index % 4}",
                "Owner": f"owner{index % 5}@example.com",
                "JD": (
                    f"Synthetic role {index}. Mandatory skills: {skills[0]}, {skills[1]}. "
                    f"Preferred skills: {skills[2]}. At least {2 + index % 6} years of experience. "
                    f"Qualification: degree in a technical subject. Location: {location}.{onsite}"
                ),
            }
        )
    return rows


def resource_rows(size: int, rng: random.Random) -> list[dict[str, object]]:
    """Mixes eligible, status-excluded, end-dated, unparseable-date, unmapped and off-grade rows."""
    rows: list[dict[str, object]] = []
    for index in range(1, size + 1):
        if index % 5 == 0:
            status = EXCLUDED_STATUS
        elif index % 47 == 0:
            status = UNMAPPED_STATUS
        else:
            status = ELIGIBLE_STATUSES[index % 3]
        end: object = None
        if index % 13 == 0:
            end = BASE_DATE + timedelta(days=30)
        elif index % 29 == 0:
            end = UNPARSEABLE_DATE
        grade = (index - 1) % len(GRADE_ORDER) + rng.choice((-2, -1, 0, 0, 0, 1, 2))
        rows.append(
            {
                "ShortName": f"Person{index:04d}",
                "Email Address": f"person{index:04d}@example.com",
                "Grade": GRADE_ORDER[min(max(grade, 0), len(GRADE_ORDER) - 1)],
                "Location": rng.choice(LOCATIONS),
                "Bench Status": status,
                "Skills": ", ".join(rng.sample(SKILLS, 4)),
                "Available Capacity": ("Y", "N", None)[index % 3],
                "Bench Start Date": BASE_DATE - timedelta(days=rng.randrange(1, 181)),
                "Bench End Date": end,
                "Group": f"RG{index % 3 + 1}",
                "Group Owner": f"lead{index % 3 + 1}@example.com",
            }
        )
    return rows


def cv_text(index: int, row: dict[str, object], rng: random.Random) -> str:
    skills = str(row["Skills"])
    first, second = skills.split(", ")[:2]
    lines = [
        str(row["ShortName"]),
        f"Email: {row['Email Address']}",
        f"Phone: {phone(index)}",
        "",
        f"Skills: {skills}",
        "",
        "Experience",
        f"{2 + rng.randrange(12)} years at Synthetic Consulting Ltd as a {first} consultant.",
        "",
        "Projects",
        f"Synthetic project {index}: {second} delivery for Synthetic Account {index % 8}.",
    ]
    if index % 17:
        lines += ["", "Qualifications", "BSc Computer Science (synthetic)"]
    return "\n".join(lines) + "\n"


def write_cv(path: Path, text: str) -> None:
    """One paragraph per line of the CV text."""
    doc = Document()
    for line in text.splitlines():
        doc.add_paragraph(line)
    doc.save(str(path))


def dirty_rows(
    rows: list[dict[str, object]], percent: float, rng: random.Random
) -> tuple[list[dict[str, object]], int]:
    """Copy of the CT rows with `percent` (at least one row) changed: one skill and the location."""
    changed = rng.sample(range(len(rows)), max(1, round(len(rows) * percent / 100)))
    result = [dict(row) for row in rows]
    for position in changed:
        row = result[position]
        skills = str(row["Skills"]).split(", ")
        skills[0] = next(skill for skill in SKILLS if skill not in skills)
        row["Skills"] = ", ".join(skills)
        row["Location"] = LOCATIONS[(LOCATIONS.index(str(row["Location"])) + 1) % len(LOCATIONS)]
    return result, len(changed)


def generate(
    out: Path,
    *,
    demands: int = 500,
    resources: int = 500,
    seed: int = SEED,
    dirty_percent: float | None = None,
) -> dict[str, Path]:
    if not 1 <= demands <= MAX_ROWS or not 1 <= resources <= MAX_ROWS:
        raise ValueError(f"Sizes must be between 1 and {MAX_ROWS}")
    if dirty_percent is not None and not 0 < dirty_percent <= 100:
        raise ValueError("--dirty-percent must be above 0 and at most 100")
    rng = random.Random(seed)
    resumes = out / "resumes"
    resumes.mkdir(parents=True, exist_ok=True)
    paths = {"dl": out / "DL.xlsx", "ct": out / "CT.xlsx", "resumes": resumes}
    dl, ct = demand_rows(demands, rng), resource_rows(resources, rng)
    paths["dl"].write_bytes(workbook_bytes(list(DL), dl))
    paths["ct"].write_bytes(workbook_bytes(list(CT), ct))
    for stale in resumes.iterdir():
        if GENERATED_CV.fullmatch(stale.name):
            stale.unlink()
    for index, row in enumerate(ct, start=1):
        write_cv(resumes / f"{row['Email Address']}.docx", cv_text(index, row, rng))
    if dirty_percent is not None:
        changed, _ = dirty_rows(ct, dirty_percent, random.Random(seed + 1))
        paths["ct_dirty"] = out / "CT_dirty.xlsx"
        paths["ct_dirty"].write_bytes(workbook_bytes(list(CT), changed))
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", type=Path, required=True, help="Output folder (created if missing)"
    )
    parser.add_argument("--demands", type=int, default=500)
    parser.add_argument("--resources", type=int, default=500)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument(
        "--dirty-percent",
        type=float,
        help="Also write CT_dirty.xlsx with this percentage of CT rows changed",
    )
    args = parser.parse_args()
    try:
        paths = generate(
            args.out,
            demands=args.demands,
            resources=args.resources,
            seed=args.seed,
            dirty_percent=args.dirty_percent,
        )
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    for name, path in paths.items():
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
