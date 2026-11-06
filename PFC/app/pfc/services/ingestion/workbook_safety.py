"""Bounded ZIP and XML inspection before openpyxl sees a workbook."""

from pathlib import Path
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile, ZipFile

from defusedxml.common import DefusedXmlException  # type: ignore[import-untyped]
from defusedxml.ElementTree import iterparse  # type: ignore[import-untyped]

from .limits import WorkbookLimits


class WorkbookError(ValueError):
    def __init__(self, code: str, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def inspect_workbook(path: Path, limits: WorkbookLimits) -> None:
    try:
        with ZipFile(path) as archive:
            members = archive.infolist()
            if sum(m.file_size for m in members) > limits.max_uncompressed_bytes:
                raise WorkbookError("WORKBOOK_ZIP_BOMB", "Workbook expands beyond the size limit")
            for member in members:
                if member.file_size > limits.max_uncompressed_bytes or (
                    member.file_size > 1024 * 1024
                    and member.file_size > 100 * max(member.compress_size, 1)
                ):
                    raise WorkbookError("WORKBOOK_ZIP_BOMB", "Suspicious workbook compression")
                if not member.filename.lower().endswith((".xml", ".rels")):
                    continue
                sheets = 0
                with archive.open(member) as source:
                    for _, element in iterparse(
                        source, forbid_dtd=True, forbid_entities=True, forbid_external=True
                    ):
                        if member.filename == "xl/workbook.xml" and element.tag == (
                            "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}sheet"
                        ):
                            sheets += 1
                            if sheets > limits.max_sheets:
                                raise WorkbookError(
                                    "WORKBOOK_TOO_MANY_SHEETS", "Workbook exceeds the sheet limit"
                                )
                        element.clear()
            if "xl/workbook.xml" not in archive.namelist():
                raise WorkbookError("WORKBOOK_INVALID", "Workbook metadata is missing")
    except (BadZipFile, ParseError, DefusedXmlException, OSError, RuntimeError) as exc:
        raise WorkbookError("WORKBOOK_INVALID", "Workbook ZIP or XML is invalid") from exc
