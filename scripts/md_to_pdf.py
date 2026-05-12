import sys
from pathlib import Path
from markdown import markdown
from weasyprint import HTML, CSS
from weasyprint.text.fonts import FontConfiguration

def md_to_pdf(input_md: str, output_pdf: str = None):
    input_path = Path(input_md)
    if not input_path.exists():
        print(f"Error: File '{input_md}' not found!")
        return

    if output_pdf is None:
        output_pdf = input_path.with_suffix('.pdf')

    # Read Markdown
    with open(input_path, encoding='utf-8') as f:
        md_content = f.read()

    # Convert Markdown to HTML
    html_body = markdown(md_content, extensions=['extra', 'tables', 'fenced_code'])

    # Full HTML + CSS with page numbers and full-page background
    full_html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            @page {{
                size: A4;
                margin: 0.8cm;
                background-color: #f8f6f0;
                @bottom-center {{
                    content: "Page " counter(page) " of " counter(pages);
                    font-family: "Georgia", serif;
                    font-size: 9.5pt;
                    color: #666666;
                    padding-top: 8px;
                }}
            }}

            body {{
                margin: 0;
                padding: 0.5cm;
                font-family: "Georgia", "DejaVu Serif", serif;
                font-size: 11.5pt;
                line-height: 1.65;
                color: #2c2c2c;
                background-color: transparent;
            }}

            img {{
                max-width: 100%;
                height: auto;
                display: block;
                margin: 18px auto;
                border-radius: 5px;
            }}

            h1, h2, h3, h4 {{
                color: #2c3e50;
                margin-top: 1.8em;
                margin-bottom: 0.7em;
            }}
            h1 {{ 
                font-size: 1.85em; 
                text-align: center; 
                margin-bottom: 1.2em;
            }}

            pre, code {{
                background-color: #f0ede6;
                border-radius: 4px;
            }}

            blockquote {{
                border-left: 5px solid #d4b38a;
                padding-left: 1.2em;
                color: #555;
                font-style: italic;
                margin: 1.5em 0;
            }}
        </style>
    </head>
    <body>
        {html_body}
    </body>
    </html>
    """

    font_config = FontConfiguration()
    html_doc = HTML(string=full_html, base_url=str(input_path.parent))

    html_doc.write_pdf(
        output_pdf,
        font_config=font_config
    )

    print(f"✅ PDF created successfully with page numbers and full-page background:")
    print(f"   {output_pdf}")


def main():
    if len(sys.argv) < 2:
        print("Usage: python md_to_pdf.py your_file.md [output.pdf]")
        sys.exit(1)

    input_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else None
    md_to_pdf(input_file, output_file)

if __name__ == "__main__":
    main()
