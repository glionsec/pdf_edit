import argparse
import os
import shutil
import subprocess
import sys

QUALITY_CHOICES = ("/screen", "/ebook", "/printer", "/prepress", "/default")


def compress_pdf(input_path, output_path, quality):
    if quality not in QUALITY_CHOICES:
        raise ValueError(
            f"不正な品質です: {quality}（{' / '.join(QUALITY_CHOICES)} のいずれかを指定）"
        )

    if shutil.which("gs") is None:
        raise RuntimeError(
            "ghostscript (gs) が見つかりません。README の手順で導入してください。"
        )

    gs_command = [
        "gs",
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.4",
        f"-dPDFSETTINGS={quality}",
        "-dNOPAUSE",
        "-dQUIET",
        "-dBATCH",
        f"-sOutputFile={output_path}",
        input_path,
    ]
    result = subprocess.run(gs_command, capture_output=True, text=True)
    if result.returncode != 0:
        if os.path.exists(output_path):
            os.remove(output_path)
        raise RuntimeError(
            f"ghostscript が失敗しました (exit {result.returncode}): {result.stderr.strip()}"
        )

    print(f"Compressed PDF saved as: {output_path}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Compress a PDF with ghostscript")
    parser.add_argument("input", nargs="?", help="入力 PDF")
    parser.add_argument("output", nargs="?", help="出力 PDF")
    parser.add_argument(
        "quality", nargs="?", help="/screen, /ebook, /printer, /prepress, /default"
    )
    args = parser.parse_args(argv)

    input_path = args.input or input("Enter the path to the PDF file: ").strip()
    output_path = (
        args.output or input("Enter the path for the compressed PDF file: ").strip()
    )
    quality = args.quality or input(
        "Enter the PDF compression quality (/screen, /ebook, /printer, /prepress, /default): "
    ).strip()

    try:
        compress_pdf(input_path, output_path, quality)
    except (ValueError, RuntimeError) as exc:
        print(f"エラー: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
