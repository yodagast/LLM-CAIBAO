---
name: data-analysis
description: Analyze data using pandas, create visualizations with matplotlib/seaborn, perform statistical analysis. Use when analyzing datasets, creating charts, or working with tabular data.
---

# Data Analysis

## Quick Start

Use `pandas` for data manipulation and `matplotlib`/`seaborn` for visualization.

### Installation

```bash
pip install pandas matplotlib seaborn openpyxl
```

### Basic Data Loading

```python
import pandas as pd

# CSV
df = pd.read_csv("data.csv")

# Excel
df = pd.read_excel("data.xlsx")

# With specific dtypes (important for stock codes)
df = pd.read_csv("data.csv", dtype={"stock_code": str})
```

## Common Operations

### Data Inspection

```python
# Basic info
df.head()
df.info()
df.describe()

# Check for missing values
df.isnull().sum()

# Unique values
df["column"].unique()
```

### Data Cleaning

```python
# Remove duplicates
df = df.drop_duplicates()

# Handle missing values
df = df.dropna()  # Remove rows with NaN
df = df.fillna(0)  # Fill NaN with 0

# Convert data types
df["date"] = pd.to_datetime(df["date"])
df["value"] = pd.to_numeric(df["value"], errors="coerce")
```

### Filtering and Selection

```python
# Filter rows
df_filtered = df[df["value"] > 100]
df_filtered = df.query("value > 100 and category == 'A'")

# Select columns
df_subset = df[["col1", "col2", "col3"]]
```

### Grouping and Aggregation

```python
# Group by and aggregate
df_grouped = df.groupby("category").agg({
    "value": ["mean", "sum", "count"],
    "amount": "sum"
})

# Pivot tables
pivot = df.pivot_table(
    values="amount",
    index="category",
    columns="year",
    aggfunc="sum"
)
```

## Visualization

### Basic Plots

```python
import matplotlib.pyplot as plt
import seaborn as sns

# Line plot
plt.figure(figsize=(10, 6))
plt.plot(df["date"], df["value"])
plt.title("Value Over Time")
plt.xlabel("Date")
plt.ylabel("Value")
plt.show()

# Bar plot
sns.barplot(data=df, x="category", y="value")
plt.show()

# Scatter plot
sns.scatterplot(data=df, x="x_col", y="y_col", hue="category")
plt.show()
```

### Financial Data Visualization

```python
# Time series with multiple lines
plt.figure(figsize=(12, 6))
for stock in df["stock_code"].unique():
    stock_data = df[df["stock_code"] == stock]
    plt.plot(stock_data["date"], stock_data["price"], label=stock)
plt.legend()
plt.title("Stock Prices Over Time")
plt.show()

# Correlation heatmap
corr_matrix = df[["col1", "col2", "col3"]].corr()
sns.heatmap(corr_matrix, annot=True, cmap="coolwarm")
plt.show()
```

## Financial Analysis

### Growth Rate Calculation

```python
# Year-over-year growth
df_sorted = df.sort_values(["stock_code", "year"])
df_sorted["revenue_growth"] = df_sorted.groupby("stock_code")["revenue"].pct_change() * 100

# Compound Annual Growth Rate (CAGR)
def calculate_cagr(start_value, end_value, years):
    return (end_value / start_value) ** (1 / years) - 1
```

### Ratio Analysis

```python
# Financial ratios
df["gross_margin"] = (df["revenue"] - df["cogs"]) / df["revenue"] * 100
df["net_margin"] = df["net_income"] / df["revenue"] * 100
df["roe"] = df["net_income"] / df["equity"] * 100
```

## Export Results

```python
# To CSV
df.to_csv("output.csv", index=False, encoding="utf-8-sig")

# To Excel
with pd.ExcelWriter("output.xlsx") as writer:
    df.to_excel(writer, sheet_name="Data", index=False)
    summary.to_excel(writer, sheet_name="Summary", index=False)
```

## Best Practices

1. **Always specify dtypes when loading** - especially for ID columns like stock codes
2. **Use vectorized operations** - avoid loops, use pandas built-in methods
3. **Handle dates properly** - convert to datetime for time series analysis
4. **Check data quality** - inspect for outliers and anomalies before analysis
5. **Document your analysis** - add comments explaining key transformations

## Additional Resources

- For advanced statistical analysis, see [reference.md](reference.md)
- For example notebooks, see [examples.md](examples.md)
