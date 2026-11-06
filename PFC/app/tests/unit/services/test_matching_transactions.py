import sqlite3
from pathlib import Path

import pytest

from pfc.services.matching.common import atomic


@pytest.mark.parametrize("outer", [False, True])
@pytest.mark.parametrize("fail", [False, True])
def test_immediate_atomic_preserves_transaction_ownership(
    tmp_path: Path, outer: bool, fail: bool
) -> None:
    path = tmp_path / "transactions.db"
    db = sqlite3.connect(path)
    reader = sqlite3.connect(path)
    try:
        db.execute("CREATE TABLE values_written(value TEXT)")
        if outer:
            db.execute("INSERT INTO values_written VALUES ('caller')")

        def write() -> None:
            with atomic(db, immediate=True):
                assert db.in_transaction
                db.execute("INSERT INTO values_written VALUES ('analysis')")
                if fail:
                    raise RuntimeError("rollback")

        if fail:
            with pytest.raises(RuntimeError, match="rollback"):
                write()
        else:
            write()
        assert db.in_transaction is outer
        expected = ([("caller",)] if outer else []) + ([] if fail else [("analysis",)])
        assert db.execute("SELECT value FROM values_written").fetchall() == expected
        assert reader.execute("SELECT value FROM values_written").fetchall() == (
            [] if outer else expected
        )
        if outer:
            db.commit()
            assert reader.execute("SELECT value FROM values_written").fetchall() == expected
    finally:
        reader.close()
        db.close()
