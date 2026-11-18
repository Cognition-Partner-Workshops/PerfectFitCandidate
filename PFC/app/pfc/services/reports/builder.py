import io
import math
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import reportlab  # type: ignore[import-untyped]
from reportlab.graphics.shapes import Drawing, Rect, String  # type: ignore[import-untyped]
from reportlab.lib import colors  # type: ignore[import-untyped]
from reportlab.lib.pagesizes import A4, landscape  # type: ignore[import-untyped]
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet  # type: ignore[import-untyped]
from reportlab.pdfbase import pdfmetrics  # type: ignore[import-untyped]
from reportlab.pdfbase.ttfonts import TTFont  # type: ignore[import-untyped]
from reportlab.pdfgen.canvas import Canvas  # type: ignore[import-untyped]
from reportlab.platypus import (  # type: ignore[import-untyped]
    Flowable,
    LongTable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    TableStyle,
)

from pfc.api.schemas.results import band_label
from pfc.services.matching.export import evidence_parts
from pfc.services.pii import mask_emails_in

from .data import ReportData

DISCLAIMER = "Shortlist for human review, not an automated staffing decision"
METHOD = (
    "Scoring method: skills 70 / grade 10 / location 5 / bench start 5 / bench end 5 / status 5 "
    "(70/10/5/5/5/5). Grade eligibility: exact or ±1 grade. "
    "Bands: Strong ≥ 90; Potential 70–89; Avg 61–69; Weak < 61; Insufficient data. "
    "Scores are relative metrics, not probabilities or automated staffing decisions (REQ-SCR-05)."
)
_FONT_LOCK = threading.Lock()


def register_fonts() -> None:
    with _FONT_LOCK:
        if "PFC" not in pdfmetrics.getRegisteredFontNames():
            fonts = Path(reportlab.__file__).parent / "fonts"
            pdfmetrics.registerFont(TTFont("PFC", str(fonts / "Vera.ttf")))
            pdfmetrics.registerFont(TTFont("PFC-Bold", str(fonts / "VeraBd.ttf")))
            pdfmetrics.registerFontFamily(
                "PFC", normal="PFC", bold="PFC-Bold", italic="PFC", boldItalic="PFC-Bold"
            )


def display(value: Any) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        return "Not recorded"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, list):
        return "; ".join(display(v) for v in value) or "None"
    return mask_emails_in(str(value))


def match_pct(score: float) -> str:
    return f"{math.floor(score + 0.5)} %"


def date_text(value: str | None) -> str:
    if not value:
        return "Not recorded"
    matched = re.match(r"^(\d{4})-(\d{2})-(\d{2})", value)
    if matched and 1 <= int(matched[2]) <= 12:
        months = (
            "Jan",
            "Feb",
            "Mar",
            "Apr",
            "May",
            "Jun",
            "Jul",
            "Aug",
            "Sep",
            "Oct",
            "Nov",
            "Dec",
        )
        return f"{matched[3]} {months[int(matched[2]) - 1]} {matched[1]}"
    return f"Unparseable: {display(value)}"


@dataclass
class Chart:
    title: str
    columns: list[str]
    rows: list[list[str]]


def chart(title: str, columns: list[str], rows: list[list[Any]]) -> Chart:
    return Chart(title, columns, [[display(v) for v in row] for row in rows])


def catalogue(data: ReportData) -> dict[str, list[Chart]]:
    if not data.panels:
        return {}
    s, d, b, m, e = (
        data.panels[key] for key in ("summary", "demand", "bench", "match", "extended")
    )

    def count_chart(title: str, rows: list[dict[str, Any]]) -> Chart:
        return chart(title, ["Label", "Count"], [[r["label"], r["count"]] for r in rows])

    def matrix(title: str, rows: list[dict[str, Any]]) -> Chart:
        return chart(
            title, ["Row", "Column", "Count"], [[r["row"], r["column"], r["count"]] for r in rows]
        )

    supply = chart(
        "DB-15 Demand vs eligible bench supply",
        ["Skill", "Demands", "Capacity Y", "Capacity N"],
        [[r["skill"], r["demandCount"], r["supplyY"], r["supplyN"]] for r in b["demandVsSupply"]],
    )
    unsuitable_pct = (
        s["rolesWithNoSuitableCandidate"] / s["requisitionsAssessed"] * 100
        if s["requisitionsAssessed"]
        else 0
    )
    summary = chart(
        "DB-01 Summary KPI strip",
        ["Metric", "Value"],
        [
            ["Requisitions assessed", s["requisitionsAssessed"]],
            ["Open requisitions", s["openRequisitions"]],
            ["Role-to-person combinations", s["roleToPersonCombinations"]],
            ["Roles with no suitable candidate", s["rolesWithNoSuitableCandidate"]],
            ["Weak-only requisitions", s["weakOnlyRequisitions"]],
            ["Data-quality limitations", s["dataQualityIssues"]["total"]],
            ["DL limitations", s["dataQualityIssues"]["dl"]],
            ["CT limitations", s["dataQualityIssues"]["ct"]],
            ["Eligible bench", s["eligibleBench"]],
            ["Excluded bench", s["excludedBench"]],
            ["Match coverage", f"{display(s['matchCoveragePct'])} %"],
            ["Emails sent ≤ 24 h", f"{display(s['emailsWithin24hPct'])} %"],
            [
                "Roles without suitable candidate (% of assessed)",
                f"{unsuitable_pct:.1f} %",
            ],
        ],
    )
    return {
        "summary": [
            summary,
            chart("DB-11 Match coverage", ["Coverage %"], [[f"{display(m['coveragePct'])} %"]]),
            count_chart("DB-17 Shortlist funnel", e["shortlistFunnel"]),
        ],
        "demand": [
            chart(
                "DB-02 Open demands by BU / region",
                ["BU", "Region", "Count"],
                [[r["bu"], r["region"], r["count"]] for r in d["openByBuRegion"]],
            ),
            count_chart("DB-03 Top 5 skills in demand", d["topSkills"]),
            count_chart("DB-04 Top 3 accounts by JD count", d["topAccounts"]),
            count_chart("DB-05 Demand start-date pipeline", d["startPipeline"]),
            supply,
            count_chart("DB-31 Grade → JD count", d["gradeToJdCount"]),
        ],
        "bench": [
            count_chart("DB-06 Top 5 skills in pool", b["topSkills"]),
            chart(
                "DB-07 Grade histogram",
                ["Grade", "Bench", "Demand"],
                [[r["grade"], r["bench"], r["demand"]] for r in b["gradeHistogram"]],
            ),
            chart(
                "DB-08 Bench timeline",
                ["Person", "Bench Start", "Days"],
                [[r["name"], date_text(r["start"]), r["days"]] for r in b["timeline"]],
            ),
            count_chart("DB-09 Bench aging", b["aging"]),
            chart(
                "DB-10 Bench status distribution",
                ["Status", "People", "Excluded"],
                [[r["status"], r["count"], r["excluded"]] for r in b["statusDistribution"]],
            ),
            chart(
                "DB-14 Demand-grade × eligible-bench-grade",
                ["Demand grade", "Bench grade", "Pairs", "Demands", "Bench", "Relation"],
                [
                    [
                        r["row"],
                        r["column"],
                        r["count"],
                        r["demandCount"],
                        r["benchCount"],
                        r["relation"],
                    ]
                    for r in e["gradeMatrix"]
                ],
            ),
            matrix("DB-20 Readiness group × bench status", e["readinessStatus"]),
            count_chart("DB-26 Exclusion reasons", e["exclusionReasons"]),
            count_chart("DB-27 Candidate type split", e["candidateTypes"]),
        ],
        "match": [
            count_chart("DB-12 Band distribution", m["bandDistribution"]),
            count_chart("DB-13 Email status counts", m["emailStatusCounts"]),
            chart(
                "DB-16 Match-score distribution (thresholds 61 / 70 / 90)",
                ["Lower", "Upper", "All pairs", "Top 5"],
                [
                    [r["lower"], r["upper"], r["allPairs"], r["top5"]]
                    for r in e["scoreHistogram"]["bins"]
                ],
            ),
            chart(
                "DB-19 Weekly time-to-email",
                ["Week", "Sent", "Median hours", "Within 24 h %"],
                [
                    [r["week"], r["sentCount"], r["medianHours"], r["within24hPct"]]
                    for r in e["emailTrend"]
                ],
            ),
            chart(
                "DB-25 Auto-match accuracy (confirmation proxy)",
                [
                    "Run",
                    "Confirmed demands",
                    "Confirmed pairs",
                    "Hits",
                    "Hit rate at 5",
                    "Precision at 5",
                ],
                [
                    [
                        r["runId"],
                        r["confirmedDemands"],
                        r["confirmedPairs"],
                        r["hits"],
                        r["hitRateAt5"],
                        r["precisionAt5"],
                    ]
                    for r in e["accuracy"]
                ],
            ),
            chart(
                "DB-28 Candidate contention",
                ["Person", "Email", "Demands", "Requisitions"],
                [
                    [r["name"], r["emailMasked"], r["demandCount"], r["requisitions"]]
                    for r in e["contention"]
                ],
            ),
            chart(
                "DB-30 Override rate per run",
                ["Run", "Pairs", "Overridden pairs", "Rate %"],
                [
                    [r["runId"], r["pairCount"], r["overriddenPairs"], r["ratePct"]]
                    for r in e["overrideRates"]
                ],
            ),
            chart(
                "DB-30 Override actions",
                ["Run", "Action", "Count"],
                [
                    [r["runId"], a["label"], a["count"]]
                    for r in e["overrideRates"]
                    for a in r["byType"]
                ],
            ),
        ],
        "unmatched": [
            chart(
                "DB-18 Unmatched demands",
                ["Requisition", "Start date", "Reasons"],
                [
                    [r["codeRequisition"], date_text(r["positionStartDate"]), r["reasonCodes"]]
                    for r in e["unmatchedDemands"]
                ],
            )
        ],
        "extended-other": [
            matrix("DB-21 Demand region × person location", e["locationMatrix"]),
            matrix("DB-22 Skill co-occurrence", e["skillCooccurrence"]),
            chart(
                "DB-24 Upskilling candidates vs demand",
                ["Person", "Email", "Matched skills", "Missing skills", "Best Match %"],
                [
                    [
                        r["name"],
                        r["emailMasked"],
                        r["matchedSkills"],
                        r["missingSkills"],
                        match_pct(r["bestScore"]) if r["bestScore"] is not None else None,
                    ]
                    for r in e["upskilling"]
                ],
            ),
            count_chart("DB-29 Demand aging", e["demandAging"]["buckets"]),
            chart(
                "DB-29 Aging checks",
                ["Metric", "Count"],
                [
                    ["Unknown start", e["demandAging"]["unknownStart"]],
                    ["Past start without match", e["demandAging"]["pastStartWithoutMatch"]],
                ],
            ),
        ],
        "data-quality-method": [
            chart(
                "DB-23 Validation checks",
                ["Check", "Passed", "Detail"],
                [[r["label"], r["passed"], r["detail"]] for r in m["validation"]],
            )
        ],
    }


class NumberedCanvas(Canvas):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        Canvas.__init__(self, *args, **kwargs)
        self.states: list[dict[str, Any]] = []

    def showPage(self) -> None:
        self.states.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:
        total = len(self.states)
        for state in self.states:
            self.__dict__.update(state)
            self.setFont("PFC", 6.5)
            self.setFillColor(colors.HexColor("#475467"))
            self.drawString(32, 23, DISCLAIMER)
            self.drawRightString(self._pagesize[0] - 32, 23, f"Page {self._pageNumber}/{total}")
            Canvas.showPage(self)
        Canvas.save(self)


class ReportParagraph(Paragraph):
    _wrapped_width: float | None = None

    def wrap(self, availWidth: float, availHeight: float) -> tuple[float, float]:
        if self._wrapped_width == availWidth and hasattr(self, "blPara"):
            return float(self.width), float(self.height)
        width, height = super().wrap(availWidth, availHeight)
        self._wrapped_width = availWidth
        return float(width), float(height)


def drawing(item: Chart, width: float) -> Drawing | None:
    if item.title.startswith("DB-01") or len(item.rows) < 2:
        return None
    points: list[tuple[str, int, float]] = []
    selected = {
        "DB-02": [2],
        "DB-03": [1],
        "DB-04": [1],
        "DB-05": [1],
        "DB-06": [1],
        "DB-07": [1, 2],
        "DB-08": [2],
        "DB-09": [1],
        "DB-10": [1],
        "DB-12": [1],
        "DB-13": [1],
        "DB-14": [2],
        "DB-15": [1, 2, 3],
        "DB-16": [2, 3],
        "DB-17": [1],
        "DB-18": [],
        "DB-19": [2],
        "DB-20": [2],
        "DB-21": [2],
        "DB-22": [2],
        "DB-23": [],
        "DB-24": [4],
        "DB-25": [4, 5],
        "DB-26": [1],
        "DB-27": [1],
        "DB-28": [2],
        "DB-29": [1],
        "DB-30": [3],
        "DB-31": [1],
    }.get(item.title.split()[0], [])
    if item.title.startswith("DB-30 Override actions"):
        selected = [2]
    for row in item.rows:
        for index in selected:
            value = row[index]
            try:
                number = float(value.removesuffix(" %"))
            except ValueError:
                continue
            label = " · ".join(row[:2] if item.columns[0] in {"Row", "Demand grade"} else row[:1])
            points.append((label, index, number))
    if len(points) < 2:
        return None
    series = {index for _, index, _ in points}
    points = points[:20]
    height = 18 * len(points) + 20
    result = Drawing(width, height)
    maximum = max(1, max(n for _, _, n in points))
    for i, (label, index, plotted) in enumerate(points):
        if len(series) > 1:
            label += " · " + item.columns[index]
        y = height - 20 - i * 18
        result.add(String(0, y + 3, display(label)[:43], fontName="PFC", fontSize=7))
        result.add(
            Rect(
                width * 0.48,
                y,
                max(0, plotted) / maximum * width * 0.38,
                10,
                fillColor=colors.HexColor("#247c99"),
                strokeColor=None,
            )
        )
        result.add(String(width * 0.88, y + 2, display(plotted), fontName="PFC", fontSize=7))
    return result


def build_pdf(data: ReportData) -> bytes:
    register_fonts()
    output = io.BytesIO()
    size = landscape(A4) if data.request.orientation == "landscape" else A4
    doc = SimpleDocTemplate(
        output,
        pagesize=size,
        leftMargin=32,
        rightMargin=32,
        topMargin=43,
        bottomMargin=44,
        title="PerfectFitCandidate report",
        author=display(data.generated_by),
    )
    styles = getSampleStyleSheet()
    for style in styles.byName.values():
        style.fontName = "PFC"
    styles["Title"].fontName = styles["Heading1"].fontName = styles["Heading2"].fontName = (
        "PFC-Bold"
    )
    small = ParagraphStyle(
        "values", fontName="PFC", fontSize=7, leading=10, spaceAfter=2, splitLongWords=True
    )
    kpi_value = ParagraphStyle("kpi-value", fontName="PFC-Bold", fontSize=16, leading=20)
    result_style = ParagraphStyle(
        "result", fontName="PFC", fontSize=6.5, leading=8, splitLongWords=True
    )
    evidence_style = ParagraphStyle(
        "evidence",
        fontName="PFC",
        fontSize=6,
        leading=7.5,
        splitLongWords=True,
        textColor=colors.HexColor("#475467"),
    )
    story: list[Flowable] = []

    def paragraph(text: Any, style: Any = small) -> Paragraph:
        return ReportParagraph(escape(display(text)).replace("\n", "<br/>"), style)

    def table(columns: list[str], rows: list[list[str]]) -> None:
        if not rows:
            story.append(paragraph("No data for this selection."))
            return
        values = [[paragraph(v) for v in columns]] + [[paragraph(v) for v in row] for row in rows]
        t = LongTable(
            values, colWidths=[doc.width / len(columns)] * len(columns), repeatRows=1, hAlign="LEFT"
        )
        t.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8f1f5")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.HexColor("#98a2b3")),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        story.append(t)

    def tiles(item: Chart) -> None:
        cells = [
            [paragraph(row[0]), paragraph(row[1], kpi_value)]
            if len(row) > 1
            else [paragraph(item.columns[0]), paragraph(row[0], kpi_value)]
            for row in item.rows
        ]
        columns = 3 if len(cells) > 1 else 1
        values = [
            cells[i : i + columns] + [""] * (columns - len(cells[i : i + columns]))
            for i in range(0, len(cells), columns)
        ]
        grid = LongTable(values, colWidths=[doc.width / columns] * columns, hAlign="LEFT")
        grid.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#e8f1f5")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("BOX", (0, 0), (-1, -1), 0.5, colors.white),
                    ("INNERGRID", (0, 0), (-1, -1), 3, colors.white),
                    ("TOPPADDING", (0, 0), (-1, -1), 8),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
                ]
            )
        )
        story.append(grid)

    title = (
        "Dashboard analytics report"
        if data.request.report == "dashboard"
        else "Match results report"
    )
    story.append(paragraph(title, styles["Title"]))
    story.append(paragraph(f"Generated by: {data.generated_by}"))
    generated = data.generated_at
    timestamp = f"{date_text(generated.isoformat())} {generated:%H:%M} {generated.tzname() or ''}"
    story.append(paragraph(f"Generated at: {timestamp.strip()}"))
    query = (
        data.request.dashboard_filters()
        if data.request.report == "dashboard"
        else data.request.results_query()
    )
    applied = query.model_dump(by_alias=True, exclude_none=True, exclude_defaults=True)
    story.append(
        paragraph(
            "Filters applied: "
            + (
                "; ".join(
                    f"{re.sub(r'(?<!^)(?=[A-Z])', ' ', k).lower()}: {display(v)}"
                    for k, v in applied.items()
                )
                or "All (unfiltered)"
            )
        )
    )
    story.append(paragraph("Scope: " + data.request.scope))
    for run in data.runs:
        model = run["embedding_model"]
        story.append(
            paragraph(
                f"Run #{run['run_id']} · Template v{run['template_version']} "
                f"· LLM {run['llm_model']} "
                f"· Engine {run['engine_version']} · Embedding {display(model.get('name'))} / "
                f"{display(model.get('pipeline_version'))} "
                f"· SHA-256 {display(model.get('file_sha256'))}"
            )
        )
    if not data.runs:
        story.append(paragraph("Contributing runs: none"))
    table(
        ["Source", "File", "SHA-256"],
        [[s["kind"], display(s["file_name"]), s["file_sha256"]] for s in data.sources],
    )
    story.append(Spacer(1, 16))
    story.append(paragraph(DISCLAIMER, styles["Heading2"]))
    story.append(
        paragraph(
            "Contacts are always masked. Generated locally without LLM calls or network access."
        )
    )
    if "cover" in data.request.sections:
        story.append(PageBreak())
    sections = catalogue(data)
    for section in data.request.sections:
        if section not in sections:
            continue
        story.append(paragraph(section.replace("-", " ").title(), styles["Heading1"]))
        for item in sections[section]:
            story.append(paragraph(item.title, styles["Heading2"]))
            if item.title.startswith("DB-01") or len(item.rows) == 1 and len(item.columns) <= 2:
                tiles(item)
                story.append(Spacer(1, 12))
                continue
            if (graphic := drawing(item, doc.width)) is not None:
                story.append(graphic)
                story.append(
                    paragraph("Chart preview: first 20 values; complete values table below.")
                )
            table(item.columns, item.rows)
            story.append(Spacer(1, 12))
        if section == "match":
            story.append(
                paragraph(
                    "Accuracy uses explicit pin/include or email approval as a confirmation proxy, "
                    "not automatic shortlisting. Hit rate at 5 and precision at 5 "
                    "are ratios (0–1); "
                    "missing confirmation is Not recorded."
                )
            )
        if section == "data-quality-method":
            story.append(paragraph(METHOD))
        story.append(PageBreak())
    if data.request.report == "match_results":
        story.append(paragraph("Current match results", styles["Heading1"]))
        story.append(paragraph(f"Rows: {len(data.results)} of {data.total}"))
        if len(data.results) < data.total:
            story.append(
                paragraph(
                    f"truncated: {len(data.results)} of {data.total} "
                    f"(maximum 5000; selected offset {data.request.results_query().offset})",
                    styles["Heading2"],
                )
            )
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in data.results:
            groups.setdefault(row["requisition"], []).append(row)
        columns = [
            "Requisition",
            "Rank",
            "Name",
            "Email (masked)",
            "Demand grade",
            "Person grade",
            "Location",
            "Match %",
            "Band",
            "Location assessment",
        ]
        proportions = [0.1, 0.045, 0.13, 0.16, 0.065, 0.065, 0.13, 0.07, 0.09, 0.145]
        for requisition, rows in groups.items():
            first = rows[0]
            region = (
                display(first["role_location"])
                if str(first["role_location"] or "").strip()
                else "Unspecified"
            )
            heading = (
                f"Requisition {requisition} · Account {display(first['account'])} "
                f"· Role {display(first['role'])}"
            )
            metadata = paragraph(f"Start date {date_text(first['start_date'])} · Region {region}")
            values: list[list[Any]] = [
                [paragraph(heading, styles["Heading2"])] + [""] * 9,
                [metadata] + [""] * 9,
                [paragraph(c, result_style) for c in columns],
            ]
            commands: list[tuple[Any, ...]] = [
                ("SPAN", (0, 0), (-1, 0)),
                ("SPAN", (0, 1), (-1, 1)),
                ("NOSPLIT", (0, 0), (-1, 3)),
                ("BACKGROUND", (0, 2), (-1, 2), colors.HexColor("#e8f1f5")),
                ("FONTNAME", (0, 0), (-1, -1), "PFC"),
                ("FONTSIZE", (0, 0), (-1, -1), result_style.fontSize),
                ("LEADING", (0, 0), (-1, -1), result_style.leading),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
            for row in rows:
                fields = [
                    row["requisition"],
                    row["rank"],
                    row["potential_person"],
                    row["person_email"],
                    row["demand_grade"],
                    row["person_grade"],
                    row["person_location"],
                    match_pct(row["score"]),
                    band_label(row["effective_band"]),
                    row["location_assessment"],
                ]
                cells: list[Any] = []
                for value, proportion in zip(fields, proportions, strict=True):
                    text = display(value)
                    width = doc.width * proportion - 6
                    if (
                        "\n" not in text
                        and pdfmetrics.stringWidth(text, "PFC", result_style.fontSize) <= width
                    ):
                        cells.append(text)
                    else:
                        cells.append(paragraph(text, result_style))
                values.append(cells)
                evidence_text = "\n".join(
                    label + ": " + "; ".join(evidence_parts(row[key]))
                    for key, label in (
                        ("matching_evidence", "Matching evidence"),
                        ("gaps_checks", "Gaps / Checks"),
                        ("eligibility_evidence", "Eligibility evidence"),
                    )
                )
                evidence_row = len(values)
                evidence_cell = paragraph(evidence_text, evidence_style)
                row_height = max(
                    cell.wrap(doc.width * p - 6, doc.height)[1]
                    if isinstance(cell, Paragraph)
                    else result_style.leading
                    for cell, p in zip(values[-1], proportions, strict=True)
                )
                evidence_height = evidence_cell.wrap(doc.width - 6, doc.height)[1]
                values.append([evidence_cell] + [""] * 9)
                commands.extend(
                    [
                        ("SPAN", (0, evidence_row), (-1, evidence_row)),
                        (
                            "LINEBELOW",
                            (0, evidence_row),
                            (-1, evidence_row),
                            0.25,
                            colors.HexColor("#d0d5dd"),
                        ),
                    ]
                )
                if row_height + evidence_height < doc.height - 60:
                    commands.append(("NOSPLIT", (0, evidence_row - 1), (-1, evidence_row)))
            results_table = LongTable(
                values,
                colWidths=[doc.width * p for p in proportions],
                repeatRows=[2],
                hAlign="LEFT",
                splitInRow=1,
            )
            results_table.setStyle(TableStyle(commands))
            story.append(results_table)
            story.append(Spacer(1, 8))
        if not data.results:
            story.append(paragraph("No results for this selection."))
    while story and isinstance(story[-1], PageBreak):
        story.pop()

    def header(canvas: Any, document: Any) -> None:
        canvas.saveState()
        canvas.setFont("PFC", 7)
        canvas.setFillColor(colors.HexColor("#475467"))
        canvas.drawString(32, size[1] - 23, "PerfectFitCandidate · " + title)
        canvas.restoreState()

    doc.build(story, onFirstPage=header, onLaterPages=header, canvasmaker=NumberedCanvas)
    return output.getvalue()
