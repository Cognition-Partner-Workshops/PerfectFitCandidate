import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from pfc.adapters.llm.providers import LlamaCppProvider, PromptRequest, ProviderError
from pfc.adapters.llm.supervisor import LlamaServerSupervisor
from pfc.config import Settings
from pfc.db.connection import database
from pfc.db.migrate import migrate
from pfc.services.occ import llm
from pfc.services.packages.common import PackageServiceError
from pfc.services.packages.payloads import decode_snapshot, encode_snapshot, snapshot


@pytest.mark.parametrize("case", ["present", "absent", "hash", "size", "existing"])
def test_optional_registration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    settings = Settings(data_dir=tmp_path, models_dir=tmp_path)
    migrate(settings)
    monkeypatch.setattr(llm, "THINKING_MODEL_SIZE", 3)
    monkeypatch.setitem(llm.THINKING_MODEL, "file_sha256", hashlib.sha256(b"abc").hexdigest())
    path = tmp_path / str(llm.THINKING_MODEL["file_name"])
    if case != "absent":
        path.write_bytes(b"bad" if case == "hash" else b"abcd" if case == "size" else b"abc")
    if case == "existing":
        with database(settings) as db:
            llm.seed_default_models(db)
    llm.register_optional_thinking_model(settings)
    with patch("pfc.services.occ.llm.hashlib.file_digest", side_effect=AssertionError("rehash")):
        if case in {"present", "existing"}:
            llm.register_optional_thinking_model(settings)
    with database(settings) as db:
        rows = db.execute(
            "SELECT * FROM llm_models WHERE name=?", (llm.THINKING_MODEL["name"],)
        ).fetchall()
        assert len(rows) == int(case in {"present", "existing"})
        if rows:
            assert rows[0]["thinking"] == 1
            assert rows[0]["approved"] == rows[0]["is_active"] == 0


@pytest.mark.parametrize(
    ("thinking", "contents", "success", "calls"),
    [
        (True, ['{"ok":true}'], True, 1),
        (True, ['<think>private</think>{"ok":true}'], True, 1),
        (True, ["bad", '{"ok":true}'], True, 2),
        (True, ["bad", "bad"], False, 2),
        (True, ["", ""], False, 2),
        (False, ["bad", '{"ok":true}'], False, 1),
    ],
)
def test_thinking_output(thinking: bool, contents: list[str], success: bool, calls: int) -> None:
    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": contents[len(seen) - 1],
                            "reasoning_content": "private",
                        }
                    }
                ]
            },
        )

    async def run() -> None:
        provider = LlamaCppProvider(
            "http://localhost",
            "test",
            api_key="test",
            thinking=thinking,
            ctx_size=8192,
            transport=httpx.MockTransport(respond),
        )
        request = PromptRequest(messages=[], json_schema={"type": "object"}, max_tokens=100)
        try:
            if success:
                result = await provider.generate(request)
                assert result.text == '{"ok":true}'
                assert result.parsed_json == {"ok": True}
            else:
                with pytest.raises(ProviderError, match="Invalid structured output"):
                    await provider.generate(request)
            assert provider.capabilities().max_context == 8192
        finally:
            await provider.aclose()

    asyncio.run(run())
    assert len(seen) == calls
    assert seen[0]["max_tokens"] == (1124 if thinking else 100)


def test_migration_and_package_metadata(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    migrations = Path(__file__).parents[4] / "pfc/db/migrations"
    old = tmp_path / "old"
    old.mkdir()
    for migration in migrations.glob("*.sql"):
        if migration.name < "0020":
            (old / migration.name).write_text(migration.read_text())
    migrate(settings, old)
    with database(settings) as db:
        db.execute(
            "INSERT INTO llm_models(name,file_name,file_sha256,size_mb,ram_estimate_mb,"
            "context_tokens,licence,approved) VALUES "
            "('Qwen3-4B-Instruct-2507','x.gguf','',0,0,4096,'Apache-2.0',1)"
        )
    migrate(settings)
    with database(settings) as db:
        row = db.execute("SELECT * FROM llm_models").fetchone()
        assert row["thinking"] == 0 and row["kv_bytes_per_token"] == 147456
        db.execute("UPDATE llm_models SET thinking=1")
        data = snapshot(db)
        decoded = decode_snapshot(encode_snapshot(data))
        assert decoded["llm_models"] == data["llm_models"]
        legacy = encode_snapshot(data)
        for name, rows in legacy.items():
            if name.startswith("llm_models."):
                for model in rows:
                    model.pop("thinking")
                    model.pop("kv_bytes_per_token")
        assert decode_snapshot(legacy)["llm_models"][0]["thinking"] == 0


@pytest.mark.parametrize("value", [4096, 8192, 16384, 2048, 32768, "8192", True, None, [], {}])
def test_context_import_and_resolution(tmp_path: Path, value: object) -> None:
    settings = Settings(data_dir=tmp_path, models_dir=tmp_path, llm_ctx_size=2048)
    migrate(settings)
    (tmp_path / "test.gguf").touch()
    with database(settings) as db:
        llm.seed_default_models(db)
        db.execute("UPDATE llm_models SET approved=1,file_name='test.gguf' WHERE model_id=1")
        db.execute(
            "INSERT INTO settings(scope,key,value_json,updated_at) "
            "VALUES ('system','llm.ctx_size',?,'2026-01-01T00:00:00Z')",
            (json.dumps(value),),
        )
        resolved = LlamaServerSupervisor(settings).resolve_model(db)
        assert resolved is not None
        valid = type(value) is int and value in {4096, 8192, 16384}
        assert resolved.ctx_size == (value if valid else 2048)
        data = snapshot(db)
        if valid:
            assert decode_snapshot(encode_snapshot(data))["settings"][0][
                "value_json"
            ] == json.dumps(value)
        else:
            with pytest.raises(PackageServiceError, match="Invalid llm.ctx_size"):
                decode_snapshot(encode_snapshot(data))


def test_context_package_malformed_json(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    migrate(settings)
    with database(settings) as db:
        data = snapshot(db)
    data["settings"] = [{"key": "llm.ctx_size", "value_json": "not json"}]
    with pytest.raises(PackageServiceError, match="Invalid llm.ctx_size"):
        decode_snapshot(encode_snapshot(data))
