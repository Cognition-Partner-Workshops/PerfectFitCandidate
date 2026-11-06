"""Capture P1-EX-03 KPI-02/05 baselines without modifying the application database."""

import argparse
import json
import os
import sqlite3
import stat
import sys
import tempfile
from contextlib import closing
from pathlib import Path

from pfc.config import Settings
from pfc.services.kpi.baselines import markdown, measure


def _report_mode(target: Path) -> int:
    """Keep an existing report's permissions; new reports get the umask default."""
    if target.exists():
        return stat.S_IMODE(target.stat().st_mode)
    umask = os.umask(0)
    os.umask(umask)
    return 0o666 & ~umask


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
        rendered = [json.dumps(report, indent=2, allow_nan=False) + "\n", markdown(report)]
        staged: list[Path] = []
        try:
            for target, content in zip(outputs, rendered, strict=True):
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=target.parent,
                    prefix=f".{target.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as stream:
                    staged.append(Path(stream.name))
                    stream.write(content)
                os.chmod(staged[-1], _report_mode(target))
            for temporary, target in zip(staged, outputs, strict=True):
                os.replace(temporary, target)
        finally:
            for temporary in staged:
                temporary.unlink(missing_ok=True)
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
