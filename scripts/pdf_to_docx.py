"""
pdf_to_docx.py

Convert a PDF file to DOCX using pdf2docx, preserving as much structure and formatting as possible.

Usage:
    python pdf_to_docx.py input.pdf [output.docx]

Requires:
    pip install pdf2docx
"""
import sys
from pdf2docx import Converter
from pathlib import Path

def main():
    if len(sys.argv) < 2:
        print("Usage: python pdf_to_docx.py input.pdf [output.docx]")
        sys.exit(1)
    pdf_path = Path(sys.argv[1])
    if len(sys.argv) >= 3:
        docx_path = Path(sys.argv[2])
    else:
        docx_path = pdf_path.with_suffix('.docx')
    cv = Converter(str(pdf_path))
    cv.convert(str(docx_path), start=0, end=None)
    cv.close()
    print(f"DOCX written: {docx_path}")

if __name__ == "__main__":
    main()
