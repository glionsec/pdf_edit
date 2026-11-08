import argparse
import os
import sys

from pypdf import PasswordType, PdfReader, PdfWriter


def split_pdf(file_path, split_points, password=None):
    reader = PdfReader(file_path)
    if reader.is_encrypted:
        if password is None:
            raise RuntimeError(
                f"{file_path} は暗号化されています。"
                "unlock.py で解除するか --password を指定してください。"
            )
        if reader.decrypt(password) == PasswordType.NOT_DECRYPTED:
            raise RuntimeError(
                f"{file_path} の復号に失敗しました（パスワードが違う可能性）"
            )

    total_pages = len(reader.pages)

    # Split points should be sorted and unique
    split_points = sorted(set(split_points))
    split_points = [point for point in split_points if 0 < point < total_pages]

    # Add start and end points
    split_points = [0] + split_points + [total_pages]

    base_name = os.path.splitext(os.path.basename(file_path))[0]
    directory = os.path.dirname(file_path)

    for i in range(len(split_points) - 1):
        pdf_writer = PdfWriter()
        start = split_points[i]
        end = split_points[i + 1]

        for page_num in range(start, end):
            pdf_writer.add_page(reader.pages[page_num])

        output_filename = os.path.join(directory, f"{base_name}_part{i+1}.pdf")
        with open(output_filename, "wb") as output_pdf:
            pdf_writer.write(output_pdf)

    print("PDF has been split successfully.")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Split a PDF at specified pages")
    parser.add_argument("pdf", nargs="?", help="対象 PDF")
    parser.add_argument(
        "pages", nargs="?", help="区切りページ（カンマ区切り、例: 2,5）"
    )
    parser.add_argument("--password", help="復号パスワード（暗号化 PDF の場合）")
    args = parser.parse_args(argv)

    file_path = args.pdf or input("Enter the path to the PDF file: ").strip()
    if not os.path.isfile(file_path):
        print("File not found.")
        return 1

    pages_input = args.pages or input(
        "Enter the page numbers to split the PDF at (comma-separated): "
    ).strip()
    try:
        split_points = [int(num) for num in pages_input.split(",")]
    except ValueError:
        print("Invalid input. Please enter valid page numbers.")
        return 1

    try:
        split_pdf(file_path, split_points, password=args.password)
    except (ValueError, RuntimeError) as exc:
        print(f"エラー: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
