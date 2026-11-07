"""Pure deterministic hybrid scoring (REQ-MAT-01..10, REQ-SCR-01..05)."""

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from ..config_loader import DomainConfig, ScoringTemplate
from ..normalise import normalise_term, tokenise_skills
from .models import (
    BasisResult,
    Component,
    Demand,
    DemandResult,
    Exclusion,
    ExclusionReason,
    ItemResult,
    MatchRun,
    PersonResult,
    RequirementItem,
    Resource,
    SemanticInput,
    SkillBasis,
    SkillEvidence,
)
from .selection import RankingInput, input_key, select_scored_rows

ENGINE_VERSION = "1.1.0"


def normalise_cosine(cos: float, template: ScoringTemplate) -> float:
    """REQ-MAT-01: normalise caller cosine; reject nonfinite scores or invalid thresholds."""
    low, high = template.skills.semantic_low, template.skills.semantic_high
    if not math.isfinite(cos) or high <= low:
        raise ValueError("Cosine must be finite and semantic_high must exceed semantic_low")
    return max(0.0, min(1.0, (cos - low) / (high - low)))


def build_ct_basis(resource: Resource, config: DomainConfig) -> SkillBasis:
    """REQ-IN-24: build an independent CT basis with a globally unique basis ID."""
    tokens = tokenise_skills(resource.ct_skills or "", config.synonyms)
    return SkillBasis(
        f"CT:{resource.resource_id}",
        "CT",
        tuple(SkillEvidence(t.raw, t.normalised, t.canonical, t.raw) for t in tokens.tokens),
        False,
    )


def build_combined_basis(
    resource: Resource, cvs: Sequence[SkillBasis], config: DomainConfig
) -> SkillBasis:
    ct = build_ct_basis(resource, config)
    latest = [basis for basis in cvs if basis.is_latest]
    if not latest:
        return ct
    if len(latest) != 1:
        raise ValueError("Expected one latest CV per resource")
    cv = latest[0]
    kind, version = cv.basis_id.split(":")
    if kind != "CV" or not version.isdecimal():
        raise ValueError("Invalid CV basis ID")
    canonical_ids = {
        normalise_term(skill.canonical): skill.canonical_skill_id
        for skill in cv.skills
        if skill.canonical and skill.canonical_skill_id is not None
    }
    skills: dict[tuple[str, str | int], SkillEvidence] = {}
    for skill in (*ct.skills, *cv.skills):
        canonical = skill.canonical or config.synonyms.resolve(skill.raw)
        canonical_id = skill.canonical_skill_id
        if canonical_id is None and canonical:
            canonical_id = canonical_ids.get(normalise_term(canonical))
        key: tuple[str, str | int] = (
            ("id", canonical_id)
            if canonical_id is not None
            else ("norm", normalise_term(canonical or skill.normalised))
        )
        skills[key] = skill
    return SkillBasis(
        f"CTCV:{resource.resource_id}:{version}", f"CT + {cv.label}", tuple(skills.values()), True
    )


def _recognised(basis: SkillBasis, config: DomainConfig) -> set[str]:
    """REQ-ELIG-01: approved dictionary terms or explicit caller canonical assertions."""
    return {
        normalise_term(skill.canonical or config.synonyms.resolve(skill.normalised) or "")
        for skill in basis.skills
        if skill.canonical or config.synonyms.resolve(skill.normalised)
    }


def eligibility(
    resource: Resource,
    bases: Sequence[SkillBasis],
    config: DomainConfig,
) -> tuple[ExclusionReason, ...]:
    """REQ-ELIG-01..08: collect all global exclusion reasons, never short-circuit."""
    reasons = []
    status = config.bench_statuses.resolve(resource.bench_status or "")
    if status and status.is_excluded:
        reasons.append(ExclusionReason("STATUS_EXCLUDED", f"Excluded status '{status.label}'"))
    if normalise_term(resource.group or "") == "not on bench":
        reasons.append(ExclusionReason("NOT_ON_BENCH", "Group is Not on Bench"))
    if resource.bench_end.status == "valid" and resource.bench_end.value is not None:
        reasons.append(ExclusionReason("BENCH_END_SET", "Bench End Date is set"))
    try:
        config.grades.ordinal(resource.grade or "")
    except ValueError:
        reasons.append(ExclusionReason("NO_GRADE", "Person grade not recorded or unknown"))
    if not any(_recognised(basis, config) for basis in bases):
        reasons.append(ExclusionReason("NO_SKILLS", "No recognised skills recorded in CT or CV"))
    return tuple(reasons)


def _places(raw: str | None, config: DomainConfig) -> set[str]:
    parts = re.split(r"[/,&;]|\s+or\s+", raw or "", flags=re.IGNORECASE)
    return {
        config.locations.aliases.get(key, key) for part in parts if (key := normalise_term(part))
    }


def assess_location(
    demand: Demand,
    resource: Resource,
    config: DomainConfig,
) -> tuple[str, tuple[str, ...]]:
    """REQ-LOC-01, FR-MAT-30..32: classify without excluding."""
    wanted, person = _places(demand.region, config), _places(resource.location, config)
    gaps = []
    if not person:
        gaps.append("Person location not recorded")
    if not wanted or "remote" in wanted:
        classification = "Remote/unspecified"
    elif not person:
        classification = "Location check"
    elif wanted & person:
        classification = "Aligned"
    elif any(
        wanted.intersection(cluster) and person.intersection(cluster)
        for cluster in config.locations.travel_clusters.values()
    ):
        classification = "Potential travel"
    else:
        classification = "Location check"
    if classification in {"Location check", "Potential travel"} and person:
        gaps.append(
            f"{classification}: {resource.location} vs {demand.region or 'Unspecified'} — confirm"
        )
    if demand.onsite_required and classification != "Aligned":
        gaps.append(f"Onsite requirement at {demand.region or 'Unspecified'} — confirm")
    return classification, tuple(gaps)


def round_score(value: float) -> float:
    """REQ-SCR-02: decimal half-up, including 89.95 → 90.0."""
    return float(Decimal(str(value)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def assign_band(score: float, template: ScoringTemplate) -> str:
    """REQ-SCR-02: apply thresholds to the rounded score."""
    rounded = round_score(score)
    for band in ("Strong", "Potential", "Avg"):
        if rounded >= template.bands[band]:
            return band
    return "Weak"


def _item(
    item: RequirementItem,
    basis: SkillBasis,
    semantic: SemanticInput,
    config: DomainConfig,
    template: ScoringTemplate,
) -> ItemResult:
    term = item.skill_term or item.text
    key = normalise_term(term)
    canonical = config.synonyms.resolve(term)
    credit, method, source = 0.0, "not recorded", ""
    for skill in basis.skills:
        skill_key = normalise_term(skill.normalised)
        skill_canonical = skill.canonical or config.synonyms.resolve(skill_key)
        exact = key == skill_key or (
            canonical is not None
            and skill_canonical is not None
            and normalise_term(canonical) == normalise_term(skill_canonical)
        )
        if exact:
            credit = 1.0
            method = "exact" if key == normalise_term(skill.raw) else "synonym"
            source = skill.raw
            if skill.evidence and skill.evidence != skill.raw:
                source += f": {skill.evidence}"
            break
        related = [
            amount
            for equivalent in {key, *config.synonyms.expand(term)}
            for target, amount in config.synonyms.related(equivalent)
            if normalise_term(target) == normalise_term(skill_canonical or skill_key)
        ] + [
            amount
            for equivalent in {skill_key, *config.synonyms.expand(skill.raw)}
            for target, amount in config.synonyms.related(equivalent)
            if normalise_term(target) == normalise_term(canonical or key)
        ]
        if related and template.skills.related_synonym_credit > credit:
            credit, method, source = template.skills.related_synonym_credit, "related", skill.raw
    semantic_credit = min(
        semantic.item_credits.get(item.item_id, 0), template.skills.semantic_item_cap
    )
    if semantic_credit > credit:
        credit, method, source = semantic_credit, "semantic", f"semantic credit {semantic_credit:g}"
    credit = round(credit, 4)
    if credit == 0:
        evidence = f"{item.text}: not recorded — requires validation ({basis.label})"
    else:
        via = f" via {method} {source}" if method != "exact" else f" — {source}"
        evidence = f"{term}{via} ({basis.label})"
        if credit < 1:
            evidence += "; partial — requires validation"
    return ItemResult(item.item_id, item.kind, credit, method, evidence)


def score_basis(
    demand: Demand,
    resource: Resource,
    basis: SkillBasis,
    semantic: SemanticInput,
    config: DomainConfig,
    as_of_date: date,
    *,
    template: ScoringTemplate | None = None,
    has_cv: bool = False,
) -> BasisResult:
    """REQ-IN-24/REQ-SCR-04: score an eligible pair's single basis independently.

    REQ-ELIG-07: unmapped status scores zero in binary mode; tiered mode uses
    the configured Overall upskilling priority, falling back to the unmapped
    priority if that mapping is unavailable. Both modes retain the warning.
    """
    t = template or config.template
    distance = config.grades.distance(resource.grade or "", demand.grade or "")
    status = config.bench_statuses.resolve(resource.bench_status or "")
    priority = status.priority if status and status.priority else t.unmapped_status_priority
    gaps: list[str] = []
    if distance:
        gaps.append(f"Adjacent grade ({resource.grade} vs {demand.grade}) — confirm")
    if not status:
        gaps.append("Unmapped status")
    if status and status.flag_in_gaps:
        gaps.append(f"CV with client caveat: {status.label} — confirm availability")
    if status and status.available_from_bench_start:
        gaps.append("Expected on Bench Soon — availability from Bench Start Date")
    if resource.bench_end.status == "unparseable":
        gaps.append("Bench End Date unparseable — pending review")
    if not has_cv:
        gaps.append("CV not found")
    classification, location_gaps = assess_location(demand, resource, config)
    gaps.extend(location_gaps)
    reference = demand.position_start.value if demand.position_start.status == "valid" else None
    if reference is not None and reference < as_of_date:
        gaps.append(f"Position Start Date {reference.isoformat()} is in the past — confirm")
    if reference is None:
        reference = as_of_date
        gaps.append("Position Start Date not recorded or unparseable — using as_of_date")
    start = resource.bench_start.value if resource.bench_start.status == "valid" else None
    gap_days = max(0, (start - reference).days) if start else None
    if gap_days is None:
        bench_start = t.bench_start.missing
        gaps.append("Bench Start Date not recorded or unparseable — requires validation")
    else:
        bench_start = next(
            (points for day, points in t.bench_start.tiers_days if gap_days <= day),
            t.bench_start.beyond,
        )
        if gap_days:
            reference_label = (
                "Position Start Date" if demand.position_start.status == "valid" else "as_of_date"
            )
            gaps.append(f"Bench Start Date after {reference_label} by {gap_days} days — confirm")
    items = tuple(_item(item, basis, semantic, config, t) for item in demand.requirements)
    mandatory = [item.credit for item in items if item.kind == "mandatory"]
    preferred = [item.credit for item in items if item.kind == "preferred"]
    mand = round(sum(mandatory) / len(mandatory), 4) if mandatory else 1.0
    pref = round(sum(preferred) / len(preferred), 4) if preferred else 1.0
    missing = sum(credit < 0.5 for credit in mandatory)
    for item in items:
        if item.credit < 1:
            prefix = "Mandatory gap: " if item.kind == "mandatory" and item.credit < 0.5 else ""
            gaps.append(prefix + item.evidence)
    w = t.skills.sub_weights
    skills = round(
        t.weights.skills
        * (w.mandatory * mand + w.preferred * pref + w.semantic * semantic.overall),
        4,
    )
    status_points = 0.0
    score_priority = priority
    if t.status_score_mode == "tiered":
        if not status:
            upskilling = config.bench_statuses.resolve("Overall upskilling (New bench)")
            if upskilling and upskilling.priority is not None:
                score_priority = upskilling.priority
        status_points = t.status_tiered_points.get(score_priority, 0)
    elif status and status.code == "AVAILABLE":
        status_points = t.weights.status
    components = {
        "skills": Component(
            skills,
            t.weights.skills,
            (
                ("mandatory_cov", str(mand)),
                ("preferred_cov", str(pref)),
                ("semantic", str(semantic.overall)),
            ),
        ),
        "grade": Component(
            t.grade.exact if distance == 0 else t.grade.adjacent,
            t.weights.grade,
            (("distance", str(distance)),),
        ),
        "location": Component(
            t.location_points[classification],
            t.weights.location,
            (("classification", classification), ("role_location", demand.region or "Unspecified")),
        ),
        "bench_start": Component(
            bench_start,
            t.weights.bench_start,
            (("gap_days", str(gap_days)), ("reference", reference.isoformat())),
        ),
        "bench_end": Component(
            t.bench_end_blank_points,
            t.weights.bench_end,
            (("parse_status", resource.bench_end.status),),
        ),
        "status": Component(
            status_points,
            t.weights.status,
            (
                ("priority", str(priority)),
                ("score_priority", str(score_priority)),
                ("source", resource.bench_status or ""),
            ),
        ),
    }
    total = sum((Decimal(str(component.points)) for component in components.values()), Decimal(0))
    score = float(total.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
    recognised = len(_recognised(basis, config))
    band = assign_band(score, t)
    if recognised <= 1 or not mandatory:
        band = "Insufficient data"
        gaps.append("Insufficient data — at most one recognised skill or no mandatory requirements")
    elif missing and t.mandatory_gap_policy == "cap_band" and band == "Strong":
        band = "Potential"
    end_text = "blank" if resource.bench_end.status == "blank" else "unparseable — pending review"
    caveat = f"; CV with client caveat: {status.label}" if status and status.flag_in_gaps else ""
    eligibility_text = (
        f"Bench End Date {end_text}; grade {resource.grade} within ±1 of {demand.grade}; "
        f"status '{resource.bench_status or ''}' (priority {priority}); "
        f"{recognised} recognised skills{caveat}; subject to review"
    )
    return BasisResult(
        basis.basis_id,
        basis.label,
        basis.is_latest,
        score,
        band,
        components,
        items,
        missing,
        recognised,
        semantic.overall,
        "; ".join(item.evidence for item in items),
        tuple(gaps),
        eligibility_text,
    )


def ranking_key(row: PersonResult) -> tuple[float, int, int, date, int, str, str]:
    """FR-MAT-10/60: deterministic ranking, using rounded score."""
    return input_key(RankingInput.from_result(row))


def run_match(
    demands: Iterable[Demand],
    resources: Iterable[Resource],
    cv_bases: Mapping[str, Sequence[SkillBasis]],
    semantic_inputs: Mapping[tuple[str, str], SemanticInput],
    config: DomainConfig,
    as_of_date: date,
    *,
    template: ScoringTemplate | None = None,
) -> MatchRun:
    """REQ-MAT-03/REQ-ELIG-08: score, deduplicate and shortlist; CV keys are candidate IDs."""
    t = template or config.template
    global_exclusions = []
    pair_exclusions = []
    eligible = []
    for resource in sorted(resources, key=lambda r: r.resource_id):
        cvs = cv_bases.get(resource.candidate_id or resource.resource_id, ())
        basis = build_combined_basis(resource, cvs, config)
        bases = (basis,)
        reasons = eligibility(resource, bases, config)
        if reasons:
            global_exclusions.append(Exclusion(resource.resource_id, None, reasons))
        else:
            eligible.append((resource, bases, basis.is_latest))
    results = []
    pairs_scored = 0
    for demand in sorted(demands, key=lambda d: d.code_requisition):
        rows = []
        flags = []
        try:
            config.grades.ordinal(demand.grade or "")
        except ValueError:
            flags.append("Unknown demand grade — demand not scorable")
        if not flags:
            for resource, bases, has_cv in eligible:
                distance = config.grades.distance(resource.grade or "", demand.grade or "")
                if distance > t.grade.max_distance:
                    pair_exclusions.append(
                        Exclusion(
                            resource.resource_id,
                            demand.code_requisition,
                            (ExclusionReason("GRADE_GAP_GT1", "Grade outside ±1"),),
                        )
                    )
                    continue
                scores = tuple(
                    score_basis(
                        demand,
                        resource,
                        basis,
                        semantic_inputs.get(
                            (demand.code_requisition, basis.basis_id), SemanticInput()
                        ),
                        config,
                        as_of_date,
                        template=t,
                        has_cv=has_cv,
                    )
                    for basis in bases
                )
                best = scores[0]
                status = config.bench_statuses.resolve(resource.bench_status or "")
                priority = (
                    status.priority if status and status.priority else t.unmapped_status_priority
                )
                rows.append(PersonResult(resource, best, scores, priority, distance))
                pairs_scored += 1
        selected = select_scored_rows((RankingInput.from_result(row) for row in rows), t)
        by_id = {row.resource.resource_id: row for row in rows}
        results.append(
            DemandResult(
                demand,
                tuple(by_id[row.resource.resource_id] for row in selected.sufficient),
                tuple(by_id[row.resource.resource_id] for row in selected.insufficient),
                tuple(by_id[row.resource.resource_id] for row in selected.shortlist),
                tuple(by_id[row.resource.resource_id] for row in selected.emails),
                tuple(flags),
                selected.no_suitable,
                selected.weak_only,
            )
        )
    return MatchRun(
        tuple(results),
        tuple(global_exclusions),
        tuple(pair_exclusions),
        len(results),
        pairs_scored,
        sum(r.no_suitable_candidate for r in results),
        sum(r.weak_only for r in results),
        t.name,
        t.version,
        ENGINE_VERSION,
        as_of_date,
    )
