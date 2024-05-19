import os
import PyPDF2

def merge_pdfs(folder_path, output_path):
    pdf_writer = PyPDF2.PdfWriter()

    # Get list of PDF files in the folder and sort them by name
    pdf_files = sorted([f for f in os.listdir(folder_path) if f.endswith('.pdf')])

    for pdf_file in pdf_files:
        pdf_path = os.path.join(folder_path, pdf_file)
        pdf_reader = PyPDF2.PdfReader(pdf_path)

        for page_num in range(len(pdf_reader.pages)):
            pdf_writer.add_page(pdf_reader.pages[page_num])

    with open(output_path, 'wb') as output_pdf:
        pdf_writer.write(output_pdf)

    print(f"Merged PDF saved as: {output_path}")

def main():
    folder_path = input("Enter the path to the folder containing PDF files: ").strip()
    if not os.path.isdir(folder_path):
        print("Folder not found.")
        return

    output_path = input("Enter the path for the merged PDF file: ").strip()
    merge_pdfs(folder_path, output_path)

if __name__ == "__main__":
    main()
