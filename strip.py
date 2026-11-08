"""ページ直下のテキスト（透かし・ヘッダー・フッター）とメタデータを除去する。

本文が Form XObject 内にあり、ページの content stream に重ね文字だけが描かれている
PDF 向け（例: SANS 教材のライセンス表記と斜めの透かし）。
テキスト表示命令（Tj / TJ / ' / "）だけを落とし、画像・図形・Form XObject の
描画命令はそのまま残す。

標準で以下も削除する（--keep-metadata で無効化できる）:
- 文書情報辞書（/Info）の全項目（Author / Title / Copyright / Hash / Hash2 など）
- ファイル識別子（/ID）を新規生成
- 各オブジェクトの /PieceInfo / /LastModified / /Metadata

注意: 本文のテキストもページ直下に描かれている PDF では本文も消える。
適用前に --dry-run で対象ページ数・命令数を確認すること。
"""
import argparse
import io
import os
import sys

from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    IndirectObject,
    PdfObject,
)

TEXT_SHOW_OPERATORS = {b"Tj", b"TJ", b"'", b'"'}
SCRUB_KEYS = ("/PieceInfo", "/LastModified", "/Metadata")
OUTPUT_SUFFIX = "_stripped"


def serialize_operations(operations):
    """content stream の命令列を PDF のバイト列に戻す。"""
    buffer = io.BytesIO()
    for operands, operator in operations:
        for value in operands:
            if isinstance(value, PdfObject):
                value.write_to_stream(buffer)
            else:
                buffer.write(str(value).encode("latin-1"))
            buffer.write(b" ")
        buffer.write(operator)
        buffer.write(b"\n")
    return buffer.getvalue()


def _scrub_object(obj, seen):
    if isinstance(obj, IndirectObject):
        obj = obj.get_object()
    if id(obj) in seen:
        return
    seen.add(id(obj))
    if isinstance(obj, DictionaryObject):
        for key in SCRUB_KEYS:
            if key in obj:
                del obj[key]
        for value in list(obj.values()):
            _scrub_object(value, seen)
    elif isinstance(obj, ArrayObject):
        for value in obj:
            _scrub_object(value, seen)


def scrub_metadata(writer):
    """文書情報・ファイル識別子・作成履歴を消す。"""
    writer.metadata = None  # /Info を削除
    writer._ID = None  # type: ignore[attr-defined]  # 古い ID を引き継がない
    writer.generate_file_identifiers()

    seen = set()
    for obj in list(getattr(writer, "_objects", [])):
        if obj is not None:
            _scrub_object(obj, seen)


def strip_pdf(input_path, output_path=None, dry_run=False, scrub=True):
    if not os.path.isfile(input_path):
        raise ValueError(f"ファイルが見つかりません: {input_path}")
    if not dry_run and output_path:
        if os.path.abspath(output_path) == os.path.abspath(input_path):
            raise ValueError("入力と出力が同じパスです")

    reader = PdfReader(input_path)
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)

    pages_changed = 0
    removed_total = 0
    for page in writer.pages:
        contents = page.get_contents()
        if contents is None:
            continue
        operations = contents.operations
        kept = [item for item in operations if item[1] not in TEXT_SHOW_OPERATORS]
        removed = len(operations) - len(kept)
        if removed == 0:
            continue
        pages_changed += 1
        removed_total += removed
        if dry_run:
            continue
        new_content = DecodedStreamObject()
        new_content.set_data(serialize_operations(kept))
        page.replace_contents(new_content)

    if dry_run:
        note = "、メタデータも削除" if scrub else ""
        print(
            f"[dry-run] {pages_changed} ページ / {removed_total} 個のテキスト描画命令が対象"
            f"（書き出す場合は追記: {note}）"
        )
        return

    if scrub:
        scrub_metadata(writer)

    with open(output_path, "wb") as output:
        writer.write(output)
    print(
        f"{pages_changed} ページ / {removed_total} 個のテキスト描画命令を除去"
        f"{'（メタデータも削除）' if scrub else '（メタデータは保持）'} -> {output_path}"
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="ページ直下のテキスト（透かし・ヘッダー・フッター）とメタデータを除去する"
    )
    parser.add_argument("input", nargs="?", help="入力 PDF")
    parser.add_argument(
        "output", nargs="?", help="出力 PDF（省略時は <入力>_stripped.pdf）"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="書き出さずに対象ページ数・命令数のみ表示"
    )
    parser.add_argument(
        "--keep-metadata",
        action="store_true",
        help="文書情報・ID・PieceInfo を消さずに残す",
    )
    args = parser.parse_args(argv)

    input_path = args.input or input("Enter the path to the PDF file: ").strip()
    if not os.path.isfile(input_path):
        print("File not found.")
        return 1

    output_path = args.output
    if output_path is None and not args.dry_run:
        output_path = os.path.splitext(input_path)[0] + OUTPUT_SUFFIX + ".pdf"

    try:
        strip_pdf(
            input_path,
            output_path,
            dry_run=args.dry_run,
            scrub=not args.keep_metadata,
        )
    except (ValueError, RuntimeError) as exc:
        print(f"エラー: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
