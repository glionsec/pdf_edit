import json
import os

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

import summarize
from summarize import ExtractError


# ---------------------------------------------------------------- ヘルパー


class _StubItem:
    def __init__(self, title, page):
        self.title = title
        self.page = page


class _StubReader:
    def __init__(self, texts, outline=(), labels=None):
        self._texts = list(texts)
        self.outline = list(outline)
        self.page_labels = labels if labels is not None else [str(i + 1) for i in range(len(texts))]
        self.trailer = {"/Root": {"/PageLabels": object()} if labels is not None else {}}

    def get_destination_page_number(self, item):
        return item.page - 1


def _book_texts(pages):
    texts = ["cover"]
    for i in range(1, pages):
        texts.append(f"SEC542 | Book {i - 1}\nbody {i}")
    return texts


VALID_FRAGMENT = (
    '<p class="lead">この章の目的と位置づけを説明します。</p>\n<ul>\n'
    "<li>要点の説明です。重要語は<strong>強調</strong>します。</li>\n"
    "<li>二つ目の要点です。手順と確認ポイントを整理します。</li>\n"
    "<li>三つ目の要点です。失敗時の注意点にも触れます。</li>\n"
    "<li>四つ目の要点です。関連する章との関係を示します。</li>\n"
    "</ul>\n"
)

VALID_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 40">'
    '<rect x="0" y="0" width="100" height="40" fill="#eef"/></svg>'
)


# ---------------------------------------------------------------- 目次


def test_parse_toc_entries_supports_two_layouts():
    texts = [
        "TABLE OF CONTENTS (1)\n7EXERCISE 1.1: Intro\n1. Introduction ..... 3\n9Why the Web?",
        "TABLE OF CONTENTS (2)\n9Why the Web?",
    ]
    entries = summarize.parse_toc_entries(texts, [0, 1])
    assert (7, "EXERCISE 1.1: Intro") in entries
    assert (3, "1. Introduction") in entries
    assert sum(1 for number, title in entries if title == "Why the Web?") == 1


def test_chapters_from_entries_builds_ranges():
    chapters = summarize.chapters_from_entries([(3, "A"), (6, "B")], 10)
    assert chapters == [
        {"title": "A", "start": 3, "end": 5},
        {"title": "B", "start": 6, "end": 10},
    ]


def test_toc_with_page_labels_mapping():
    texts = ["CONTENTS\n1. Aaa\n3. Bbb\n5. Ccc"] + ["body"] * 9
    labels = ["i", "ii", "1", "2", "3", "4", "5", "6", "7", "8"]
    reader = _StubReader(texts, labels=labels)

    detected = summarize.detect_chapters(reader, texts)
    assert detected["strategy"] == "toc+labels"
    assert [(c["start"], c["end"]) for c in detected["chapters"]] == [(3, 4), (5, 6), (7, 10)]


def test_toc_offset_is_detected_from_page_edges():
    texts = _book_texts(12)
    texts[0] = "TABLE OF CONTENTS\n1. First\n4. Second"
    reader = _StubReader(texts, labels=["x"] * 12)

    detected = summarize.detect_chapters(reader, texts)
    assert detected["strategy"] == "toc+offset"
    assert [(c["start"], c["end"]) for c in detected["chapters"]] == [(3, 5), (6, 12)]


def test_explicit_offset_overrides_detection():
    texts = _book_texts(12)
    texts[0] = "TABLE OF CONTENTS\n1. First\n4. Second"
    reader = _StubReader(texts, labels=["x"] * 12)

    detected = summarize.detect_chapters(reader, texts, {"offset": 0})
    assert detected["strategy"] == "toc+offset"
    assert [(c["start"], c["end"]) for c in detected["chapters"]] == [(1, 3), (4, 12)]


# ---------------------------------------------------------------- しおり・見出し


def test_outline_strategy_generic():
    texts = ["body"] * 10
    reader = _StubReader(
        texts,
        outline=[_StubItem("1. Introduction", 3), _StubItem("2. Details", 6)],
    )
    detected = summarize.detect_chapters(reader, texts)
    assert detected["strategy"] == "outline"
    assert [(c["start"], c["end"]) for c in detected["chapters"]] == [(3, 5), (6, 10)]


def test_outline_sans_profile_filters_exercises():
    texts = ["body"] * 20
    reader = _StubReader(
        texts,
        outline=[
            _StubItem("Welcome", 1),
            _StubItem("Exercise 1.1", 3),
            _StubItem("Setup", 5),
            _StubItem("Exercise 1.2", 10),
        ],
    )
    generic = summarize.detect_chapters(reader, texts)
    sans = summarize.detect_chapters(reader, texts, {"profile": "sans"})
    assert len(generic["chapters"]) == 4
    assert len(sans["chapters"]) == 2
    assert sans["chapters"][0]["title"] == "Exercise 1.1"


def test_many_outline_entries_fall_back_to_fixed():
    texts = ["body"] * 20
    reader = _StubReader(
        texts, outline=[_StubItem(f"Item {i}", i + 1) for i in range(40)]
    )
    detected = summarize.detect_chapters(reader, texts)
    assert detected["strategy"] == "fixed"


def test_numbered_headings_strategy():
    texts = []
    for i in range(10):
        if i == 0:
            texts.append("1. Chapter One\nbody0")
        elif i == 5:
            texts.append("2. Chapter Two\nbody5")
        else:
            texts.append(f"body{i}")
    reader = _StubReader(texts, labels=["x"] * 10)
    detected = summarize.detect_chapters(reader, texts)
    assert detected["strategy"] == "headings"
    assert [(c["start"], c["end"]) for c in detected["chapters"]] == [(1, 5), (6, 10)]


# ---------------------------------------------------------------- 手動・異常系


def test_manual_chapters(tmp_path):
    path = tmp_path / "chapters.json"
    path.write_text(
        json.dumps({"chapters": [{"title": "A", "start": 2, "end": 4}, {"title": "B", "start": 5, "end": 9}]}),
        encoding="utf-8",
    )
    texts = ["body"] * 10
    reader = _StubReader(texts)
    detected = summarize.detect_chapters(reader, texts, {"manual": str(path)})
    assert detected["strategy"] == "manual"
    assert detected["chapters"][0] == {"title": "A", "start": 2, "end": 4}


def test_manual_chapters_rejects_overlap(tmp_path):
    path = tmp_path / "chapters.json"
    path.write_text(
        json.dumps([{"title": "A", "start": 2, "end": 5}, {"title": "B", "start": 5, "end": 9}]),
        encoding="utf-8",
    )
    with pytest.raises(ExtractError):
        summarize.load_manual_chapters(str(path), 10)


def test_detect_rejects_textless_pdf():
    with pytest.raises(ExtractError):
        summarize.detect_chapters(_StubReader(["", "", ""]), ["", "", ""])


# ---------------------------------------------------------------- 整形


def test_clean_text_removes_headers_and_footers():
    texts = [
        "SEC542 | Book 1\n本文A\n© 2023 Someone\n\n\n本文A2",
        "SEC542 | Book 2\n本文B\n© 2023 Someone",
    ]
    repeated = summarize.repeated_lines(texts)
    cleaned = summarize.clean_text(texts[0], repeated, summarize.PROFILES["sans"]["header_pattern"])
    assert "SEC542 |" not in cleaned
    assert "© 2023" not in cleaned
    assert "本文A" in cleaned
    assert "\n\n\n" not in cleaned


# ---------------------------------------------------------------- extract（合成 PDF）


def _make_text_pdf(path, page_texts, outline=None):
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
    for title, page_index in outline or []:
        writer.add_outline_item(title, page_index)
    with open(path, "wb") as handle:
        writer.write(handle)


def test_extract_outline_pdf(tmp_path):
    pdf = tmp_path / "in.pdf"
    _make_text_pdf(
        pdf,
        [f"page {i} text" for i in range(1, 13)],
        outline=[("Chapter A", 0), ("Chapter B", 6)],
    )
    out_dir = tmp_path / "out"
    manifest = summarize.extract(str(pdf), str(out_dir))

    assert manifest["strategy"] == "outline"
    assert manifest["pages"] == 12
    assert [(c["start"], c["end"]) for c in manifest["chapters"]] == [(1, 6), (7, 12)]
    assert os.path.isfile(out_dir / "manifest.json")


def test_extract_dry_run_writes_nothing(tmp_path):
    pdf = tmp_path / "in.pdf"
    _make_text_pdf(pdf, [f"page {i}" for i in range(1, 6)])
    out_dir = tmp_path / "out"
    assert summarize.extract(str(pdf), str(out_dir), dry_run=True) is None
    assert not out_dir.exists()


def test_extract_refuses_existing_output_without_force(tmp_path):
    pdf = tmp_path / "in.pdf"
    _make_text_pdf(pdf, ["a", "b"])
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "keep.txt").write_text("x")

    with pytest.raises(ExtractError):
        summarize.extract(str(pdf), str(out_dir))


def test_safe_removal_rejects_cwd():
    with pytest.raises(ExtractError):
        summarize._safe_removal_target(os.getcwd())


def test_safe_removal_rejects_git_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    with pytest.raises(ExtractError):
        summarize._safe_removal_target(repo)


def test_extract_rejects_encrypted_pdf(tmp_path):
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.encrypt("secret")
    pdf = tmp_path / "enc.pdf"
    with open(pdf, "wb") as handle:
        writer.write(handle)

    with pytest.raises(ExtractError):
        summarize.extract(str(pdf), str(tmp_path / "out"))


# ---------------------------------------------------------------- check / assemble


def _make_out_dir(tmp_path, fragments=None, svg=VALID_SVG):
    out_dir = tmp_path / "book"
    (out_dir / "summaries").mkdir(parents=True)
    (out_dir / "diagrams").mkdir()
    manifest = {
        "source": "work/SEC542 - Book 1_2607585_decrypted_stripped.pdf",
        "pages": 20,
        "strategy": "toc+offset",
        "confidence": 0.9,
        "created": "2026-01-01T00:00:00+00:00",
        "chapters": [
            {"index": 0, "title": "前付け", "start": 1, "end": 2, "chars": 10, "file": "chapters/00_front.txt"},
            {"index": 1, "title": "Intro", "start": 3, "end": 8, "chars": 10, "file": "chapters/01_intro.txt"},
            {"index": 2, "title": "Deep", "start": 9, "end": 20, "chars": 10, "file": "chapters/02_deep.txt"},
        ],
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    fragments = fragments or {0: VALID_FRAGMENT, 1: VALID_FRAGMENT, 2: VALID_FRAGMENT}
    for index, fragment in fragments.items():
        (out_dir / "summaries" / f"{index:02d}.html").write_text(fragment, encoding="utf-8")
    if svg:
        (out_dir / "diagrams" / "overview-1.svg").write_text(svg, encoding="utf-8")
    return out_dir


def test_check_passes_valid_dir(tmp_path):
    assert summarize.check(str(_make_out_dir(tmp_path))) == []


def test_check_reports_bad_fragments(tmp_path):
    out_dir = _make_out_dir(
        tmp_path, fragments={0: VALID_FRAGMENT, 1: "<p>lead なし</p>", 2: VALID_FRAGMENT}
    )
    problems = summarize.check(str(out_dir))
    assert any("章 01" in p for p in problems)
    assert any("lead" in p for p in problems)


def test_check_rejects_script_and_external_in_fragment(tmp_path):
    bad = VALID_FRAGMENT + '<script>alert(1)</script><a href="https://example.com">x</a>'
    out_dir = _make_out_dir(tmp_path, fragments={0: VALID_FRAGMENT, 1: bad, 2: VALID_FRAGMENT})
    problems = summarize.check(str(out_dir))
    assert any("script" in p for p in problems)
    assert any("外部参照" in p for p in problems)


def test_check_rejects_dangerous_svg(tmp_path):
    bad_svg = '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    out_dir = _make_out_dir(tmp_path)
    (out_dir / "diagrams" / "ch-01.svg").write_text(bad_svg, encoding="utf-8")
    problems = summarize.check(str(out_dir))
    assert any("script" in p for p in problems)


def test_check_rejects_external_svg_asset(tmp_path):
    bad_svg = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<image href="https://example.com/x.png"/></svg>'
    )
    out_dir = _make_out_dir(tmp_path)
    (out_dir / "diagrams" / "ch-01.svg").write_text(bad_svg, encoding="utf-8")
    problems = summarize.check(str(out_dir))
    assert any("外部" in p for p in problems)


def test_check_rejects_invalid_manifest(tmp_path):
    out_dir = _make_out_dir(tmp_path)
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest["chapters"][1]["end"] = 999
    (out_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ExtractError):
        summarize.check(str(out_dir))


def test_assemble_builds_single_html(tmp_path):
    out_dir = _make_out_dir(tmp_path)
    output = summarize.assemble(str(out_dir))

    html = open(output, encoding="utf-8").read()
    assert output.endswith("_summary.html")
    assert "<!DOCTYPE html>" in html
    assert html.count("<section") == 3
    assert 'href="#ch-1"' in html
    assert "p.3–8" in html
    assert html.count("<svg") == 1
    assert summarize.validate_final_html(html) == []


def test_assemble_refuses_invalid_fragment(tmp_path):
    out_dir = _make_out_dir(
        tmp_path, fragments={0: VALID_FRAGMENT, 1: "<p>短い</p>", 2: VALID_FRAGMENT}
    )
    with pytest.raises(ExtractError):
        summarize.assemble(str(out_dir))


def test_prompt_contains_chapters(tmp_path):
    out_dir = _make_out_dir(tmp_path)
    path = summarize.write_prompt(str(out_dir), language="ja")
    text = open(path, encoding="utf-8").read()
    assert "chapters/01_intro.txt" in text
    assert "01.html" in text
    assert '<p class="lead">' in text


# ---------------------------------------------------------------- 敵対的ケース


def test_manifest_index0_requires_front_flag_or_title(tmp_path):
    out_dir = _make_out_dir(tmp_path)
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest["chapters"][0]["title"] = "Intro"
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ExtractError):
        summarize.load_manifest(str(out_dir))


def test_fragment_unclosed_tags_reported(tmp_path):
    broken = VALID_FRAGMENT.replace("</ul>", "")
    out_dir = _make_out_dir(tmp_path, fragments={0: VALID_FRAGMENT, 1: broken, 2: VALID_FRAGMENT})
    problems = summarize.check(str(out_dir))
    assert any("閉じられていない" in p for p in problems)


def test_fragment_event_handler_and_unknown_tag(tmp_path):
    bad = VALID_FRAGMENT + '<video src="x"></video>'
    bad = bad.replace('<p class="lead">', '<p class="lead" onclick="alert(1)">')
    out_dir = _make_out_dir(tmp_path, fragments={0: VALID_FRAGMENT, 1: bad, 2: VALID_FRAGMENT})
    problems = summarize.check(str(out_dir))
    assert any("onclick" in p or "イベント" in p for p in problems)
    assert any("video" in p for p in problems)


def test_svg_rejects_image_and_external_xlink(tmp_path):
    bad_svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
        '<image xlink:href="https://example.com/a.png"/></svg>'
    )
    out_dir = _make_out_dir(tmp_path)
    (out_dir / "diagrams" / "ch-01.svg").write_text(bad_svg, encoding="utf-8")
    problems = summarize.check(str(out_dir))
    assert any("image" in p for p in problems)
    assert any("外部" in p for p in problems)


def test_safe_removal_rejects_directory_with_pdfs(tmp_path):
    target = tmp_path / "pdfs"
    target.mkdir()
    (target / "x.pdf").write_bytes(b"%PDF-1.4")
    with pytest.raises(ExtractError):
        summarize._safe_removal_target(target)


def test_prompt_uses_japanese_label(tmp_path):
    out_dir = _make_out_dir(tmp_path)
    text = open(summarize.write_prompt(str(out_dir), language="ja"), encoding="utf-8").read()
    assert "日本語の技術要約" in text
    assert "jaの" not in text


def test_toc_pages_invalid_value(tmp_path):
    pdf = tmp_path / "in.pdf"
    _make_text_pdf(pdf, [f"page {i}" for i in range(1, 6)])
    assert summarize.main(["extract", str(pdf), "--toc-pages", "a,b", "--out-dir", str(tmp_path / "o")]) == 1


def test_manual_chapters_record_uncovered_pages(tmp_path):
    pdf = tmp_path / "in.pdf"
    _make_text_pdf(pdf, [f"page {i}" for i in range(1, 7)])
    chapters = tmp_path / "ch.json"
    chapters.write_text(json.dumps([{"title": "A", "start": 2, "end": 3}]), encoding="utf-8")
    out_dir = tmp_path / "out"
    manifest = summarize.extract(str(pdf), str(out_dir), {"manual": str(chapters)})
    assert manifest["uncovered_pages"] == [4, 5, 6]


def test_duplicate_manifest_index_rejected(tmp_path):
    out_dir = _make_out_dir(tmp_path)
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest["chapters"][2]["index"] = 1
    (out_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ExtractError):
        summarize.load_manifest(str(out_dir))


def test_check_reports_invalid_utf8_fragment(tmp_path):
    out_dir = _make_out_dir(tmp_path)
    (out_dir / "summaries" / "01.html").write_bytes(b"\xff\xfe\x00bad")
    problems = summarize.check(str(out_dir))
    assert any("読めません" in p for p in problems)


def test_load_manifest_rejects_broken_json(tmp_path):
    out_dir = _make_out_dir(tmp_path)
    (out_dir / "manifest.json").write_text("{", encoding="utf-8")
    with pytest.raises(ExtractError):
        summarize.load_manifest(str(out_dir))


def test_check_rejects_blank_chapter_title(tmp_path):
    out_dir = _make_out_dir(tmp_path)
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest["chapters"][1]["title"] = "  "
    (out_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ExtractError):
        summarize.load_manifest(str(out_dir))


def test_out_of_range_toc_pages_are_ignored(tmp_path):
    pdf = tmp_path / "in.pdf"
    _make_text_pdf(pdf, [f"page {i}" for i in range(1, 6)], outline=[("A", 0), ("B", 2)])
    out_dir = tmp_path / "out"
    manifest = summarize.extract(
        str(pdf), str(out_dir), {"toc_pages": [999, -1], "profile": "generic"}
    )
    assert manifest["strategy"] == "outline"


def test_svg_rejects_data_uri_reference(tmp_path):
    bad_svg = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<use href="data:image/svg+xml;base64,PHN2Zz48L3N2Zz4="/></svg>'
    )
    out_dir = _make_out_dir(tmp_path)
    (out_dir / "diagrams" / "ch-01.svg").write_text(bad_svg, encoding="utf-8")
    problems = summarize.check(str(out_dir))
    assert any("data:" in p for p in problems)


def test_safe_removal_rejects_source_directory(tmp_path):
    target = tmp_path / "src"
    target.mkdir()
    (target / "tool.py").write_text("print('x')")
    with pytest.raises(ExtractError):
        summarize._safe_removal_target(target)
