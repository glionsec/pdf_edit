import subprocess

def compress_pdf(input_path, output_path):
    gs_command = [
        "gs",
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.4",
        "-dPDFSETTINGS=/screen",
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
    compress_pdf(input_path, output_path)

if __name__ == "__main__":
    main()
