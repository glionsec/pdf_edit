import shutil

import pytest
from pypdf import PdfReader, PdfWriter

import compression
import merge
import split
import unlock


def make_pdf(path, pages=2, base_width=595):
    writer = PdfWriter()
    for i in range(pages):
        writer.add_blank_page(width=base_width + i, height=842)
    with open(path, "wb") as writer_file:
        writer.write(writer_file)
    return path


def make_encrypted_pdf(path, password="secret", pages=2, outline=False):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    if outline:
        writer.add_outline_item("Chapter 1", 0)
    writer.encrypt(password, algorithm="AES-256")
    with open(path, "wb") as writer_file:
        writer.write(writer_file)
    return path


def page_widths(path):
    return [round(float(page.mediabox.width)) for page in PdfReader(path).pages]


def test_merge_uses_natural_order(tmp_path):
    make_pdf(tmp_path / "2.pdf", pages=1, base_width=200)
    make_pdf(tmp_path / "10.pdf", pages=1, base_width=300)

    output = tmp_path / "merged.pdf"
    assert merge.main([str(tmp_path), str(output)]) == 0
    assert page_widths(output) == [200, 300]


def test_merge_interactive_fallback(tmp_path, monkeypatch):
    make_pdf(tmp_path / "a.pdf", pages=1)
    output = tmp_path / "merged.pdf"
    answers = iter([str(tmp_path), str(output)])
    monkeypatch.setattr("builtins.input", lambda *args: next(answers))

    assert merge.main([]) == 0
    assert output.exists()


def test_merge_rejects_encrypted_without_password(tmp_path, capsys):
    make_encrypted_pdf(tmp_path / "enc.pdf")

    assert merge.main([str(tmp_path), str(tmp_path / "out.pdf")]) == 1
    assert "暗号化" in capsys.readouterr().out
    assert not (tmp_path / "out.pdf").exists()


def test_merge_decrypts_with_password(tmp_path):
    make_encrypted_pdf(tmp_path / "enc.pdf")
    output = tmp_path / "out.pdf"

    assert merge.main([str(tmp_path), str(output), "--password", "secret"]) == 0
    assert len(PdfReader(output).pages) == 2


def test_split_splits_at_page(tmp_path):
    pdf = make_pdf(tmp_path / "doc.pdf", pages=4, base_width=100)

    assert split.main([str(pdf), "2"]) == 0
    assert page_widths(tmp_path / "doc_part1.pdf") == [100, 101]
    assert page_widths(tmp_path / "doc_part2.pdf") == [102, 103]


def test_split_rejects_encrypted_without_password(tmp_path, capsys):
    pdf = make_encrypted_pdf(tmp_path / "enc.pdf")

    assert split.main([str(pdf), "1"]) == 1
    assert "暗号化" in capsys.readouterr().out


def test_unlock_decrypts_aes(tmp_path):
    pdf = make_encrypted_pdf(tmp_path / "enc.pdf")
    output = tmp_path / "enc_decrypted.pdf"

    assert unlock.main([str(pdf), "--password", "secret", "--force"]) == 0
    reader = PdfReader(output)
    assert len(reader.pages) == 2
    assert not reader.is_encrypted


def test_unlock_preserves_outline_and_pages(tmp_path):
    pdf = make_encrypted_pdf(tmp_path / "enc_outline.pdf", outline=True)
    output = tmp_path / "enc_outline_decrypted.pdf"

    assert unlock.main([str(pdf), "--password", "secret", "--force"]) == 0
    reader = PdfReader(output)
    assert len(reader.pages) == 2
    assert not reader.is_encrypted
    assert len(reader.outline) == 1


def test_unlock_reports_wrong_password(tmp_path, capsys):
    pdf = make_encrypted_pdf(tmp_path / "enc.pdf")

    assert unlock.main([str(pdf), "--password", "wrong", "--force"]) == 1
    assert "パスワードが違います" in capsys.readouterr().out
    assert not (tmp_path / "enc_decrypted.pdf").exists()


def test_unlock_does_not_prompt_for_unencrypted(tmp_path, monkeypatch):
    pdf = make_pdf(tmp_path / "plain.pdf", pages=1)

    def fail_input(*args):
        raise AssertionError("input() must not be called for unencrypted PDFs")

    monkeypatch.setattr("builtins.input", fail_input)

    assert unlock.main([str(pdf), "--force"]) == 0
    assert (tmp_path / "plain_decrypted.pdf").exists()


def test_unlock_does_not_overwrite_without_confirmation(tmp_path, monkeypatch):
    pdf = make_pdf(tmp_path / "plain.pdf", pages=1)
    output = tmp_path / "plain_decrypted.pdf"
    output.write_bytes(b"keep")

    monkeypatch.setattr("builtins.input", lambda *args: "n")

    assert unlock.main([str(pdf)]) == 0
    assert output.read_bytes() == b"keep"


def test_compression_rejects_invalid_quality(tmp_path, capsys):
    assert compression.main([str(tmp_path / "in.pdf"), str(tmp_path / "out.pdf"), "/bogus"]) == 1
    assert "不正な品質" in capsys.readouterr().out


def test_compression_reports_missing_gs(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(compression.shutil, "which", lambda name: None)

    assert compression.main([str(tmp_path / "in.pdf"), str(tmp_path / "out.pdf"), "/ebook"]) == 1
    assert "ghostscript" in capsys.readouterr().out


class _FailedResult:
    returncode = 1
    stderr = "boom"


def test_compression_failure_removes_output(tmp_path, monkeypatch):
    monkeypatch.setattr(compression.shutil, "which", lambda name: "/usr/bin/gs")
    monkeypatch.setattr(compression.subprocess, "run", lambda *args, **kwargs: _FailedResult())

    output = tmp_path / "out.pdf"
    output.write_bytes(b"x")

    with pytest.raises(RuntimeError):
        compression.compress_pdf(str(tmp_path / "in.pdf"), str(output), "/ebook")
    assert not output.exists()


@pytest.mark.skipif(shutil.which("gs") is None, reason="ghostscript が無い")
def test_compression_runs_with_real_gs(tmp_path):
    pdf = make_pdf(tmp_path / "in.pdf", pages=2)
    output = tmp_path / "out.pdf"

    assert compression.main([str(pdf), str(output), "/ebook"]) == 0
    assert len(PdfReader(output).pages) == 2
