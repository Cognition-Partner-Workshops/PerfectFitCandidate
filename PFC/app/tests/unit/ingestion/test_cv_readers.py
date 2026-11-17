import subprocess
from pathlib import Path
from unittest.mock import Mock
from zipfile import ZipFile

import pytest
from docx import Document

from pfc.adapters.sources import cv_converter, resume_folder
from pfc.adapters.sources.cv_readers import read_odt, read_pptx, read_rtf
from pfc.adapters.sources.resume_paths import resume_files
from pfc.cv_formats import ALLOWED_CV_EXTENSIONS


def zipped(path, files):
    with ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return path


def test_pptx_slide_order_and_notes(tmp_path):
    a = "http://schemas.openxmlformats.org/drawingml/2006/main"
    r = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    path = zipped(
        tmp_path / "cv.pptx",
        {
            "ppt/presentation.xml": f'<p xmlns:r="{r}"><sldId r:id="b"/><sldId r:id="a"/></p>',
            "ppt/_rels/presentation.xml.rels": '<r><rel Id="a" Target="slides/slide1.xml"/>'
            '<rel Id="b" Target="slides/slide2.xml"/></r>',
            "ppt/slides/slide1.xml": f'<s xmlns:a="{a}"><a:p><a:r><a:t>First</a:t></a:r></a:p></s>',
            "ppt/slides/slide2.xml": f'<s xmlns:a="{a}"><a:p><a:r><a:t>Second</a:t>'
            "</a:r><a:br/><a:r><a:t>line</a:t></a:r></a:p></s>",
            "ppt/slides/_rels/slide2.xml.rels": '<r><rel Id="n" '
            'Target="../notesSlides/notesSlide2.xml"/></r>',
            "ppt/notesSlides/notesSlide2.xml": f'<s xmlns:a="{a}"><a:p><a:r><a:t>Note</a:t>'
            "</a:r></a:p></s>",
        },
    )
    assert read_pptx(path) == "Second\nline\nNote\nFirst"


def test_odt_blocks(tmp_path):
    path = zipped(
        tmp_path / "cv.odt",
        {
            "content.xml": '<o xmlns:t="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
            '<t:h>Title</t:h><t:p>Hello<t:s t:c="2"/>world<t:line-break/>next<t:tab/>tab</t:p>'
            "<t:list><t:list-item><t:p>List</t:p></t:list-item></t:list>"
            "<table><row><cell><t:p>Cell</t:p></cell></row></table></o>"
        },
    )
    assert read_odt(path) == "Title\nHello  world\nnext\ttab\nList\nCell"


def test_rtf_controls(tmp_path):
    path = tmp_path / "cv.rtf"
    path.write_bytes(
        rb"{\rtf1{\fonttbl hidden}{\colortbl hidden}{\stylesheet hidden}"
        rb"{\info hidden}{\pict hidden}{\object hidden}{\header hidden}"
        rb"{\footer hidden}{\*\unknown hidden}Hello\par next\line tab\tab "
        rb"\'80 \u233? {\uc2\u945??} \u946? \\ \{ok\}}"
    )
    assert read_rtf(path) == "Hello\nnext\ntab\t€ é α β \\ {ok}"


def test_rtf_table_and_control_symbol_separators(tmp_path):
    path = tmp_path / "table.rtf"
    path.write_bytes(rb"{\rtf1 SQL\cell Python\row Data\~Science\par full\_time co\-operate}")
    assert read_rtf(path) == "SQL\tPython\nData Science\nfull-time cooperate"


@pytest.mark.parametrize("hex_escaped", [False, True])
def test_rtf_ansi_codepage(tmp_path, hex_escaped):
    path = tmp_path / "cyrillic.rtf"
    content = "Привет мир".encode("cp1251")
    if hex_escaped:
        content = b"".join(f"\\'{byte:02x}".encode("ascii") for byte in content)
    path.write_bytes(rb"{\rtf1\ansi\ansicpg1251 " + content + b"}")
    assert read_rtf(path) == "Привет мир"


def test_rtf_unknown_codepage_uses_cp1252(tmp_path):
    path = tmp_path / "fallback.rtf"
    path.write_bytes(rb"{\rtf1\ansicpg99999 \'80 " + b"\x80}")
    assert read_rtf(path) == "€ €"


@pytest.mark.parametrize("extension", [".rtf", ".odt", ".pptx"])
def test_empty_reader(tmp_path, extension):
    path = tmp_path / f"empty{extension}"
    if extension == ".rtf":
        path.write_text(r"{\rtf1}")
    elif extension == ".odt":
        zipped(path, {"content.xml": "<root/>"})
    else:
        zipped(path, {"ppt/presentation.xml": "<root/>"})
    assert resume_folder.extract_text(path) == ""


@pytest.mark.parametrize("extension", [".odt", ".pptx"])
def test_invalid_archive(tmp_path, extension):
    path = tmp_path / f"broken{extension}"
    path.write_bytes(b"not a zip")
    text, flags = resume_folder.bounded_extract(
        path, resume_folder.extract_text, 10, content=path.read_bytes(), work_dir=tmp_path / "work"
    )
    assert text == ""
    assert flags == ["extraction failed: BadZipFile"]


def test_extensions(tmp_path):
    for extension in ALLOWED_CV_EXTENSIONS:
        (tmp_path / f"cv{extension}").touch()
    assert len(list(resume_files(tmp_path))) == 8
    assert [p.suffix for p in resume_files(tmp_path, {".rtf"})] == [".rtf"]
    assert list(resume_files(tmp_path, set())) == []


@pytest.mark.parametrize("extension", [".doc", ".ppt", ".DOC", ".PPT"])
def test_direct_legacy_extraction_requires_bounded_path(tmp_path, monkeypatch, extension):
    convert = Mock()
    settings = Mock()
    monkeypatch.setattr(resume_folder, "convert_legacy", convert)
    monkeypatch.setattr(resume_folder, "Settings", settings)
    with pytest.raises(ValueError, match="require bounded_extract with timeout and work_dir"):
        resume_folder.extract_text(tmp_path / f"cv{extension}")
    convert.assert_not_called()
    settings.assert_not_called()


@pytest.mark.parametrize("extension", [".doc", ".ppt"])
def test_bounded_legacy_extraction_uses_supplied_budget(tmp_path, monkeypatch, extension):
    convert = Mock(return_value=(b"converted", "docx" if extension == ".doc" else "pptx"))
    reader = Mock(return_value=("Tiny CV", []))
    monkeypatch.setattr(resume_folder, "convert_legacy", convert)
    monkeypatch.setattr(resume_folder, "_run_extractor", reader)
    monkeypatch.setattr(resume_folder.time, "monotonic", Mock(side_effect=[10, 10.25]))
    work_dir = tmp_path / "configured-work"
    assert resume_folder.bounded_extract(
        tmp_path / f"cv{extension}",
        resume_folder.extract_text,
        7.5,
        content=b"legacy",
        work_dir=work_dir,
    ) == ("Tiny CV", [])
    assert convert.call_args.kwargs == {"timeout": 7.5, "work_dir": work_dir}
    assert reader.call_args.args[2] == 7.25
    assert not list(work_dir.iterdir())


def test_missing_converter(tmp_path, monkeypatch):
    monkeypatch.setenv("PFC_SOFFICE", str(tmp_path / "absent"))
    assert not cv_converter.converter_available()
    with pytest.raises(cv_converter.ConverterUnavailable):
        cv_converter.convert_legacy(tmp_path / "cv.doc", timeout=2, work_dir=tmp_path)
    assert resume_folder.bounded_extract(
        tmp_path / "cv.doc", resume_folder.extract_text, 2, content=b"legacy", work_dir=tmp_path
    ) == ("", ["extraction failed: ConverterUnavailable"])


def test_bundled_converter(tmp_path, monkeypatch):
    monkeypatch.delenv("PFC_SOFFICE", raising=False)
    monkeypatch.setenv("PFC_HOME", str(tmp_path))
    bundled = tmp_path / "libreoffice" / "program" / "soffice.exe"
    bundled.parent.mkdir(parents=True)
    bundled.touch()
    assert cv_converter.find_soffice() == bundled


def test_converter_override(tmp_path, monkeypatch):
    executable = tmp_path / "custom-soffice.exe"
    executable.touch(mode=0o700)
    monkeypatch.setenv("PFC_SOFFICE", str(executable))
    assert cv_converter.find_soffice() == executable


def test_converter_success(tmp_path, monkeypatch):
    monkeypatch.setattr(cv_converter, "find_soffice", lambda: Path("soffice"))
    calls = []

    def launch(argv, **kwargs):
        calls.append((argv, kwargs))
        directory = Path(argv[argv.index("--outdir") + 1])
        doc = Document()
        doc.add_paragraph("Converted CV")
        doc.save(directory / "cv.docx")
        return Mock(wait=Mock(return_value=0))

    monkeypatch.setattr(cv_converter.subprocess, "Popen", launch)
    assert resume_folder.bounded_extract(
        tmp_path / "cv.doc", resume_folder.extract_text, 10, content=b"legacy", work_dir=tmp_path
    ) == ("Converted CV", [])
    argv, kwargs = calls[0]
    assert argv[argv.index("--convert-to") + 1] == "docx"
    assert kwargs["start_new_session"] is (cv_converter.os.name != "nt")
    assert not list(tmp_path.glob("convert-*"))
    assert not list(tmp_path.glob("pfc-cv-*"))


def test_converter_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(cv_converter, "find_soffice", lambda: Path("soffice"))
    process = Mock(pid=123)
    process.wait.side_effect = [subprocess.TimeoutExpired("soffice", 0.1), 0]
    monkeypatch.setattr(cv_converter.subprocess, "Popen", Mock(return_value=process))
    kill = Mock()
    if cv_converter.sys.platform == "win32":
        monkeypatch.setattr(cv_converter.subprocess, "run", kill)
    else:
        monkeypatch.setattr(cv_converter.os, "killpg", kill)
    assert resume_folder.bounded_extract(
        tmp_path / "cv.ppt", resume_folder.extract_text, 0.1, content=b"legacy", work_dir=tmp_path
    ) == ("", ["extraction_timeout"])
    if cv_converter.sys.platform == "win32":
        kill.assert_called_once_with(
            ["taskkill", "/PID", "123", "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        kill.assert_called_once_with(123, cv_converter.signal.SIGKILL)
    assert process.wait.call_args_list[0].kwargs == {"timeout": 0.1}
    assert not list(tmp_path.glob("convert-*"))


def test_converter_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(cv_converter, "find_soffice", lambda: Path("soffice"))
    monkeypatch.setattr(
        cv_converter.subprocess, "Popen", Mock(return_value=Mock(wait=lambda **kw: 1))
    )
    with pytest.raises(RuntimeError, match="conversion failed"):
        cv_converter.convert_legacy(tmp_path / "cv.doc", timeout=1, work_dir=tmp_path)


@pytest.mark.skipif(not cv_converter.converter_available(), reason="LibreOffice not installed")
@pytest.mark.parametrize("extension", ["doc", "ppt"])
def test_real_legacy_conversion(tmp_path, extension):
    path = Path(__file__).parents[2] / "fixtures" / "cv_formats" / f"tiny.{extension}"
    text, flags = resume_folder.bounded_extract(
        path, resume_folder.extract_text, 30, content=path.read_bytes(), work_dir=tmp_path
    )
    assert flags == []
    assert "Tiny CV" in text
