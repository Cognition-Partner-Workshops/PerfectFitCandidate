import hashlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from threading import Event

import numpy as np
import onnx
import pytest
from onnx import TensorProto, helper, numpy_helper
from tokenizers import Tokenizer, models, pre_tokenizers

from pfc.adapters.vector.stores import NumpyStore, SqliteVecStore
from pfc.config import Settings
from pfc.db.connection import connect
from pfc.db.migrate import migrate
from pfc.jobs.runner import JobContext, JobRunner

NOW = "2026-11-06T00:00:00+00:00"


def tiny_bundle(directory: Path, *, token_types: bool = True) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    tokenizer = Tokenizer(
        models.WordLevel(
            {
                "[UNK]": 0,
                "[PAD]": 1,
                "search_document": 2,
                ":": 3,
                "search_query": 4,
                "Python": 5,
                "SQL": 6,
                "Engineer": 7,
            },
            unk_token="[UNK]",
        )
    )
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer.save(str(directory / "tokenizer.json"))
    inputs = [
        helper.make_tensor_value_info(name, TensorProto.INT64, ["batch", "sequence"])
        for name in ("input_ids", "attention_mask", "token_type_ids")
        if token_types or name != "token_type_ids"
    ]
    nodes = [
        helper.make_node("Cast", ["input_ids"], ["ids_float"], to=TensorProto.FLOAT),
        helper.make_node("Unsqueeze", ["ids_float", "axes"], ["ids_3d"]),
        helper.make_node("Add", ["ids_3d", "bias"], ["last_hidden_state"]),
    ]
    graph = helper.make_graph(
        nodes,
        "tiny-nomic",
        inputs,
        [
            helper.make_tensor_value_info(
                "last_hidden_state", TensorProto.FLOAT, ["batch", "sequence", 8]
            )
        ],
        initializer=[
            numpy_helper.from_array(np.array([2], dtype=np.int64), "axes"),
            numpy_helper.from_array(np.arange(8, dtype=np.float32), "bias"),
        ],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 9
    onnx.checker.check_model(model)
    path = directory / "model_int8.onnx"
    onnx.save(model, path)
    return path


def jd(db: sqlite3.Connection, id: int, text: str = "Python", title: str = "") -> None:
    db.execute(
        "INSERT INTO demands(demand_id,code_requisition,id_format_ok,demand_request_name,"
        "row_hash,created_at,updated_at) VALUES (?,?,1,?,'row',?,?)",
        (id, f"T1{id:04}", title, NOW, NOW),
    )
    db.execute(
        "INSERT INTO demand_versions(demand_version_id,demand_id,version_no,jd_text_raw,"
        "jd_text_norm,jd_sha256,created_at) VALUES (?,?,1,?,?,?,?)",
        (id, id, text, text, hashlib.sha256(text.encode()).hexdigest(), NOW),
    )
    db.execute("UPDATE demands SET current_version_id=? WHERE demand_id=?", (id, id))
    db.commit()


def cv(db: sqlite3.Connection, id: int, text: str = "Python", sections: str | None = None) -> None:
    db.execute(
        "INSERT INTO resumes(resume_id,source_path,file_name,file_sha256,mime_type,discovered_at) "
        "VALUES (?,?,?,?,'application/pdf',?)",
        (id, f"{id}.pdf", f"{id}.pdf", f"file{id}", NOW),
    )
    db.execute(
        "INSERT INTO cv_versions(cv_version_id,resume_id,version_no,text_sha256,text_norm,"
        "sections_json,extracted_by,is_latest,extracted_at) VALUES (?,?,1,?,?,?,'test',1,?)",
        (id, id, hashlib.sha256(text.encode()).hexdigest(), text, sections, NOW),
    )
    db.commit()


@pytest.fixture(params=[NumpyStore, SqliteVecStore])
def state(
    request: pytest.FixtureRequest, tmp_path: Path
) -> Iterator[tuple[Settings, NumpyStore, JobContext]]:
    settings = Settings(data_dir=tmp_path / "data", config_dir=tmp_path / "config")
    migrate(settings)
    runner = JobRunner(settings)
    db = connect(settings)
    store = request.param(db, dimensions=8)
    db.execute("INSERT INTO embedding_models VALUES (1,'old','onnx','',384,1,'old',1)")
    db.execute("INSERT INTO embedding_models VALUES (2,'new','onnx','',8,1,'v1',0)")
    db.execute(
        "INSERT INTO jobs(job_id,kind,status,created_at) VALUES (1,'embed','running',?)", (NOW,)
    )
    db.execute(
        "INSERT INTO embedding_chunks VALUES (1,'jd',999,'full',0,'old','old',1,1,?)", (NOW,)
    )
    store.upsert(1, np.ones(384), "old-384")
    db.commit()
    yield settings, store, JobContext(runner, 1, Event())
    runner.shutdown()
    db.close()
