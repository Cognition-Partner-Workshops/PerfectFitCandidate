"""Capture P1-EX-03 KPI-02/05 baselines without modifying the application database."""

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from pfc.config import Settings
from pfc.services.kpi.baselines import markdown, measure


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, help="Override Settings/PFC_DATA_DIR")
    parser.add_argument("--out-json", type=Path, required=True)
    parser.add_argument("--out-md", type=Path, required=True)
    args = parser.parse_args()
    try:
        settings = Settings(data_dir=args.data_dir) if args.data_dir else Settings()
        root = settings.data_dir.resolve()
        source = root / "db" / "pfc.db"
        if not source.is_file():
            raise ValueError("Configured database does not exist")
        outputs = [args.out_json.resolve(), args.out_md.resolve()]
        if outputs[0] == outputs[1] or any(
            path.is_relative_to(root) or (path.exists() and path.samefile(source))
            for path in outputs
        ):
            raise ValueError("Outputs must be distinct and outside the application data directory")
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            report = measure(db, settings)
        args.out_json.write_text(
            json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8"
        )
        args.out_md.write_text(markdown(report), encoding="utf-8")
    except (ValueError, OSError, sqlite3.Error) as error:
        message = (
            str(error)
            if isinstance(error, ValueError)
            else "Unable to read inputs or write reports"
        )
        print(f"KPI baselines: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
