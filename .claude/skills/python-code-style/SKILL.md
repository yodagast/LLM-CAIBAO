---
name: python-code-style
description: Enforce Python coding standards and best practices for financial data analysis projects. Use when writing Python code, reviewing code, or when the user asks for code style improvements.
---

# Python Code Style Guide

## General Principles

- Follow PEP 8 style guide
- Use type hints for function signatures
- Write docstrings for all public functions and classes
- Keep functions focused and under 50 lines when possible
- Use meaningful variable names (avoid single-letter except in loops)

## Naming Conventions

```python
# Variables and functions: snake_case
def calculate_revenue_growth(revenue_current, revenue_previous):
    pass

# Constants: UPPER_SNAKE_CASE
MAX_RETRY_ATTEMPTS = 3
DEFAULT_TIMEOUT = 30

# Classes: PascalCase
class FinancialDataProcessor:
    pass

# Private attributes: leading underscore
_private_cache = {}

# Module names: short, lowercase
# Good: data_utils.py, pdf_parser.py
# Bad: DataUtils.py, PDF_Parser.py
```

## Financial Data Handling

### DataFrame Operations

```python
import pandas as pd

def load_stock_data(filepath: str) -> pd.DataFrame:
    """Load stock data with proper dtypes."""
    # Always specify dtypes for ID columns
    df = pd.read_csv(
        filepath,
        dtype={
            "stock_code": str,  # Prevent leading zero loss
            "industry_code": str,
        },
        parse_dates=["date"],
    )
    return df


def process_financial_data(df: pd.DataFrame) -> pd.DataFrame:
    """Process financial data with proper handling."""
    # Create copy to avoid modifying original
    df = df.copy()
    
    # Handle missing values explicitly
    df = df.fillna(0)
    
    # Use vectorized operations
    df["growth_rate"] = df["revenue"].pct_change() * 100
    
    return df
```

### Numeric Precision

```python
# Financial calculations: use Decimal for precise arithmetic
from decimal import Decimal, ROUND_HALF_UP

def calculate_net_profit_margin(net_profit: float, revenue: float) -> float:
    """Calculate net profit margin with proper rounding."""
    if revenue == 0:
        return 0.0
    
    margin = (net_profit / revenue) * 100
    
    # Round to 2 decimal places
    return round(margin, 2)
```

## Error Handling

```python
from typing import Optional
import logging

logger = logging.getLogger(__name__)

def fetch_financial_report(stock_code: str, year: int) -> Optional[dict]:
    """Fetch financial report with proper error handling."""
    try:
        if not stock_code or len(stock_code) != 6:
            raise ValueError(f"Invalid stock code: {stock_code}")
        
        if year < 1990 or year > 2100:
            raise ValueError(f"Invalid year: {year}")
        
        # Fetch data...
        data = _fetch_from_api(stock_code, year)
        return data
        
    except ValueError as e:
        logger.warning(f"Validation error: {e}")
        return None
    except ConnectionError as e:
        logger.error(f"Network error fetching {stock_code}: {e}")
        return None
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        return None
```

## File Operations

```python
from pathlib import Path
from typing import Union

def save_dataframe(df: pd.DataFrame, output_path: Union[str, Path]) -> None:
    """Save DataFrame with proper encoding and formatting."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Use UTF-8 BOM for Excel compatibility
    df.to_csv(output_path, index=False, encoding="utf-8-sig")
    
    logger.info(f"Saved data to {output_path}")


def read_pdf_report(pdf_path: Union[str, Path]) -> str:
    """Read PDF report with proper resource management."""
    pdf_path = Path(pdf_path)
    
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")
    
    # Always use context managers
    import pdfplumber
    
    with pdfplumber.open(pdf_path) as pdf:
        text = "\n".join(
            page.extract_text() or "" 
            for page in pdf.pages
        )
    
    return text
```

## Documentation

```python
def calculate_cagr(
    start_value: float,
    end_value: float,
    years: int,
) -> float:
    """
    Calculate Compound Annual Growth Rate (CAGR).
    
    Args:
        start_value: Initial value
        end_value: Final value
        years: Number of years
        
    Returns:
        CAGR as a percentage
        
    Raises:
        ValueError: If years <= 0 or start_value <= 0
        
    Example:
        >>> calculate_cagr(100, 200, 5)
        14.87
    """
    if years <= 0:
        raise ValueError("Years must be positive")
    if start_value <= 0:
        raise ValueError("Start value must be positive")
    
    cagr = (end_value / start_value) ** (1 / years) - 1
    return round(cagr * 100, 2)
```

## Project-Specific Rules

### Stock Code Handling

```python
# Always treat stock codes as strings
def normalize_stock_code(code: str) -> str:
    """Normalize stock code to 6-digit string."""
    code = str(code).strip()
    
    # Remove exchange suffixes
    code = code.split(".")[0]
    
    # Pad with leading zeros
    code = code.zfill(6)
    
    return code
```

### Currency Handling

```python
# Convert between units
def to_yuan(amount_in_ten_thousand: float) -> float:
    """Convert 万元 to 元."""
    return amount_in_ten_thousand * 10_000


def to_billion(amount_in_yuan: float) -> float:
    """Convert 元 to 亿元."""
    return amount_in_yuan / 100_000_000
```

## Code Organization

```python
# Standard import order
# 1. Standard library
import os
import json
from pathlib import Path
from typing import Optional, Dict, List

# 2. Third-party packages
import pandas as pd
import numpy as np
import pdfplumber

# 3. Local modules
from .config import settings
from .utils import logger


# Module-level constants
DEFAULT_ENCODING = "utf-8"
FINANCIAL_YEAR_START = 1


class DataProcessor:
    """Process financial data with standardized methods."""
    
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self._cache: Dict[str, pd.DataFrame] = {}
    
    def load_data(self, filename: str) -> pd.DataFrame:
        """Load data from file."""
        if filename in self._cache:
            return self._cache[filename]
        
        filepath = self.data_dir / filename
        df = pd.read_csv(filepath)
        self._cache[filename] = df
        
        return df
```

## Testing

```python
import pytest


def test_calculate_cagr():
    """Test CAGR calculation."""
    assert calculate_cagr(100, 200, 5) == 14.87
    
    with pytest.raises(ValueError):
        calculate_cagr(100, 200, 0)


def test_normalize_stock_code():
    """Test stock code normalization."""
    assert normalize_stock_code("000858.SZ") == "000858"
    assert normalize_stock_code("858") == "000858"
    assert normalize_stock_code(858) == "000858"
```

## Linting Configuration

Use `flake8` with project-specific settings:

```ini
# .flake8
[flake8]
max-line-length = 88
extend-ignore = E203, W503
exclude = 
    .git,
    __pycache__,
    venv,
    .venv
per-file-ignores =
    __init__.py:F401
```

Use `black` for formatting:

```toml
# pyproject.toml
[tool.black]
line-length = 88
target-version = ['py39']
include = '\.pyi?$'
```
