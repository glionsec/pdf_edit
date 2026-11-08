import argparse
import os
import sys

from pypdf import PasswordType, PdfReader, PdfWriter


def unlock_pdfs(files=None, password=None, force=False):
    if files is None:
        files = sorted(
            name for name in os.listdir() if name.lower().endswith(".pdf")
        )
    if not files:
        raise ValueError("PDF が見つかりません")

    failures = 0
    for pdf_file in files:
        print(f"処理中: {pdf_file}")
        try:
            pdf_reader = PdfReader(pdf_file)
            if pdf_reader.is_encrypted:
                pw = password or input("PDFのパスワードを入力してください: ")
                if pdf_reader.decrypt(pw) == PasswordType.NOT_DECRYPTED:
                    print(f"エラー: パスワードが違います: {pdf_file}")
                    failures += 1
                    continue

            output_file = os.path.splitext(pdf_file)[0] + "_ja.pdf"
            if os.path.exists(output_file) and not force:
                answer = input(
                    f"{output_file} は既に存在します。上書きしますか？ [y/N]: "
                ).strip().lower()
                if answer not in ("y", "yes"):
                    print(f"スキップ: {pdf_file}")
                    continue

            pdf_writer = PdfWriter()
            # ページをコピーするのではなく文書ごと複製し、しおりやメタデータも保持する
            pdf_writer.clone_document_from_reader(pdf_reader)
            with open(output_file, "wb") as output_pdf:
                pdf_writer.write(output_pdf)
            print(f"処理完了: {output_file}")
        except Exception as exc:
            print(f"エラー: {exc}")
            failures += 1

    print("処理が完了しました。")
    return 1 if failures else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Remove PDF passwords")
    parser.add_argument(
        "files", nargs="*", help="対象 PDF（省略時はカレントディレクトリの *.pdf）"
    )
    parser.add_argument("--password", help="復号パスワード（省略時は対話入力）")
    parser.add_argument(
        "--force", action="store_true", help="既存の出力を確認せず上書きする"
    )
    args = parser.parse_args(argv)

    try:
        return unlock_pdfs(args.files or None, password=args.password, force=args.force)
    except ValueError as exc:
        print(f"エラー: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
