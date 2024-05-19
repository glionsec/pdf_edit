import PyPDF2
import os

def split_pdf(file_path, split_points):
    pdf_reader = PyPDF2.PdfReader(file_path)
    total_pages = len(pdf_reader.pages)

    # Split points should be sorted and unique
    split_points = sorted(set(split_points))
    split_points = [point for point in split_points if 0 < point < total_pages]

    # Add start and end points
    split_points = [0] + split_points + [total_pages]

    base_name = os.path.splitext(os.path.basename(file_path))[0]
    directory = os.path.dirname(file_path)

    for i in range(len(split_points) - 1):
        pdf_writer = PyPDF2.PdfWriter()
        start = split_points[i]
        end = split_points[i + 1]

        for page_num in range(start, end):
            pdf_writer.add_page(pdf_reader.pages[page_num])

        output_filename = os.path.join(directory, f"{base_name}_part{i+1}.pdf")
        with open(output_filename, "wb") as output_pdf:
            pdf_writer.write(output_pdf)

    print("PDF has been split successfully.")

def main():
    file_path = input("Enter the path to the PDF file: ").strip()
    if not os.path.isfile(file_path):
        print("File not found.")
        return

    split_input = input("Enter the page numbers to split the PDF at (comma-separated): ").strip()
    try:
        split_points = [int(num) for num in split_input.split(",")]
    except ValueError:
        print("Invalid input. Please enter valid page numbers.")
        return

    split_pdf(file_path, split_points)

if __name__ == "__main__":
    main()
