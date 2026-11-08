import json
import os
import re

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

import summarize


# ---------------------------------------------------------------- 章検出


def test_parse_toc_entries_dedupes():
    texts = [
        "TABLE OF CONTENTS (1)\n7EXERCISE 1.1: Intro\n9Why the Web?\n9Why the Web?",
        "TABLE OF CONTENTS (2)\n13Assessment",
    ]
    entries = summarize.parse_toc_entries(texts, [0, 1])
    assert entries == [(7, "EXERCISE 1.1: Intro"), (9, "Why the Web?"), (13, "Assessment")]


def _book_texts(pages):
    texts = []
    for i in range(pages):
        if i == 0:
            texts.append("cover")
        else:
            texts.append(f"SEC542 | Book {i - 1}\nbody {i}")
    return texts


def test_toc_ranges_maps_slide_to_page():
    texts = _book_texts(12)
    entries = [(1, "Intro"), (4, "Deep")]
    chapters = summarize.toc_ranges(texts, entries)
    assert chapters == [
        {"title": "Intro", "start": 3, "end": 5},
        {"title": "Deep", "start": 6, "end": 12},
    ]


def test_toc_ranges_rejects_bad_mapping():
    texts = _book_texts(12)
    # ヘッダーの数字が合わないので検証に失敗する
    texts[2] = "SEC542 | Book 99"
    assert summarize.toc_ranges(texts, [(1, "Intro"), (4, "Deep")]) is None


def test_fallback_ranges():
    chapters = summarize.fallback_ranges(["x"] * 23)
    assert [(c["start"], c["end"]) for c in chapters] == [(1, 10), (11, 20), (21, 23)]


class _StubItem:
    def __init__(self, title):
        self.title = title


class _StubReader:
    def __init__(self, entries):
        self._entries = entries
        self.outline = [_StubItem(t) for t, _ in entries]

    def get_destination_page_number(self, item):
        for title, page in self._entries:
            if item.title == title:
                return page - 1
        raise KeyError(title)


def test_outline_ranges():
    reader = _StubReader([("Exercise 1.1", 3), ("Exercise 1.2", 10), ("Intro", 1)])
    chapters = summarize.outline_ranges(reader, ["x"] * 20)
    assert chapters == [
        {"title": "Exercise 1.1", "start": 3, "end": 9},
        {"title": "Exercise 1.2", "start": 10, "end": 20},
    ]


# ---------------------------------------------------------------- 整形


def test_clean_text_removes_header_footer_and_blanks():
    texts = [
        "SEC542 | Book 1\n本文A\n© 2023 Someone\n\n\n本文A2",
        "SEC542 | Book 2\n本文B\n© 2023 Someone",
    ]
    repeated = summarize.repeated_lines(texts)
    cleaned = summarize.clean_text(texts[0], repeated)
    assert "SEC542 | Book" not in cleaned
    assert "© 2023" not in cleaned
    assert "本文A" in cleaned
    assert "\n\n\n" not in cleaned


# ---------------------------------------------------------------- extract（合成 PDF）


def _make_text_pdf(path, page_texts):
    writer = PdfWriter()
    for text in page_texts:
        page = writer.add_blank_page(width=300, height=300)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
        )
        content = DecodedStreamObject()
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        content.set_data(f"BT /F1 10 Tf 20 250 Td ({escaped}) Tj ET".encode("latin-1"))
        page.replace_contents(content)
    with open(path, "wb") as handle:
        writer.write(handle)


def test_extract_fallback_writes_manifest(tmp_path):
    pdf = tmp_path / "in.pdf"
    _make_text_pdf(pdf, [f"page {i} text" for i in range(1, 13)])

    out_dir = tmp_path / "out"
    manifest = summarize.extract(str(pdf), str(out_dir))

    assert manifest["pages"] == 12
    assert manifest["method"] == "fallback"
    assert [(c["start"], c["end"]) for c in manifest["chapters"]] == [(1, 10), (11, 12)]
    assert os.path.isfile(out_dir / "manifest.json")
    assert os.path.isfile(out_dir / "chapters" / "01_p-1-10.txt")


def test_extract_refuses_existing_output(tmp_path):
    pdf = tmp_path / "in.pdf"
    _make_text_pdf(pdf, ["a", "b"])
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "keep.txt").write_text("x")

    with pytest.raises(ValueError):
        summarize.extract(str(pdf), str(out_dir))


# ---------------------------------------------------------------- assemble


def _build_assembly_dir(tmp_path):
    out_dir = tmp_path / "book"
    (out_dir / "summaries").mkdir(parents=True)
    (out_dir / "diagrams").mkdir()
    manifest = {
        "source": "work/SEC542 - Book 1.pdf",
        "pages": 20,
        "method": "toc",
        "created": "2026-01-01T00:00:00+00:00",
        "chapters": [
            {"index": 0, "title": "前付け", "start": 1, "end": 2, "chars": 10, "file": "chapters/00_front.txt"},
            {"index": 1, "title": "Intro", "start": 3, "end": 8, "chars": 10, "file": "chapters/01_intro.txt"},
            {"index": 2, "title": "Deep", "start": 9, "end": 20, "chars": 10, "file": "chapters/02_deep.txt"},
        ],
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    (out_dir / "summaries" / "00.html").write_text("<p>全体の説明</p>", encoding="utf-8")
    (out_dir / "summaries" / "01.html").write_text("<ul><li>要点1</li></ul>", encoding="utf-8")
    (out_dir / "summaries" / "02.html").write_text("<p>詳細</p>", encoding="utf-8")
    (out_dir / "diagrams" / "overview-1.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 40">'
        '<rect x="0" y="0" width="100" height="40" fill="#eef"/>'
        '<text x="50" y="25" text-anchor="middle">構成</text></svg>',
        encoding="utf-8",
    )
    (out_dir / "diagrams" / "ch-01.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 40">'
        '<text x="50" y="25">流れ</text></svg>',
        encoding="utf-8",
    )
    return out_dir


def test_assemble_builds_single_html(tmp_path):
    out_dir = _build_assembly_dir(tmp_path)
    output = summarize.assemble(str(out_dir))

    html = open(output, encoding="utf-8").read()
    assert output.endswith("_summary.html")
    assert "<!DOCTYPE html>" in html
    assert html.count("<section") == 3
    assert 'href="#ch-1"' in html
    assert "p.3–8" in html
    assert html.count("<svg") == 2
    assert re.search(r'(?:src|href)\s*=\s*["\']https?:', html) is None


def test_assemble_reports_missing_summary(tmp_path):
    out_dir = _build_assembly_dir(tmp_path)
    (out_dir / "summaries" / "01.html").unlink()

    with pytest.raises(ValueError) as excinfo:
        summarize.assemble(str(out_dir))
    assert "01.html" in str(excinfo.value)
