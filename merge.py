import argparse
import os
import re
import sys

from pypdf import PasswordType, PdfReader, PdfWriter


def natural_sort_key(name):
    """ファイル名を自然順に並べるためのキー（10.pdf を 2.pdf の後ろにする）。"""
    return [
        (0, int(part)) if part.isdigit() else (1, part.lower())
        for part in re.split(r"(\d+)", name)
    ]


def merge_pdfs(folder_path, output_path, password=None):
    if not os.path.isdir(folder_path):
        raise ValueError(f"フォルダが見つかりません: {folder_path}")

    pdf_files = sorted(
        (name for name in os.listdir(folder_path) if name.lower().endswith(".pdf")),
        key=natural_sort_key,
    )
    if not pdf_files:
        raise ValueError(f"PDF が見つかりません: {folder_path}")

    readers = []
    for pdf_file in pdf_files:
        reader = PdfReader(os.path.join(folder_path, pdf_file))
        if reader.is_encrypted:
            if password is None:
                raise RuntimeError(
                    f"{pdf_file} は暗号化されています。"
                    "unlock.py で解除するか --password を指定してください。"
                )
            if reader.decrypt(password) == PasswordType.NOT_DECRYPTED:
                raise RuntimeError(
                    f"{pdf_file} の復号に失敗しました（パスワードが違う可能性）"
                )
        readers.append(reader)

    pdf_writer = PdfWriter()
    for reader in readers:
        for page in reader.pages:
            pdf_writer.add_page(page)

    with open(output_path, "wb") as output_pdf:
        pdf_writer.write(output_pdf)

    print(f"Merged PDF saved as: {output_path}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Merge PDFs in a folder")
    parser.add_argument("folder", nargs="?", help="PDF を含むフォルダ")
    parser.add_argument("output", nargs="?", help="出力 PDF")
    parser.add_argument("--password", help="復号パスワード（暗号化 PDF を含む場合）")
    args = parser.parse_args(argv)

    folder_path = args.folder or input(
        "Enter the path to the folder containing PDF files: "
    ).strip()
    output_path = (
        args.output or input("Enter the path for the merged PDF file: ").strip()
    )

    try:
        merge_pdfs(folder_path, output_path, password=args.password)
    except (ValueError, RuntimeError) as exc:
        print(f"エラー: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
