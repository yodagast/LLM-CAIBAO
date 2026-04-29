#!/usr/bin/env python3
"""
提取五粮液2020-2024年资产负债表数据
"""
import pdfplumber
import pandas as pd
from pathlib import Path
import re

# 五粮液PDF文件路径
PDF_DIR = Path("/Users/huangyong/git/llm-caibao/pdf/白酒/五粮液-000858")

# 年份和对应的PDF文件
YEARS_FILES = {
    2020: "000858_2020_五粮液：2020年年度报告.pdf",
    2021: "000858_2021_五粮液：2021年年度报告.pdf",
    2022: "000858_2022_五粮液：2022年年度报告.pdf",
    2023: "000858_2023_五粮液：2023年年度报告.pdf",
    2024: "000858_2024_五粮液：2024年年度报告.pdf",
}


def find_balance_sheet_pages(pdf_path):
    """查找资产负债表的页码范围"""
    with pdfplumber.open(pdf_path) as pdf:
        balance_sheet_start = None
        balance_sheet_end = None
        
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            
            # 查找资产负债表标题 - 更精确的匹配
            if ("合并资产负债表" in text or "母公司资产负债表" in text) and balance_sheet_start is None:
                balance_sheet_start = i
                print(f"  找到资产负债表起始页: {i+1}")
            
            # 查找资产负债表结束标记
            if balance_sheet_start is not None and i > balance_sheet_start:
                # 如果遇到其他财务报表，说明资产负债表结束
                if any(marker in text for marker in ["合并利润表", "母公司利润表", "合并现金流量表", "母公司现金流量表"]):
                    balance_sheet_end = i
                    print(f"  找到资产负债表结束页: {i}")
                    break
        
        # 如果没有找到结束页，设置一个合理的范围
        if balance_sheet_start is not None and balance_sheet_end is None:
            balance_sheet_end = min(balance_sheet_start + 5, len(pdf.pages))
            print(f"  设置默认结束页: {balance_sheet_end}")
            
        return balance_sheet_start, balance_sheet_end


def extract_tables_from_pages(pdf_path, start_page, end_page):
    """从指定页面提取表格，使用更好的设置"""
    tables = []
    table_settings = {
        "vertical_strategy": "lines",
        "horizontal_strategy": "lines",
        "snap_tolerance": 3,
        "join_tolerance": 3,
        "edge_min_length": 10,
        "min_words_vertical": 3,
        "min_words_horizontal": 1,
    }
    
    with pdfplumber.open(pdf_path) as pdf:
        for i in range(start_page, min(end_page, len(pdf.pages))):
            page = pdf.pages[i]
            # 尝试不同的表格提取设置
            page_tables = page.extract_tables(table_settings)
            if not page_tables:
                # 如果没有找到表格，尝试更宽松的设置
                page_tables = page.extract_tables()
            for table in page_tables:
                if table and len(table) > 0:
                    tables.append(table)
    return tables


def is_balance_sheet_item(item_name):
    """判断是否是资产负债表项目"""
    if not item_name:
        return False
    
    # 排除非财务项目的标题和说明
    exclude_patterns = [
        "编制单位", "单位：", "元", "人民币", "附注", "注释",
        "流动资产", "非流动资产", "流动负债", "非流动负债",
        "所有者权益", "股东权益", "资产总计", "负债总计",
        "负债和所有者权益总计", "负债和股东权益总计",
        "中国人寿", "中国工商银行", "景顺长城", "招商中证",
        "易方达", "中央汇金", "国泰君安", "宜宾发展",
        "香港中央结算", "中国证券金融", "中国银行"
    ]
    
    for pattern in exclude_patterns:
        if pattern in item_name:
            return False
    
    # 包含常见的资产负债表项目关键词
    asset_keywords = ["货币资金", "应收", "预付", "存货", "流动资产", "非流动资产",
                      "固定资产", "无形", "长期", "投资", "资产"]
    liability_keywords = ["应付", "预收", "合同负债", "应付职工", "应交税费",
                          "流动负债", "非流动负债", "长期借款", "负债"]
    equity_keywords = ["股本", "资本公积", "盈余公积", "未分配利润", "所有者权益",
                       "股东权益", "库存股", "其他综合收益"]
    
    all_keywords = asset_keywords + liability_keywords + equity_keywords
    
    return any(keyword in item_name for keyword in all_keywords)


def parse_balance_sheet(tables):
    """解析资产负债表表格数据"""
    balance_sheet_data = {}
    
    for table in tables:
        if not table or len(table) < 2:
            continue
            
        # 检查是否是资产负债表
        header = " ".join(str(cell) or "" for cell in table[0])
        
        print(f"  处理表格，行数: {len(table)}, 表头: {header[:50]}...")
        
        # 解析每一行
        for row in table[1:]:  # 跳过表头
            if not row or len(row) < 2:
                continue
                
            item_name = str(row[0] or "").strip().replace("\n", "").replace(" ", "")
            
            # 过滤非资产负债表项目
            if not is_balance_sheet_item(item_name):
                continue
            
            # 提取期末余额和期初余额（可能在不同列）
            try:
                # 尝试找到数值列
                values = []
                for cell in row[1:]:
                    if cell:
                        val_str = str(cell).replace(",", "").replace(" ", "").strip()
                        if val_str and val_str != "-":
                            try:
                                # 处理括号表示的负数
                                if val_str.startswith("(") and val_str.endswith(")"):
                                    val_str = "-" + val_str[1:-1]
                                val = float(val_str)
                                values.append(val)
                            except:
                                pass
                
                if len(values) >= 2:
                    balance_sheet_data[item_name] = {
                        "期末余额": values[0],
                        "期初余额": values[1]
                    }
                elif len(values) == 1:
                    balance_sheet_data[item_name] = {
                        "期末余额": values[0],
                        "期初余额": None
                    }
                    
            except Exception as e:
                continue
    
    return balance_sheet_data


def extract_balance_sheet_for_year(year, filename):
    """提取指定年份的资产负债表"""
    pdf_path = PDF_DIR / filename
    print(f"\n处理 {year} 年财报: {filename}")
    
    if not pdf_path.exists():
        print(f"  文件不存在: {pdf_path}")
        return None
    
    try:
        # 查找资产负债表页面
        start_page, end_page = find_balance_sheet_pages(pdf_path)
        
        if start_page is None:
            print(f"  未找到资产负债表")
            return None
        
        print(f"  页面范围: {start_page+1} - {end_page}")
        
        # 提取表格
        tables = extract_tables_from_pages(pdf_path, start_page, end_page)
        print(f"  提取到 {len(tables)} 个表格")
        
        # 解析数据
        data = parse_balance_sheet(tables)
        print(f"  解析到 {len(data)} 个项目")
        
        return data
        
    except Exception as e:
        print(f"  处理出错: {e}")
        import traceback
        traceback.print_exc()
        return None


def main():
    """主函数"""
    all_years_data = {}
    
    for year, filename in YEARS_FILES.items():
        data = extract_balance_sheet_for_year(year, filename)
        if data:
            all_years_data[year] = data
    
    # 整理数据为DataFrame
    if all_years_data:
        # 获取所有项目
        all_items = set()
        for year_data in all_years_data.values():
            all_items.update(year_data.keys())
        
        # 创建数据字典
        result_data = {"项目": sorted(all_items)}
        
        for year in sorted(all_years_data.keys()):
            year_data = all_years_data[year]
            result_data[f"{year}_期末"] = [year_data.get(item, {}).get("期末余额") for item in result_data["项目"]]
            result_data[f"{year}_期初"] = [year_data.get(item, {}).get("期初余额") for item in result_data["项目"]]
        
        df = pd.DataFrame(result_data)
        
        # 保存到CSV
        output_file = Path("/Users/huangyong/git/llm-caibao/五粮液资产负债表_2020-2024.csv")
        df.to_csv(output_file, index=False, encoding="utf-8-sig")
        print(f"\n✅ 数据已保存到: {output_file}")
        
        # 显示前几行
        print("\n数据预览:")
        print(df.head(20).to_string())
        
        return df
    else:
        print("\n❌ 未提取到任何数据")
        return None


if __name__ == "__main__":
    main()
