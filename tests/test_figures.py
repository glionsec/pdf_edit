import json
import os
import struct
import zlib

import pytest
from pypdf import PdfWriter
from pypdf.generic import (
    DecodedStreamObject,
    DictionaryObject,
    EncodedStreamObject,
    NameObject,
    NumberObject,
)

import figures
from summarize import ExtractError


def make_flate_image(width, height, rgb, color_space="/DeviceRGB"):
    stream = EncodedStreamObject()
    stream[NameObject("/Type")] = NameObject("/XObject")
    stream[NameObject("/Subtype")] = NameObject("/Image")
    stream[NameObject("/Width")] = NumberObject(width)
    stream[NameObject("/Height")] = NumberObject(height)
    stream[NameObject("/ColorSpace")] = NameObject(color_space)
    stream[NameObject("/BitsPerComponent")] = NumberObject(8)
    stream[NameObject("/Filter")] = NameObject("/FlateDecode")
    # pypdf 7 の EncodedStreamObject は _data を直接持たないと set_data できない
    stream._data = zlib.compress(rgb)
    return stream


def make_pdf_with_images(path, page_images):
    """page_images: list of list[image stream]（各要素が 1 ページ分）"""
    writer = PdfWriter()
    for images in page_images:
        page = writer.add_blank_page(width=300, height=300)
        xobjects = DictionaryObject()
        for index, image in enumerate(images):
            xobjects[NameObject(f"/Im{index}")] = writer._add_object(image)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/XObject"): xobjects}
        )
        content = DecodedStreamObject()
        content.set_data(b"")
        page.replace_contents(content)
    with open(path, "wb") as handle:
        writer.write(handle)


def make_manifest(out_dir, pages=4, chapters=None):
    os.makedirs(out_dir, exist_ok=True)
    manifest = {
        "source": "work/test.pdf",
        "pages": pages,
        "strategy": "manual",
        "confidence": 1.0,
        "created": "2026-01-01T00:00:00+00:00",
        "chapters": chapters
        or [{"index": 1, "title": "Chapter", "start": 1, "end": pages, "chars": 10, "file": "chapters/01.txt"}],
    }
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False)


# ---------------------------------------------------------------- エンコード


def test_png_rgb_header():
    data = figures.png_encode_rgb(3, 2, b"\x00" * (3 * 2 * 3))
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", data[16:24])
    assert (width, height) == (3, 2)


def test_decode_flate_rgb():
    image = make_flate_image(2, 2, b"\x10\x20\x30" * 4)
    extension, payload = figures.decode_image(image)
    assert extension == "png"
    assert payload.startswith(b"\x89PNG")


def test_decode_dct_uses_raw_jpeg_bytes():
    stream = EncodedStreamObject()
    stream[NameObject("/Filter")] = NameObject("/DCTDecode")
    stream[NameObject("/Width")] = NumberObject(10)
    stream[NameObject("/Height")] = NumberObject(10)
    stream._data = b"\xff\xd8\xff\xe0fake-jpeg"
    extension, payload = figures.decode_image(stream)
    assert extension == "jpg"
    assert payload.startswith(b"\xff\xd8")


# ---------------------------------------------------------------- 選抜


def _candidates(images, widths_heights, pages=None):
    out = []
    for index, ((width, height), image) in enumerate(zip(widths_heights, images)):
        out.append(
            {
                "page": (pages or [1])[0] if pages is None else pages[index],
                "name": f"/Im{index}",
                "width": width,
                "height": height,
                "bytes": len(getattr(image, "_data", b"")),
                "filter": "/FlateDecode",
                "hash": figures._hash(getattr(image, "_data", b"")),
                "image": image,
            }
        )
    return out


def test_select_filters_templates_duplicates_and_extremes():
    big = make_flate_image(100, 100, b"\x00" * (100 * 100 * 3))
    template = make_flate_image(100, 100, b"\x01" * (100 * 100 * 3))
    wide = make_flate_image(1000, 50, b"\x02" * (1000 * 50 * 3))
    tiny = make_flate_image(10, 10, b"\x03" * (10 * 10 * 3))
    unique = make_flate_image(200, 200, b"\x04" * (200 * 200 * 3))

    candidates = [
        {"page": 1, "name": "/A", "width": 100, "height": 100, "bytes": 10, "filter": "/FlateDecode", "hash": "same-big", "image": big},
        {"page": 2, "name": "/B", "width": 100, "height": 100, "bytes": 10, "filter": "/FlateDecode", "hash": "same-big", "image": big},
        {"page": 3, "name": "/C", "width": 100, "height": 100, "bytes": 10, "filter": "/FlateDecode", "hash": "same-big", "image": big},
        {"page": 4, "name": "/D", "width": 100, "height": 100, "bytes": 10, "filter": "/FlateDecode", "hash": "same-big", "image": big},
        {"page": 5, "name": "/E", "width": 100, "height": 100, "bytes": 10, "filter": "/FlateDecode", "hash": "same-big", "image": big},
        {"page": 6, "name": "/F", "width": 100, "height": 100, "bytes": 10, "filter": "/FlateDecode", "hash": "same-big", "image": big},
        {"page": 7, "name": "/T", "width": 100, "height": 100, "bytes": 10, "filter": "/FlateDecode", "hash": "template", "image": template},
        {"page": 7, "name": "/W", "width": 1000, "height": 50, "bytes": 10, "filter": "/FlateDecode", "hash": "wide", "image": wide},
        {"page": 8, "name": "/X", "width": 10, "height": 10, "bytes": 10, "filter": "/FlateDecode", "hash": "tiny", "image": tiny},
        {"page": 9, "name": "/U", "width": 200, "height": 200, "bytes": 10, "filter": "/FlateDecode", "hash": "unique", "image": unique},
    ]
    chapters = [{"index": 1, "title": "C", "start": 1, "end": 20}]
    selected, stats = figures.select_candidates(candidates, chapters, per_chapter=3)
    names = [c["name"] for c in selected]
    assert "/U" in names
    assert "/X" not in names  # 小さすぎる
    assert "/W" not in names  # 縦横比が極端
    assert names.count("/A") + names.count("/B") + names.count("/C") == 0  # 同一画像の使い回し
    assert stats["selected"] == len(selected)


def test_select_respects_total_byte_cap():
    a = make_flate_image(100, 100, b"\x05" * (100 * 100 * 3))
    b = make_flate_image(120, 120, b"\x06" * (120 * 120 * 3))
    candidates = _candidates([a, b], [(100, 100), (120, 120)])
    chapters = [{"index": 1, "title": "C", "start": 1, "end": 2}]
    selected, stats = figures.select_candidates(
        candidates, chapters, per_chapter=2, max_total_bytes=1000
    )
    assert stats["bytes"] <= 1000
    assert len(selected) <= 1


# ---------------------------------------------------------------- 統合


def test_extract_and_assemble_with_figures(tmp_path):
    pdf = tmp_path / "in.pdf"
    image = make_flate_image(300, 200, b"\x42" * (300 * 200 * 3))
    make_pdf_with_images(pdf, [[image], []])

    out_dir = tmp_path / "book"
    make_manifest(out_dir, pages=2)
    os.makedirs(out_dir / "summaries")
    (out_dir / "summaries" / "01.html").write_text(
        '<p class="lead">この章の目的と位置づけを、学習者向けに説明する導入です。</p><ul>'
        "<li>要点の説明です。手順と確認ポイントを順序立てて整理します。</li>"
        "<li>二つ目の要点です。失敗しやすい箇所と注意点にも触れます。</li>"
        "<li>三つ目の要点です。関連する章や次の学習項目との関係を示します。</li></ul>",
        encoding="utf-8",
    )

    result = figures.extract_figures(str(pdf), str(out_dir), per_chapter=2)
    assert result["stats"]["selected"] == 1
    entry = result["figures"][0]
    assert entry["format"] == "png"
    assert os.path.isfile(out_dir / entry["file"])

    import summarize

    assert summarize.check(str(out_dir)) == []
    output = summarize.assemble(str(out_dir))
    html = open(output, encoding="utf-8").read()
    assert "data:image/png;base64," in html
    assert "https://" not in html
    assert summarize.validate_final_html(html) == []


def test_check_rejects_tampered_figure(tmp_path):
    pdf = tmp_path / "in.pdf"
    image = make_flate_image(300, 200, b"\x42" * (300 * 200 * 3))
    make_pdf_with_images(pdf, [[image]])
    out_dir = tmp_path / "book"
    make_manifest(out_dir, pages=1)
    figures.extract_figures(str(pdf), str(out_dir), per_chapter=1)

    with open(out_dir / "manifest.json", encoding="utf-8") as handle:
        manifest = json.load(handle)
    entry = json.load(open(out_dir / "figures.json", encoding="utf-8"))["figures"][0]
    (out_dir / entry["file"]).write_bytes(b"not an image")

    import summarize

    os.makedirs(out_dir / "summaries", exist_ok=True)
    (out_dir / "summaries" / "01.html").write_text(
        '<p class="lead">この章の目的と位置づけを、学習者向けに説明する導入です。</p><ul>'
        "<li>要点の説明です。手順と確認ポイントを順序立てて整理します。</li>"
        "<li>二つ目の要点です。失敗しやすい箇所と注意点にも触れます。</li>"
        "<li>三つ目の要点です。関連する章や次の学習項目との関係を示します。</li></ul>",
        encoding="utf-8",
    )
    problems = summarize.check(str(out_dir))
    assert any("一致しません" in p or "図版" in p for p in problems)


def test_final_html_rejects_non_image_data_uri():
    import summarize

    problems = summarize.validate_final_html(
        '<html><body><img src="data:text/html;base64,PHNjcmlwdD4="></body></html>'
    )
    assert problems
