import argparse
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from pfc.config import Settings
from pfc.main import create_app


def openapi_document() -> dict[str, object]:
    with TemporaryDirectory(prefix="pfc-openapi-") as temporary_directory:
        temporary_path = Path(temporary_directory)
        app = create_app(
            Settings(
                data_dir=temporary_path / "data",
                frontend_dir=temporary_path / "frontend",
            )
        )
        return app.openapi()


def main() -> int:
    parser = argparse.ArgumentParser(description="Export PFC's FastAPI OpenAPI document.")
    parser.add_argument(
        "output",
        nargs="?",
        type=Path,
        help="output JSON path (defaults to stdout)",
    )
    args = parser.parse_args()
    output = json.dumps(openapi_document(), indent=2) + "\n"

    if args.output is None:
        sys.stdout.write(output)
    else:
        args.output.write_text(output, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
