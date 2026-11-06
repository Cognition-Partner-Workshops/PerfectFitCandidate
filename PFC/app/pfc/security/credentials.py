"""REQ-AUTH-06 (S4): typed registry of credential settings and the legacy plaintext migration."""

import json
import logging
from contextlib import closing
from typing import Final, Literal, get_args

from pfc.config import Settings
from pfc.db.audit import append_audit
from pfc.db.connection import connect, database
from pfc.security.secrets_store import PREFIX, SecretStore
from pfc.services.occ.common import now

logger = logging.getLogger(__name__)

# Setting keys that hold credentials today. Each is also caught by `sensitive_key`, so OCC admin
# already encrypts new writes; the registry lets startup find legacy plaintext without guessing.
CredentialSettingKey = Literal["smtp.password"]
CREDENTIAL_SETTING_KEYS: Final[tuple[CredentialSettingKey, ...]] = get_args(CredentialSettingKey)


def encrypt_legacy_credentials(settings: Settings) -> int:
    """Encrypt plaintext values under registry keys once; idempotent, audited without values.

    Afterwards the WAL is truncated and the file vacuumed, so the old plaintext does not survive
    in free pages or the WAL. Pre-existing backups are out of scope.
    """
    store = SecretStore(settings.data_dir)
    migrated = 0
    with database(settings) as db:
        rows = db.execute(
            "SELECT setting_id,key,value_json FROM settings WHERE key IN "
            f"({','.join('?' * len(CREDENTIAL_SETTING_KEYS))}) ORDER BY setting_id",
            CREDENTIAL_SETTING_KEYS,
        ).fetchall()
        for row in rows:
            value = json.loads(row["value_json"])
            if isinstance(value, str) and value.startswith(PREFIX):
                continue
            encrypted = store.encrypt(row["value_json"].encode("utf-8"))
            db.execute(
                "UPDATE settings SET value_json=? WHERE setting_id=?",
                (json.dumps(encrypted), row["setting_id"]),
            )
            append_audit(
                db,
                ts=now(),
                action="SETTING_CHANGE",
                entity_type="settings",
                entity_id=row["key"],
                details={
                    "before": {"value": "[plaintext]"},
                    "after": {"value": "[encrypted]"},
                    "reason": "legacy credential migration",
                },
            )
            migrated += 1
    if migrated:
        with closing(connect(settings)) as db:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db.execute("VACUUM")
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        logger.info("Encrypted %d legacy credential setting(s)", migrated)
    return migrated
