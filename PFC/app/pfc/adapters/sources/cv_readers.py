"""Small text-only readers for zipped XML and RTF CVs."""

import posixpath
import re
from pathlib import Path
from zipfile import ZipFile

from lxml import etree  # type: ignore[import-untyped]

_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_T = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"


def _xml(archive: ZipFile, name: str) -> etree._Element:
    return etree.fromstring(
        archive.read(name), etree.XMLParser(resolve_entities=False, no_network=True)
    )


def _relationships(archive: ZipFile, part: str) -> dict[str, str]:
    parent, name = posixpath.split(part)
    rels = f"{parent}/_rels/{name}.rels"
    if rels not in archive.namelist():
        return {}
    return {
        item.attrib["Id"]: posixpath.normpath(posixpath.join(parent, item.attrib["Target"])).lstrip(
            "/"
        )
        for item in _xml(archive, rels)
        if item.get("TargetMode") != "External"
    }


def read_pptx(path: Path) -> str:
    parts = []
    with ZipFile(path) as archive:
        presentation = _xml(archive, "ppt/presentation.xml")
        relationships = _relationships(archive, "ppt/presentation.xml")
        slides = presentation.findall(".//{*}sldId")
        for slide in slides:
            name = relationships[slide.attrib[f"{{{_R}}}id"]]
            names = [name]
            names.extend(
                target
                for target in _relationships(archive, name).values()
                if target.startswith("ppt/notesSlides/") and target.endswith(".xml")
            )
            for part in names:
                for paragraph in _xml(archive, part).iter(f"{{{_A}}}p"):
                    parts.append(
                        "".join(
                            "\n" if node.tag == f"{{{_A}}}br" else node.text or ""
                            for node in paragraph.iter()
                            if node.tag in {f"{{{_A}}}t", f"{{{_A}}}br"}
                        )
                    )
    return "\n".join(parts).strip()


def read_odt(path: Path) -> str:
    def text(node: etree._Element) -> str:
        if node.tag == f"{{{_T}}}line-break":
            return "\n"
        if node.tag == f"{{{_T}}}tab":
            return "\t"
        if node.tag == f"{{{_T}}}s":
            return " " * min(int(node.get(f"{{{_T}}}c", "1")), 10000)
        return (node.text or "") + "".join(text(c) + (c.tail or "") for c in node)

    with ZipFile(path) as archive:
        root = _xml(archive, "content.xml")
        return "\n".join(
            text(node) for node in root.iter() if node.tag in {f"{{{_T}}}p", f"{{{_T}}}h"}
        ).strip()


_RTF_TOKEN = re.compile(r"\\([a-zA-Z]+)(-?\d+)? ?|\\'([0-9a-fA-F]{2})|\\(.)|([{}])|([^\\{}]+)")
_DESTINATIONS = {
    "fonttbl",
    "colortbl",
    "stylesheet",
    "info",
    "pict",
    "object",
    "header",
    "footer",
    "headerl",
    "headerr",
    "headerf",
    "footerl",
    "footerr",
    "footerf",
}


def read_rtf(path: Path) -> str:
    output: list[str] = []
    stack: list[tuple[bool, int]] = []
    skip, uc, fallback = False, 1, 0
    for match in _RTF_TOKEN.finditer(path.read_bytes().decode("cp1252", errors="replace")):
        word, number, hex_byte, escaped, brace, plain = match.groups()
        if brace == "{":
            stack.append((skip, uc))
        elif brace == "}":
            if stack:
                skip, uc = stack.pop()
        elif word in _DESTINATIONS or escaped == "*":
            skip = True
        elif skip:
            continue
        elif word == "uc":
            uc = max(0, int(number or "1"))
        elif word == "u":
            output.append(chr(int(number or "0") % 65536))
            fallback = uc
        else:
            value = ""
            if word in {"par", "line"}:
                value = "\n"
            elif word == "tab":
                value = "\t"
            elif hex_byte:
                value = bytes.fromhex(hex_byte).decode("cp1252", errors="replace")
            elif escaped in {"\\", "{", "}"}:
                value = escaped
            elif plain:
                value = plain.replace("\r", "").replace("\n", "")
            count = min(fallback, len(value))
            fallback -= count
            output.append(value[count:])
    return (
        "".join(output)
        .encode("utf-16", errors="surrogatepass")
        .decode("utf-16", errors="replace")
        .strip()
    )
