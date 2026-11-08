from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
)

import strip


def build_pdf(path, with_outline=False):
    """本文を Form XObject に、透かしテキストをページ直下に持つ PDF を作る。"""
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)

    form = DecodedStreamObject()
    form.set_data(b"0 0 1 rg 10 10 100 100 re f")
    form[NameObject("/Type")] = NameObject("/XObject")
    form[NameObject("/Subtype")] = NameObject("/Form")
    form[NameObject("/BBox")] = ArrayObject(
        [NumberObject(0), NumberObject(0), NumberObject(300), NumberObject(300)]
    )

    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )

    resources = DictionaryObject()
    resources[NameObject("/XObject")] = DictionaryObject(
        {NameObject("/Fm1"): writer._add_object(form)}
    )
    resources[NameObject("/Font")] = DictionaryObject(
        {NameObject("/F1"): writer._add_object(font)}
    )
    page[NameObject("/Resources")] = resources

    content = DecodedStreamObject()
    content.set_data(
        b"q /Fm1 Do Q BT /F1 12 Tf 20 280 Td (SECRET WATERMARK) Tj ET"
    )
    page.replace_contents(content)

    if with_outline:
        writer.add_outline_item("Chapter 1", 0)

    with open(path, "wb") as output:
        writer.write(output)
    return path


def operators_of(page):
    return {operator for _, operator in page.get_contents().operations}


def test_strip_removes_text_but_keeps_form(tmp_path):
    source = build_pdf(tmp_path / "in.pdf")
    output = tmp_path / "out.pdf"

    assert strip.main([str(source), str(output)]) == 0

    page = PdfReader(output).pages[0]
    assert (page.extract_text() or "").strip() == ""
    operators = operators_of(page)
    assert b"Do" in operators
    assert b"Tj" not in operators
    assert b"TJ" not in operators
    assert "/Fm1" in page["/Resources"]["/XObject"]


def test_strip_preserves_outline(tmp_path):
    source = build_pdf(tmp_path / "in.pdf", with_outline=True)
    output = tmp_path / "out.pdf"

    assert strip.main([str(source), str(output)]) == 0
    assert len(PdfReader(output).outline) == 1


def test_strip_dry_run_does_not_write(tmp_path):
    source = build_pdf(tmp_path / "in.pdf")

    assert strip.main([str(source), "--dry-run"]) == 0
    assert not (tmp_path / "in_stripped.pdf").exists()


def test_strip_default_output_name(tmp_path):
    source = build_pdf(tmp_path / "in.pdf")

    assert strip.main([str(source)]) == 0
    assert (tmp_path / "in_stripped.pdf").exists()


def test_strip_rejects_same_path(tmp_path):
    source = build_pdf(tmp_path / "in.pdf")

    assert strip.main([str(source), str(source)]) == 1
