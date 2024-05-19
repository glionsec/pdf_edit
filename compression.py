import subprocess

def compress_pdf(input_path, output_path, quality):
    gs_command = [
        "gs",
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.4",
        f"-dPDFSETTINGS={quality}",
        "-dNOPAUSE",
        "-dQUIET",
        "-dBATCH",
        f"-sOutputFile={output_path}",
        input_path
    ]
    subprocess.run(gs_command)
    print(f"Compressed PDF saved as: {output_path}")

def main():
    input_path = input("Enter the path to the PDF file: ").strip()
    output_path = input("Enter the path for the compressed PDF file: ").strip()
    quality = input("Enter the PDF compression quality (/screen, /ebook, /printer, /prepress, /default): ").strip()
    compress_pdf(input_path, output_path, quality)

if __name__ == "__main__":
    main()
