"""Measure KPI-01 from a completed run and blind analyst CSV labels."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from pfc.config import Settings
from pfc.db.connection import database
from pfc.services.kpi.kpi01 import markdown, measure, parse_labels


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--labels-b", type=Path)
    parser.add_argument("--data-dir", type=Path, help="Override Settings/PFC_DATA_DIR")
    parser.add_argument("--out-json", type=Path, required=True)
    parser.add_argument("--out-md", type=Path, required=True)
    args = parser.parse_args()
    try:
        settings = Settings(data_dir=args.data_dir) if args.data_dir else Settings()
        if not (settings.data_dir / "db" / "pfc.db").is_file():
            raise ValueError("Configured database does not exist")
        paths = [args.labels, settings.data_dir / "db" / "pfc.db"]
        if args.labels_b:
            paths.append(args.labels_b)
        outputs = [args.out_json.resolve(), args.out_md.resolve()]
        if outputs[0] == outputs[1] or any(path.resolve() in outputs for path in paths):
            raise ValueError("Output files must be distinct from each other and all inputs")
        with args.labels.open(encoding="utf-8-sig", newline="") as source:
            labels = parse_labels(source)
        labels_b = None
        if args.labels_b:
            with args.labels_b.open(encoding="utf-8-sig", newline="") as source:
                labels_b = parse_labels(source)
        with database(settings) as db:
            report = measure(db, args.run_id, labels, labels_b)
        args.out_json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        args.out_md.write_text(markdown(report), encoding="utf-8")
    except (ValueError, OSError, sqlite3.Error) as error:
        message = (
            str(error)
            if isinstance(error, ValueError)
            else "Unable to read inputs or write reports"
        )
        print(f"KPI-01: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
