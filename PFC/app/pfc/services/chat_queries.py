"""Read-only lookups and deterministic answers with masked row-level citations."""

import sqlite3
from collections import Counter, defaultdict
from datetime import date
from urllib.parse import urlencode

from pfc.api.schemas.chat import ChatAnswer, ChatSource
from pfc.domain.normalise import normalise_term, tokenise_skills
from pfc.services.chat_intent import ChatMask, Lookup
from pfc.services.matching.current import read_current_runs
from pfc.services.occ.synonyms import valid_synonym_table

NOT_FOUND = "I couldn't find that in PFC data."
UNSUPPORTED = (
    "Try a question about open demand counts, top demand skills, "
    "bench skills, or a stored match rank."
)


def _demand_filter(lookup: Lookup, today: date, mask: ChatMask) -> tuple[str, list[str], str]:
    conditions = ["d.lifecycle='open'"]
    params = []
    scope = "open demands"
    if lookup.scope_type and lookup.scope_term:
        column, label = {"bu": ("bu", "business unit"), "account": ("account_name", "account")}[
            lookup.scope_type
        ]
        conditions.append(f"d.{column} = ? COLLATE NOCASE")
        params.append(lookup.scope_term)
        scope += f" for {label} {mask.mask(lookup.scope_term)}"
    if lookup.period == "this_month":
        month = today.isoformat()[:7]
        conditions.append("substr(d.position_start_date, 1, 7) = ?")
        params.append(month)
        scope += f" starting in {month}"
    return " AND ".join(conditions), params, scope


def _demand_source(row: sqlite3.Row, mask: ChatMask, **extra: str) -> ChatSource:
    try:
        start_date = date.fromisoformat(row["position_start_date"] or "").isoformat()
    except ValueError:
        start_date = "Not recorded"
    return ChatSource(
        kind="demand",
        record_id=row["demand_id"],
        label=mask.mask(row["code_requisition"]),
        link=f"/demands?demand={row['demand_id']}",
        values={"Start date": start_date, **extra},
    )


def _demand_count(
    db: sqlite3.Connection, lookup: Lookup, today: date, mask: ChatMask
) -> ChatAnswer:
    conditions, params, scope = _demand_filter(lookup, today, mask)
    rows = db.execute(
        "SELECT demand_id,code_requisition,position_start_date "
        f"FROM demands d WHERE {conditions} ORDER BY d.code_requisition",
        params,
    ).fetchall()
    if not rows:
        return ChatAnswer(outcome="no_data", text=f"{NOT_FOUND} Count of {scope}: 0.")
    return ChatAnswer(
        outcome="answered",
        text=f"Count of {scope}: {len(rows)}.",
        sources=[_demand_source(row, mask) for row in rows],
    )


def _demand_skills(
    db: sqlite3.Connection, lookup: Lookup, today: date, mask: ChatMask
) -> ChatAnswer:
    conditions, params, scope = _demand_filter(lookup, today, mask)
    synonyms = valid_synonym_table(db)
    by_demand: dict[int, set[str]] = defaultdict(set)
    demands: dict[int, sqlite3.Row] = {}
    labels: dict[str, str] = {}
    for row in db.execute(
        "SELECT d.demand_id,d.code_requisition,d.position_start_date,q.jd_req_id,"
        "i.text,s.name FROM demands d "
        "JOIN jd_requirements q ON q.demand_version_id=d.current_version_id "
        "JOIN jd_requirement_items i USING(jd_req_id) "
        "LEFT JOIN skills s ON s.skill_id=i.canonical_skill_id AND s.is_active=1 "
        f"WHERE {conditions} AND q.status='confirmed' "
        "AND i.kind IN ('mandatory_skill','preferred_skill') "
        "ORDER BY d.code_requisition,i.item_id",
        params,
    ):
        original = row["name"] or synonyms.resolve(row["text"]) or row["text"]
        name = mask.mask(original)
        key = normalise_term(name)
        if key and " masked]" not in name:
            labels.setdefault(key, name)
            by_demand[row["demand_id"]].add(key)
            demands[row["demand_id"]] = row
    counts = Counter(key for keys in by_demand.values() for key in keys)
    if not counts:
        return ChatAnswer(outcome="no_data", text=f"{NOT_FOUND} No confirmed skills for {scope}.")
    top = sorted(counts, key=lambda key: (-counts[key], key))[:5]
    skills = ", ".join(f"{labels[key]} ({counts[key]})" for key in top)
    return ChatAnswer(
        outcome="answered",
        text=f"Top confirmed skills by number of {scope}: {skills}.",
        sources=[
            _demand_source(
                demands[demand_id],
                mask,
                **{
                    "Confirmed JD": f"#{demands[demand_id]['jd_req_id']}",
                    "Skills counted": ", ".join(labels[key] for key in sorted(keys)),
                },
            )
            for demand_id, keys in by_demand.items()
        ],
    )


def _bench_skill(db: sqlite3.Connection, lookup: Lookup, mask: ChatMask) -> ChatAnswer:
    assert lookup.skill is not None
    synonyms = valid_synonym_table(db)
    name = synonyms.resolve(lookup.skill) or lookup.skill
    key = normalise_term(name)
    grades = {row["code"].casefold(): row["ordinal"] for row in db.execute("SELECT * FROM grades")}
    low = grades.get(lookup.grade_from.casefold()) if lookup.grade_from else None
    high = grades.get(lookup.grade_to.casefold()) if lookup.grade_to else low
    if lookup.grade_from and (low is None or high is None or low > high):
        return ChatAnswer(outcome="no_data", text=f"{NOT_FOUND} Check the grade or grade range.")
    grade_scope = ""
    if lookup.grade_from:
        grade_scope = f" at grade {mask.mask(lookup.grade_from.upper())}"
        if lookup.grade_to:
            grade_scope += f" through {mask.mask(lookup.grade_to.upper())}"
    seen: dict[int, sqlite3.Row] = {}
    evidence: dict[int, set[str]] = defaultdict(set)
    if key:
        for row in db.execute(
            "SELECT r.resource_id,r.candidate_id,r.grade,r.bench_status_code,r.skills_raw "
            "FROM resources r LEFT JOIN grades g ON g.code=r.grade "
            "WHERE r.lifecycle='active' AND r.eligibility IN ('eligible','eligible_caveat') "
            "AND (? IS NULL OR g.ordinal BETWEEN ? AND ?) ORDER BY r.resource_id",
            (low, low, high),
        ):
            tokens = tokenise_skills(row["skills_raw"] or "", synonyms).tokens
            if any(normalise_term(token.canonical or token.normalised) == key for token in tokens):
                seen[row["resource_id"]] = row
                evidence[row["resource_id"]].add("CT")
        for row in db.execute(
            "SELECT r.resource_id,r.candidate_id,r.grade,r.bench_status_code,"
            "rs.skill_raw,rs.cv_version_id,v.version_no,s.name "
            "FROM resources r JOIN resource_skills rs USING(resource_id) "
            "LEFT JOIN grades g ON g.code=r.grade "
            "JOIN cv_versions v ON v.cv_version_id=rs.cv_version_id "
            "LEFT JOIN skills s ON s.skill_id=rs.canonical_skill_id AND s.is_active=1 "
            "WHERE r.lifecycle='active' AND r.eligibility IN ('eligible','eligible_caveat') "
            "AND (? IS NULL OR g.ordinal BETWEEN ? AND ?) "
            "AND rs.source='CV' AND v.is_latest=1 AND v.extraction_removed_at IS NULL "
            "AND v.candidate_id=r.candidate_id ORDER BY r.resource_id,rs.resource_skill_id",
            (low, low, high),
        ):
            if (
                normalise_term(
                    synonyms.resolve(row["skill_raw"]) or row["name"] or row["skill_raw"]
                )
                == key
            ):
                resource_id = row["resource_id"]
                seen[resource_id] = row
                evidence[resource_id].add(f"CV v{row['version_no']} (#{row['cv_version_id']})")
    skill = mask.mask(name)
    if not seen:
        return ChatAnswer(
            outcome="no_data",
            text=f"{NOT_FOUND} No eligible active bench resources with {skill}{grade_scope}.",
        )
    return ChatAnswer(
        outcome="answered",
        text=f"Eligible active bench resources with {skill}{grade_scope}: {len(seen)}.",
        sources=[
            ChatSource(
                kind="resource",
                record_id=resource_id,
                label=f"Resource #{resource_id}",
                link=f"/candidates/{row['candidate_id']}" if row["candidate_id"] else "/bench",
                values={
                    "Grade": mask.mask(row["grade"] or "Not recorded"),
                    "Status": mask.mask(row["bench_status_code"] or "Not mapped"),
                    "Skill": skill,
                    "Evidence": ", ".join(sorted(evidence[resource_id])),
                },
            )
            for resource_id, row in sorted(seen.items())
        ],
    )


def _match_rank(db: sqlite3.Connection, lookup: Lookup, mask: ChatMask) -> ChatAnswer:
    demand = db.execute(
        "SELECT demand_id,code_requisition FROM demands "
        "WHERE lifecycle='open' AND code_requisition=? COLLATE NOCASE",
        (lookup.requisition,),
    ).fetchone()
    run_id = read_current_runs(db).get(demand["demand_id"]) if demand else None
    if run_id is None:
        return ChatAnswer(
            outcome="no_data", text=f"{NOT_FOUND} No completed assessment for that open demand."
        )
    row = db.execute(
        "SELECT result_id,resource_id,rank,score,band,mandatory_missing FROM match_results "
        "WHERE run_id=? AND demand_id=? AND (? IS NULL OR rank=?) "
        "AND (? IS NULL OR resource_id=?) ORDER BY result_id LIMIT 1",
        (
            run_id,
            demand["demand_id"],
            lookup.rank,
            lookup.rank,
            lookup.resource_id,
            lookup.resource_id,
        ),
    ).fetchone()
    if row is None:
        return ChatAnswer(
            outcome="no_data",
            text=f"{NOT_FOUND} The latest completed assessment has no matching result.",
        )
    result_id = row["result_id"]
    breakdown = {
        part["component"]
        .replace("_", " ")
        .capitalize(): f"{part['points']:g}/{part['max_points']:g}"
        for part in db.execute(
            "SELECT component,points,max_points FROM score_breakdowns "
            "WHERE result_id=? ORDER BY component",
            (result_id,),
        )
    }
    req = mask.mask(demand["code_requisition"])
    score = f"{row['score']:g}/100"
    text = (
        f"In completed run #{run_id}, Resource #{row['resource_id']} was ranked #{row['rank']} "
        f"for {req} with a score of {score} ({row['band']})."
    )
    if breakdown:
        text += " The stored scoring components are shown in the source."
    return ChatAnswer(
        outcome="answered",
        text=text,
        sources=[
            ChatSource(
                kind="match_result",
                record_id=result_id,
                label=f"Match result #{result_id}",
                link="/matches?" + urlencode({"req": req})
                if req == demand["code_requisition"]
                else "/matches",
                values={
                    "Requisition": req,
                    "Run": f"#{run_id}",
                    "Resource": f"#{row['resource_id']}",
                    "Rank": f"#{row['rank']}",
                    "Score": score,
                    "Band": row["band"],
                    "Mandatory gaps": str(row["mandatory_missing"]),
                    **breakdown,
                },
            )
        ],
    )


def answer_lookup(db: sqlite3.Connection, lookup: Lookup, today: date) -> ChatAnswer:
    """Run inside a read-only snapshot. Only trusted code chooses or assembles SQL."""
    mask = ChatMask(db)
    match lookup.intent:
        case "demand_count":
            return _demand_count(db, lookup, today, mask)
        case "demand_skills":
            return _demand_skills(db, lookup, today, mask)
        case "bench_skill":
            return _bench_skill(db, lookup, mask)
        case "match_rank":
            return _match_rank(db, lookup, mask)
        case "unsupported":
            return ChatAnswer(outcome="unsupported", text=UNSUPPORTED)
