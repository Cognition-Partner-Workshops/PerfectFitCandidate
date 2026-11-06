import os
import stat
from pathlib import Path

import pytest

from pfc.config import Settings
from pfc.security.setup import code_path, generate_setup_code, verify_setup_code


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not Windows ACLs")
def test_setup_code_permissions(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    code = generate_setup_code(settings)
    assert stat.S_IMODE(code_path(settings).stat().st_mode) == 0o600
    assert verify_setup_code(settings, code)
    code_path(settings).chmod(0o644)
    replacement = generate_setup_code(settings)
    assert replacement != code
    assert verify_setup_code(settings, replacement)
    assert stat.S_IMODE(code_path(settings).stat().st_mode) == 0o600
    assert list(code_path(settings).parent.iterdir()) == [code_path(settings)]
