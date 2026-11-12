"""Refresh derived resource eligibility using matching's current CT and CV bases."""

import sqlite3
from collections.abc import Iterable

from pfc.domain.scoring import build_combined_basis, eligibility
from pfc.services.matching.cv_bases import load_cv_bases, load_resources


def refresh_resource_eligibility(
    db: sqlite3.Connection, resource_ids: Iterable[int] | None = None
) -> int:
    selected = sorted(set(resource_ids)) if resource_ids is not None else None
    if selected == []:
        return 0

    resources = load_resources(db)
    if selected is not None:
        wanted = set(selected)
        resources = [resource for resource in resources if int(resource.resource_id) in wanted]
    if not resources:
        return 0

    from pfc.services.ingestion.repository import domain_config

    config = domain_config(db)
    cv_bases = load_cv_bases(db, config)
    changed = 0
    for resource in resources:
        combined = build_combined_basis(
            resource, cv_bases.get(resource.candidate_id or "", []), config
        )
        reasons = eligibility(resource, [combined], config)
        current_eligibility = "excluded" if reasons else "eligible"
        exclusion_reason = ",".join(reason.code for reason in reasons) or None
        current = db.execute(
            "SELECT eligibility,exclusion_reason FROM resources WHERE resource_id=?",
            (int(resource.resource_id),),
        ).fetchone()
        if current is None or (
            current["eligibility"] == current_eligibility
            and current["exclusion_reason"] == exclusion_reason
        ):
            continue
        db.execute(
            "UPDATE resources SET eligibility=?,exclusion_reason=? WHERE resource_id=?",
            (current_eligibility, exclusion_reason, int(resource.resource_id)),
        )
        changed += 1
    return changed
