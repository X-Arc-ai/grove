"""Isolated, bounded OOXML text extraction. No package member reaches the filesystem."""

import io
import json
import posixpath
import re
import resource
import stat
import sys
import zipfile
from xml.etree import ElementTree as ET

MAX_INPUT = 16 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024
MAX_ENTRY = 8 * 1024 * 1024
MAX_OUTPUT = 8 * 1024 * 1024
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def xml(data, budget):
    if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)", data, re.I):
        raise ValueError
    root = ET.fromstring(data)
    stack = [(root, 1)]
    count = 0
    while stack:
        node, depth = stack.pop()
        count += 1
        budget[0] += 1
        if depth > 64 or count > 100000 or budget[0] > 100000:
            raise ValueError
        stack.extend((child, depth + 1) for child in node)
    return root


def local(node):
    return node.tag.rsplit("}", 1)[-1]


def extract(data, kind):
    entries = {}
    total = 0
    seen_names = set()
    xml_budget = [0]
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        if not 0 < len(infos) <= 2048 or sum(e.file_size for e in infos) > MAX_TOTAL:
            raise ValueError
        for entry in infos:
            name = entry.filename
            if (
                entry.flag_bits & 1
                or name in seen_names
                or name.startswith("/")
                or "\\" in name
                or any(p in (".", "..", "") for p in name.rstrip("/").split("/"))
                or stat.S_ISLNK(entry.external_attr >> 16)
                or entry.file_size > MAX_ENTRY
            ):
                raise ValueError
            seen_names.add(name)
            if entry.is_dir():
                continue
            # A fixed package namespace deliberately excludes embedded objects, images,
            # macros, custom XML, comments, footnotes and unimplemented rich media.
            if not re.fullmatch(
                r"(?:\[Content_Types\]\.xml|_rels/\.rels|docProps/(?:core|app)\.xml|"
                r"word/(?:document|styles|settings|webSettings|fontTable|numbering)\.xml|word/theme/theme\d+\.xml|word/_rels/document\.xml\.rels|"
                r"ppt/(?:presentation|presProps|viewProps|tableStyles)\.xml|ppt/_rels/presentation\.xml\.rels|"
                r"ppt/(?:slides/slide|notesSlides/notesSlide|slideLayouts/slideLayout|slideMasters/slideMaster|notesMasters/notesMaster|theme/theme)\d+\.xml|"
                r"ppt/(?:slides|notesSlides|slideLayouts|slideMasters|notesMasters)/_rels/[A-Za-z]+\d+\.xml\.rels)",
                name,
            ):
                raise ValueError
            with archive.open(entry) as handle:
                body = handle.read(MAX_ENTRY + 1)
            if len(body) > MAX_ENTRY:
                raise ValueError
            total += len(body)
            if total > MAX_TOTAL:
                raise ValueError
            entries[name] = xml(body, xml_budget)
    for name, root in entries.items():
        for node in root.iter():
            if name.endswith(".rels") and local(node) == "Relationship":
                if node.get("TargetMode") == "External" or re.match(
                    r"(?:[A-Za-z]+:|/|\\)", node.get("Target", "")
                ):
                    raise ValueError
            if local(node) in {
                "altChunk",
                "object",
                "oleObj",
                "drawing",
                "pict",
                "pic",
                "chart",
                "videoFile",
                "audioFile",
                "contentPart",
            }:
                raise ValueError

    def relationships(name, source):
        answer = {}
        for node in entries.get(name, []):
            target = node.get("Target", "")
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source), target))
            if resolved.startswith("/") or resolved.startswith("../") or resolved not in entries:
                raise ValueError
            if node.get("Id") in answer:
                raise ValueError
            answer[node.get("Id")] = resolved
        return answer

    blocks = []

    def paragraphs(root, slide):
        start = len(blocks)
        ordinal = 0
        for node in root.iter():
            if local(node) != "p":
                continue
            text = "".join(
                (child.text or "")
                if local(child) == "t"
                else "\t"
                if local(child) == "tab"
                else "\n"
                if local(child) in ("br", "cr")
                else ""
                for child in node.iter()
            )
            if text.strip():
                ordinal += 1
                blocks.append({"text": text, "block": ordinal, "slide": slide})
                if len(blocks) > 1000 or len(text) > 30000:
                    raise ValueError
        return len(blocks) - start

    if kind == "docx":
        if any(n.startswith("ppt/") for n in entries):
            raise ValueError
        paragraphs(entries["word/document.xml"], None)
    elif kind == "pptx":
        if any(n.startswith("word/") for n in entries):
            raise ValueError
        # Placeholder text is an editing prompt, not a visible static template shape.
        # Until inherited shapes have exact slide locators, refuse their text rather
        # than silently retain an incomplete presentation. Package bounds still apply.
        for name, root in entries.items():
            if re.fullmatch(
                r"ppt/(?:slideLayouts/slideLayout|slideMasters/slideMaster)\d+\.xml", name
            ):
                placeholder_text = {
                    node
                    for shape in root.iter()
                    if local(shape) == "sp" and any(local(n) == "ph" for n in shape.iter())
                    for node in shape.iter()
                    if local(node) == "t"
                }
                if any(
                    local(node) == "t"
                    and (node.text or "").strip()
                    and node not in placeholder_text
                    for node in root.iter()
                ):
                    raise ValueError
        links = relationships("ppt/_rels/presentation.xml.rels", "ppt/presentation.xml")
        slides = [n for n in entries["ppt/presentation.xml"].iter() if local(n) == "sldId"]
        for ordinal, node in enumerate(slides, 1):
            path = links[node.get("{" + REL + "}id")]
            if not re.fullmatch(r"ppt/slides/slide\d+\.xml", path):
                raise ValueError
            if not paragraphs(entries[path], ordinal):
                raise ValueError
            relname = posixpath.dirname(path) + "/_rels/" + posixpath.basename(path) + ".rels"
            note_paths = [
                p for p in relationships(relname, path).values() if p.startswith("ppt/notesSlides/")
            ]
            for note in note_paths:
                before = len(blocks)
                paragraphs(entries[note], ordinal)
                # Slide and note blocks have one ordinal namespace.
                offset = sum(1 for b in blocks[:before] if b["slide"] == ordinal)
                for block in blocks[before:]:
                    block["block"] += offset
    else:
        raise ValueError
    if not blocks:
        raise ValueError
    return {"blocks": blocks}


def main():
    resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT, MAX_OUTPUT))
    if sys.platform.startswith("linux"):
        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
    data = sys.stdin.buffer.read(MAX_INPUT + 1)
    if len(data) > MAX_INPUT:
        return 1
    result = json.dumps(extract(data, sys.argv[1]), ensure_ascii=True)
    if len(result) > MAX_OUTPUT:
        return 1
    sys.stdout.write(result)
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception:
        code = 1
    sys.exit(code)
