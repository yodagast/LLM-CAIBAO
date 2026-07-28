---
name: pdf-processing
description: Extract text and tables from PDF files, fill forms, merge documents. Use when working with PDF files or when the user mentions PDFs, forms, or document extraction.
---

# PDF Processing

## Quick Start

Use `pdfplumber` for text and table extraction from PDF files.

### Installation

```bash
pip install pdfplumber
```

### Extract Text

```python
import pdfplumber

with pdfplumber.open("file.pdf") as pdf:
    text = pdf.pages[0].extract_text()
```

### Extract Tables

```python
import pdfplumber

with pdfplumber.open("file.pdf") as pdf:
    tables = pdf.pages[0].extract_tables()
```

## Common Workflows

### Batch Process Multiple PDFs

```python
import pdfplumber
from pathlib import Path

def extract_from_pdfs(pdf_dir, output_dir):
    for pdf_path in Path(pdf_dir).glob("*.pdf"):
        with pdfplumber.open(pdf_path) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
            output_file = Path(output_dir) / f"{pdf_path.stem}.txt"
            output_file.write_text(text, encoding="utf-8")
```

### Extract Specific Pages

```python
with pdfplumber.open("file.pdf") as pdf:
    # Extract pages 5-10 (0-indexed)
    for page in pdf.pages[4:10]:
        text = page.extract_text()
```

## Financial Report Processing

For annual report PDFs (年报):

```python
import pdfplumber
import re

def extract_financial_data(pdf_path):
    """Extract financial data from annual reports"""
    with pdfplumber.open(pdf_path) as pdf:
        # Usually financial data is in the middle sections
        financial_pages = pdf.pages[20:100]  # Adjust based on report structure
        
        for page in financial_pages:
            text = page.extract_text()
            tables = page.extract_tables()
            
            # Look for key financial tables
            for table in tables:
                if table and len(table) > 0:
                    # Check if this is a financial table
                    header = " ".join(str(cell) for cell in table[0] if cell)
                    if any(keyword in header for keyword in ["营业收入", "净利润", "资产", "负债"]):
                        return table
    return None
```

## Best Practices

1. **Always use context manager** (`with` statement) to ensure proper file closure
2. **Handle encoding issues** - Chinese PDFs may need special handling
3. **Check for None** - `extract_text()` and `extract_tables()` can return None
4. **Use table settings** for better table extraction:

```python
tables = page.extract_tables({
    "vertical_strategy": "lines",
    "horizontal_strategy": "lines",
    "snap_tolerance": 3,
})
```

## Alternative Libraries

- **PyMuPDF (fitz)**: Faster for large PDFs, better for scanned documents
- **pypdf**: Lightweight, good for basic operations
- **pdf2image + pytesseract**: For OCR on scanned PDFs

## Additional Resources

- For advanced table extraction, see [reference.md](reference.md)
- For examples with financial reports, see [examples.md](examples.md)
