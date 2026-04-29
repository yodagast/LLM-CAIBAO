#!/usr/bin/env python
"""下载机场行业(851751.SI)公司2019-2025年年报"""
import tushare as ts
import pandas as pd
import requests
import os
import re
import time
from bs4 import BeautifulSoup

ts.set_token(os.getenv('TUSHARE_TOKEN'))
pro = ts.pro_api(os.getenv('TUSHARE_TOKEN'))  

BASE_URL_TEMPLATE = "https://money.finance.sina.com.cn/corp/go.php/vCB_Bulletin/stockid/{stock_code}/page_type/ndbg.phtml"

headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
}


def get_pdf_links_from_sina(stock_code: str, years: list = None, max_retries: int = 3) -> list:
    """从新浪财经获取年报PDF链接"""
    if years is None:
        years = [2019, 2020, 2021, 2022, 2023, 2024, 2025]
    
    url = BASE_URL_TEMPLATE.format(stock_code=stock_code)
    pdf_links = []
    
    for retry in range(max_retries):
        try:
            response = requests.get(url, headers=headers, timeout=60)
            response.encoding = 'gb2312'
            soup = BeautifulSoup(response.text, 'html.parser')
            
            links = soup.find_all('a', href=True)
            
            for link in links:
                href = link.get('href', '')
                title = link.get_text(strip=True)
                
                if '.pdf' in href.lower() or '年报' in title or '年度报告' in title:
                    year_match = re.search(r'(20\d{2})', title)
                    if year_match:
                        year = int(year_match.group(1))
                        if year in years:
                            if href.startswith('/'):
                                href = 'https://money.finance.sina.com.cn' + href
                            elif not href.startswith('http'):
                                href = 'https://money.finance.sina.com.cn/' + href
                            pdf_links.append((year, href, title))
            
            if pdf_links:
                return pdf_links
                
        except Exception as e:
            print(f'  第{retry+1}次尝试失败: {e}')
            if retry < max_retries - 1:
                time.sleep(5)
    
    return pdf_links


def download_pdf(url: str, save_path: str) -> bool:
    """下载PDF文件"""
    try:
        response = requests.get(url, headers=headers, timeout=120, stream=True)
        if response.status_code == 200:
            with open(save_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            return True
    except Exception as e:
        print(f'  下载失败: {e}')
    return False


def main():
    print('='*50)
    print('下载机场行业公司年报')
    print('='*50)
    
    # 从CSV读取机场股票列表
    try:
        result_df = pd.read_csv('airport_stocks.csv')
        print(f'\n读取 airport_stocks.csv，共 {len(result_df)} 只机场股票')
        print('\n机场股票列表:')
        print(result_df[['ts_code', 'symbol', 'name', 'area']].to_string())
    except Exception as e:
        print(f'读取CSV失败: {e}')
        # 如果CSV不存在，直接获取机场股票
        stock_basic = pro.stock_basic(exchange='', list_status='L',
                                       fields='ts_code,symbol,name,area,industry,market,list_date')
        result_df = stock_basic[stock_basic['industry'].str.contains('机场', na=False)]
        print(f'找到 {len(result_df)} 只机场股票')
        result_df.to_csv('airport_stocks.csv', index=False, encoding='utf-8-sig')
    
    # 下载年报
    print('\n' + '='*50)
    print('开始下载2019-2025年年报PDF...')
    print('='*50)
    
    pdf_dir = 'pdf/机场'
    os.makedirs(pdf_dir, exist_ok=True)
    years = [2019, 2020, 2021, 2022, 2023, 2024, 2025]
    
    total_downloaded = 0
    
    for _, row in result_df.iterrows():
        code = str(row['symbol'])
        name = str(row['name'])
        
        # 目标目录：名称-代码格式
        target_dir = os.path.join(pdf_dir, f'{name}-{code}')
        os.makedirs(target_dir, exist_ok=True)
        
        print(f'\n处理 {name} ({code})...')
        
        # 获取年报链接
        pdf_links = get_pdf_links_from_sina(code, years, max_retries=3)
        print(f'找到 {len(pdf_links)} 个年报链接')
        
        if not pdf_links:
            continue
        
        # 下载PDF
        downloaded = 0
        for year, pdf_url, title in pdf_links:
            safe_title = re.sub(r'[\\/:*?"<>|]', '_', title)
            filename = f'{code}_{year}_{safe_title}.pdf'
            save_path = os.path.join(target_dir, filename)
            
            if os.path.exists(save_path):
                print(f'  [{year}] {title} - 已存在，跳过')
                continue
            
            print(f'  [{year}] 正在下载: {title}')
            if download_pdf(pdf_url, save_path):
                print(f'  [{year}] 下载成功')
                downloaded += 1
            else:
                print(f'  [{year}] 下载失败')
            
            time.sleep(1)
        
        total_downloaded += downloaded
        time.sleep(2)
    
    # 统计结果
    print('\n' + '='*50)
    print('下载统计')
    print('='*50)
    total = 0
    for d in sorted(os.listdir(pdf_dir)):
        subdir = os.path.join(pdf_dir, d)
        if os.path.isdir(subdir):
            count = len([f for f in os.listdir(subdir) if f.endswith('.pdf')])
            print(f'{d}: {count}份')
            total += count
    print(f'\n总计: {total}份年报PDF')
    print(f'本次下载: {total_downloaded}份')


if __name__ == '__main__':
    main()