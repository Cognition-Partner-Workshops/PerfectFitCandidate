"""Current per-demand matching runs and pair predicates."""

import json
import sqlite3

from .lifecycle import reconcile_runs


def current_runs(db: sqlite3.Connection) -> dict[int, int]:
    """Return the newest completed run that assessed each open demand."""
    reconcile_runs(db)
    open_demands = {
        int(row[0]) for row in db.execute("SELECT demand_id FROM demands WHERE lifecycle='open'")
    }
    if not open_demands:
        return {}

    assessed_by_run: dict[int, set[int]] = {}
    for row in db.execute(
        "SELECT run_id,dq_summary_json FROM match_runs "
        "WHERE status='completed' ORDER BY run_id DESC"
    ):
        run_id = int(row["run_id"])
        assessed: set[int] = set()
        try:
            summary = json.loads(row["dq_summary_json"] or "{}")
        except (TypeError, ValueError):
            summary = {}
        demands = summary.get("demands", {}) if isinstance(summary, dict) else {}
        if isinstance(demands, dict):
            for value in demands:
                try:
                    assessed.add(int(value))
                except (TypeError, ValueError):
                    continue
        assessed_by_run[run_id] = assessed

    for row in db.execute(
        "SELECT DISTINCT m.run_id,m.demand_id FROM match_results m "
        "JOIN match_runs r USING(run_id) WHERE r.status='completed' "
        "ORDER BY m.run_id DESC"
    ):
        assessed_by_run.setdefault(int(row["run_id"]), set()).add(int(row["demand_id"]))

    current: dict[int, int] = {}
    for run_id in sorted(assessed_by_run, reverse=True):
        for demand_id in assessed_by_run[run_id]:
            if demand_id in open_demands and demand_id not in current:
                current[demand_id] = run_id
    return dict(sorted(current.items()))


def run_pair_predicate(pairs: dict[int, int], *, alias: str = "m") -> tuple[str, tuple[int, ...]]:
    """Build a SQLite predicate for (run_id, demand_id) current pairs."""
    if not pairs:
        return "0", ()
    ordered = sorted((run_id, demand_id) for demand_id, run_id in pairs.items())
    values = ",".join("(?,?)" for _ in ordered)
    parameters = tuple(value for pair in ordered for value in pair)
    return f"({alias}.run_id,{alias}.demand_id) IN (VALUES {values})", parameters
