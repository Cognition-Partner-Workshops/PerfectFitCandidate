import io
import json
from pathlib import Path

import pytest

from pfc.adapters.llm.child_logs import (
    LOG_BACKUPS,
    MAX_LINE_CHARS,
    MAX_LOG_BYTES,
    MAX_READ_BYTES,
    ChildLogCapture,
    ChildLogFilter,
    RotatingChildLog,
)

FIXTURE = Path(__file__).parents[3] / "fixtures" / "llama_b8936_windows.json"


def test_captured_windows_diagnostics_and_echo_classification() -> None:
    captured = json.loads(FIXTURE.read_text(encoding="utf-8"))
    filtered = ChildLogFilter([])
    retained = {
        name: [safe for raw in lines if (safe := filtered.line(raw)) is not None]
        for name, lines in captured["cases"].items()
    }
    startup = "\n".join(retained["startup_generation_wrong_and_no_key"])
    assert "main: model loaded" in startup
    assert "main: server is listening on http://127.0.0.1:" in startup
    assert startup.count("Unauthorized: Invalid API Key") == 2
    assert "init: api_keys: <redacted>" in startup
    assert "example_format" not in startup
    assert "You are a helpful assistant" not in startup and "Hi there" not in startup
    bad_model = "\n".join(retained["bad_model_path"])
    assert (
        "failed to open GGUF file '<profile>\\PFC-native-capture\\models\\missing-model.gguf'"
        in bad_model
    )
    assert "main: exiting due to model loading error" in bad_model
    busy = "\n".join(retained["port_in_use"])
    assert "couldn't bind HTTP server socket" in busy
    assert "main: exiting due to HTTP server error" in busy
    for lines in retained.values():
        output = "\n".join(lines)
        assert "fixture-user" not in output and "capture-key-suffix" not in output
    raw = "\n".join(captured["cases"]["startup_generation_wrong_and_no_key"])
    assert "CAPTURE_COMPLETION_CANARY" not in raw
    assert captured["responses"]["generation"]["status"] == 200
    assert captured["responses"]["wrong_key"]["status"] == 401
    assert captured["responses"]["no_key"]["status"] == 401


@pytest.mark.parametrize("secret", ["generated-key", "first-env-value", "second-env-value"])
def test_key_values_are_redacted(secret: str) -> None:
    filtered = ChildLogFilter(["generated-key", "first-env-value", "second-env-value"])
    assert (
        filtered.line(f"main: model loading error: {secret}")
        == "main: model loading error: <redacted>"
    )


@pytest.mark.parametrize("field", ["api_keys", "LLAMA_API_KEY", "LLAMA_ARG_API_KEY"])
def test_named_key_fields_and_partial_keys_are_redacted(field: str) -> None:
    assert ChildLogFilter([]).line(f"main: {field}: ****suffix") == "main: api_keys: <redacted>"


@pytest.mark.parametrize("bearer", ["Bearer", "bEaReR", "BEARER"])
def test_unknown_bearer_values_are_redacted(bearer: str) -> None:
    assert (
        ChildLogFilter([]).line(f"main: error: {bearer}\tunknown-token+/==")
        == "main: error: Bearer <redacted>"
    )


@pytest.mark.parametrize(
    "path",
    [
        r"C:\Users\Fixture User\models\one.gguf",
        "c:/users/fixture-user/models/one.gguf",
        "/home/fixture-user/models/one.gguf",
        r"%USERPROFILE%\models\one.gguf",
        r"D:\PrivateProfiles\fixture-user\models\one.gguf",
    ],
)
def test_profile_prefixes_are_redacted(path: str) -> None:
    result = ChildLogFilter([], [r"D:\PrivateProfiles\fixture-user"]).line(
        f"main: loading model '{path}'"
    )
    assert result is not None and "<profile>" in result and "one.gguf" in result
    assert "fixture-user" not in result and "Fixture User" not in result


def test_actual_home_prefix_is_redacted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "nonstandard-profile"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    output = ChildLogFilter([]).line(f"main: loading model '{home}/models/one.gguf'")
    assert output == "main: loading model '<profile>/models/one.gguf'"


@pytest.mark.parametrize(
    "profile", [r"C:\Users\fixture-user\NestedProfile", "/home/fixture-user/NestedProfile"]
)
def test_specific_profile_takes_precedence_over_generic_prefix(profile: str) -> None:
    output = ChildLogFilter([], [profile]).line(f"main: loading model '{profile}/models/one.gguf'")
    assert output == "main: loading model '<profile>/models/one.gguf'"


@pytest.mark.parametrize(
    "line",
    [
        "",
        "unrecognized private continuation",
        "You are a helpful assistant",
        "slot print: prompt: PRIVATE_PROMPT",
        "main: completion = PRIVATE_COMPLETION",
        "main: request_body: PRIVATE_BODY",
        "main: response body: PRIVATE_BODY",
        'main: {"messages":[{"content":"PRIVATE_BODY"}]}',
        "main: generated_text = PRIVATE_COMPLETION",
        "init: chat template, example_format: 'PRIVATE_PROMPT",
    ],
)
def test_payloads_and_unknown_lines_are_dropped(line: str) -> None:
    assert ChildLogFilter([]).line(line) is None


def test_line_cap_applies_after_redaction() -> None:
    key = "PRIVATE_KEY_NEAR_CUTOFF"
    raw = "main: model loading error: " + "x" * (MAX_LINE_CHARS - 30) + key + "y" * 1000
    safe = ChildLogFilter([key]).line(raw)
    assert safe is not None and len(safe) == MAX_LINE_CHARS and safe.endswith(" [truncated]")
    assert "PRIVATE" not in safe


def test_control_sequences_are_removed() -> None:
    result = ChildLogFilter([]).line("\x1b[31mmain: model loaded\x1b[0m\x00\r\n")
    assert result == "main: model loaded"


def test_rotation_is_byte_bounded_and_keeps_three_files(tmp_path: Path) -> None:
    path = tmp_path / "llm.log"
    log = RotatingChildLog(path, max_bytes=64)
    try:
        for index in range(6):
            log.write(f"main: model loaded {index} " + "é" * 10)
    finally:
        log.close()
    files = sorted(tmp_path.glob("llm.log*"))
    assert len(files) == 3 and LOG_BACKUPS == 2 and MAX_LOG_BYTES == 5 * 1024 * 1024
    assert all(file.stat().st_size <= 64 for file in files)
    assert "loaded 5" in path.read_text(encoding="utf-8")
    assert "loaded 4" in path.with_name("llm.log.1").read_text(encoding="utf-8")
    assert "loaded 3" in path.with_name("llm.log.2").read_text(encoding="utf-8")


@pytest.mark.parametrize("data", [b"", b"main: model loaded\xff\xfe\n"])
def test_capture_handles_empty_and_invalid_utf8(tmp_path: Path, data: bytes) -> None:
    capture = ChildLogCapture(tmp_path / "llm.log", [], [])
    stream = io.BytesIO(data)
    capture.start(stream)
    capture.close()
    assert stream.closed and capture.thread is not None and not capture.thread.is_alive()
    output = (tmp_path / "llm.log").read_text(encoding="utf-8")
    assert output.startswith("main: model loaded") if data else output == ""


def test_oversized_lines_are_discarded_without_logging_fragments(tmp_path: Path) -> None:
    capture = ChildLogCapture(tmp_path / "llm.log", [], [])
    stream = io.BytesIO(
        b"main: model loading error: " + b"PRIVATE" * MAX_READ_BYTES + b"\nmain: model loaded\n"
    )
    capture.start(stream)
    capture.close()
    assert (tmp_path / "llm.log").read_text() == "main: model loaded\n"


def test_log_write_errors_do_not_echo_payload_or_stop_draining(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    capture = ChildLogCapture(tmp_path / "llm.log", [], [])
    calls = []

    def fail(text: str) -> None:
        calls.append(text)
        raise OSError("PRIVATE_IO_EXCEPTION")

    monkeypatch.setattr(capture.log, "write", fail)
    stream = io.BytesIO(b"main: model loaded\n" * 100)
    capture.start(stream)
    capture.close()
    assert stream.closed and len(calls) == 1
    assert "Local LLM diagnostic log unavailable" in caplog.text
    assert "PRIVATE_IO_EXCEPTION" not in caplog.text
