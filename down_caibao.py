import tushare as ts
import pandas as pd
import requests
import os
import re
from bs4 import BeautifulSoup
import time

ts.set_token(os.getenv('TUSHARE_TOKEN'))
pro = ts.pro_api(os.getenv('TUSHARE_TOKEN'))

# 新浪财经年报公告页面URL模板
BASE_URL_TEMPLATE = "https://money.finance.sina.com.cn/corp/go.php/vCB_Bulletin/stockid/{stock_code}/page_type/ndbg.phtml"


def get_sw_industry_stocks(industry_code: str = '330100.SI') -> pd.DataFrame:
    """
    获取申万行业分类的股票列表
    
    参数:
        industry_code: 申万行业代码，默认为330100.SI（白色家电）
    
    返回:
        包含股票信息的DataFrame
    """
    # 获取行业成分股
    df = pro.index_member_all(l3_code=industry_code)
    return df


def get_stock_basic_info(stock_codes: list) -> pd.DataFrame:
    """
    获取股票基本信息
    
    参数:
        stock_codes: 股票代码列表
    
    返回:
        包含股票基本信息的DataFrame
    """
    all_stocks = []
    for code in stock_codes:
        df = pro.daily_basic(ts_code=code, fields='ts_code,name,close,pe,pe_ttm,pb,ps,total_mv,circ_mv')
        if not df.empty:
            all_stocks.append(df)
    
    if all_stocks:
        return pd.concat(all_stocks, ignore_index=True)
    return pd.DataFrame()


def get_pdf_links_from_sina(stock_code: str, years: list = None) -> list:
    """
    从新浪财经获取年报PDF链接
    
    参数:
        stock_code: 股票代码（不带后缀，如000651）
        years: 需要下载的年份列表，默认为2019-2025
    
    返回:
        PDF链接列表，每个元素为(year, pdf_url, title)元组
    """
    if years is None:
        years = [2019, 2020, 2021, 2022, 2023, 2024, 2025]
    
    url = BASE_URL_TEMPLATE.format(stock_code=stock_code)
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    }
    
    pdf_links = []
    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.encoding = 'gb2312'
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 查找所有包含年报的链接
        # 新浪财经的年报页面结构：表格中包含年报公告链接
        links = soup.find_all('a', href=True)
        
        for link in links:
            href = link.get('href', '')
            title = link.get_text(strip=True)
            
            # 检查是否是年报PDF链接
            # 新浪财经的PDF链接通常包含 .pdf 或者指向年报公告
            if '.pdf' in href.lower() or '年报' in title:
                # 从标题中提取年份
                year_match = re.search(r'(20\d{2})', title)
                if year_match:
                    year = int(year_match.group(1))
                    if year in years:
                        # 如果是相对链接，需要补全
                        if href.startswith('/'):
                            href = 'https://money.finance.sina.com.cn' + href
                        elif not href.startswith('http'):
                            href = 'https://money.finance.sina.com.cn/' + href
                        pdf_links.append((year, href, title))
        
        # 如果直接没找到PDF链接，尝试查找公告详情页
        if not pdf_links:
            # 新浪财经可能使用iframe或其他结构
            tables = soup.find_all('table')
            for table in tables:
                rows = table.find_all('tr')
                for row in rows:
                    cols = row.find_all('td')
                    for col in cols:
                        links_in_col = col.find_all('a', href=True)
                        for a_link in links_in_col:
                            href = a_link.get('href', '')
                            title = a_link.get_text(strip=True)
                            if '年报' in title or '年度报告' in title:
                                year_match = re.search(r'(20\d{2})', title)
                                if year_match:
                                    year = int(year_match.group(1))
                                    if year in years:
                                        if href.startswith('/'):
                                            href = 'https://money.finance.sina.com.cn' + href
                                        elif not href.startswith('http'):
                                            href = 'https://money.finance.sina.com.cn/' + href
                                        # 尝试从详情页获取PDF链接
                                        pdf_url = get_pdf_from_detail_page(href, headers)
                                        if pdf_url:
                                            pdf_links.append((year, pdf_url, title))
    
    except Exception as e:
        print(f'获取 {stock_code} 年报链接失败: {e}')
    
    return pdf_links


def get_pdf_from_detail_page(detail_url: str, headers: dict) -> str:
    """
    从公告详情页获取PDF下载链接
    
    参数:
        detail_url: 公告详情页URL
        headers: HTTP请求头
    
    返回:
        PDF下载链接，如果找不到则返回空字符串
    """
    try:
        response = requests.get(detail_url, headers=headers, timeout=30)
        response.encoding = 'gb2312'
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 查找PDF下载链接
        links = soup.find_all('a', href=True)
        for link in links:
            href = link.get('href', '')
            if '.pdf' in href.lower():
                if href.startswith('/'):
                    href = 'https://money.finance.sina.com.cn' + href
                elif not href.startswith('http'):
                    href = 'https://money.finance.sina.com.cn/' + href
                return href
    
    except Exception as e:
        print(f'获取详情页PDF链接失败: {e}')
    
    return ''


def download_pdf(url: str, save_path: str, headers: dict) -> bool:
    """
    下载PDF文件
    
    参数:
        url: PDF下载链接
        save_path: 保存路径
        headers: HTTP请求头
    
    返回:
        是否下载成功
    """
    try:
        response = requests.get(url, headers=headers, timeout=60, stream=True)
        if response.status_code == 200:
            with open(save_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            return True
        else:
            print(f'下载失败，状态码: {response.status_code}')
    except Exception as e:
        print(f'下载PDF失败: {e}')
    
    return False


def download_annual_reports(stock_codes: list, stock_names: dict, years: list = None, save_dir: str = 'pdf'):
    """
    批量下载年报PDF
    
    参数:
        stock_codes: 股票代码列表（带后缀如000651.SZ）
        stock_names: 股票代码到名称的映射字典
        years: 需要下载的年份列表，默认为2019-2025
        save_dir: PDF保存目录
    """
    if years is None:
        years = [2019, 2020, 2021, 2022, 2023, 2024, 2025]
    
    # 创建保存目录
    os.makedirs(save_dir, exist_ok=True)
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    }
    
    for ts_code in stock_codes:
        # 剔除后缀 .SZ/.SH
        stock_code = ts_code.split('.')[0]
        stock_name = stock_names.get(ts_code, stock_code)
        
        print(f'\n处理 {stock_name} ({stock_code})...')
        
        # 获取年报PDF链接
        pdf_links = get_pdf_links_from_sina(stock_code, years)
        print(f'找到 {len(pdf_links)} 个年报链接')
        
        # 创建股票专属文件夹（临时目录结构：save_dir/stock_code）
        stock_dir = os.path.join(save_dir, stock_code)
        os.makedirs(stock_dir, exist_ok=True)
        
        # 下载PDF
        for year, pdf_url, title in pdf_links:
            # 清理文件名中的特殊字符
            safe_title = re.sub(r'[\\/:*?"<>|]', '_', title)
            filename = f'{stock_code}_{year}_{safe_title}.pdf'
            save_path = os.path.join(stock_dir, filename)
            
            # 检查是否已下载
            if os.path.exists(save_path):
                print(f'  [{year}] {title} - 已存在，跳过')
                continue
            
            print(f'  [{year}] 正在下载: {title}')
            if download_pdf(pdf_url, save_path, headers):
                print(f'  [{year}] 下载成功: {filename}')
            else:
                print(f'  [{year}] 下载失败')
            time.sleep(1)
        
        time.sleep(2)


def reorganize_baijiu_dir(result_df: pd.DataFrame):
    """
    重组白酒目录结构为 pdf/白酒/{股票名称}-{code}/*.pdf
    
    参数:
        result_df: 包含股票信息的DataFrame
    """
    import shutil
    
    pdf_dir = 'pdf/白酒'
    
    for _, row in result_df.iterrows():
        code = str(row['symbol'])
        name = str(row['name'])
        
        # 原目录: pdf/白酒/code/
        old_dir = os.path.join(pdf_dir, code)
        
        # 新目录: pdf/白酒/{股票名称}-{code}/
        new_dir = os.path.join(pdf_dir, f'{name}-{code}')
        
        if os.path.exists(old_dir) and old_dir != new_dir:
            # 移动目录
            if os.path.exists(new_dir):
                # 如果新目录已存在，合并文件
                for f in os.listdir(old_dir):
                    if f.endswith('.pdf'):
                        old_file = os.path.join(old_dir, f)
                        new_file = os.path.join(new_dir, f)
                        if not os.path.exists(new_file):
                            shutil.move(old_file, new_file)
                os.rmdir(old_dir)
            else:
                shutil.move(old_dir, new_dir)
            print(f'重组: {code} -> {name}-{code}')


def reorganize_bank_dir(result_df: pd.DataFrame):
    """
    重组银行目录结构为 pdf/银行/{股票名称}-{code}/*.pdf
    
    参数:
        result_df: 包含股票信息的DataFrame
    """
    import shutil
    
    pdf_dir = 'pdf/银行'
    
    for _, row in result_df.iterrows():
        code = str(row['symbol'])
        name = str(row['name'])
        
        # 原目录: pdf/银行/code/
        old_dir = os.path.join(pdf_dir, code)
        
        # 新目录: pdf/银行/{股票名称}-{code}/
        new_dir = os.path.join(pdf_dir, f'{name}-{code}')
        
        if os.path.exists(old_dir) and old_dir != new_dir:
            # 移动目录
            if os.path.exists(new_dir):
                # 如果新目录已存在，合并文件
                for f in os.listdir(old_dir):
                    if f.endswith('.pdf'):
                        old_file = os.path.join(old_dir, f)
                        new_file = os.path.join(new_dir, f)
                        if not os.path.exists(new_file):
                            shutil.move(old_file, new_file)
                os.rmdir(old_dir)
            else:
                shutil.move(old_dir, new_dir)
            print(f'重组: {code} -> {name}-{code}')


if __name__ == '__main__':
    # 获取银行(801780)行业的所有股票
    # 银行的申万指数代码是 801780.SI (根据申万2021分类)
    print('正在获取申万行业分类801780（银行）的股票信息...')
    
    # 获取银行指数成分股
    print('\n尝试获取银行指数(801780.SI)成分股...')
    try:
        stocks_df = pro.index_member(index_code='801780.SI')
        # 筛选当前成分股（is_new='Y'）
        current_stocks = stocks_df[stocks_df['is_new'] == 'Y']
        print(f'找到 {len(current_stocks)} 只当前银行成分股')
        
        if not current_stocks.empty:
            # 获取股票基本信息
            stock_codes = current_stocks['con_code'].tolist()
            print('\n获取股票详细信息...')
            stock_basic = pro.stock_basic(exchange='', list_status='L',
                                           fields='ts_code,symbol,name,area,industry,market,list_date')
            # 筛选银行成分股
            result_df = stock_basic[stock_basic['ts_code'].isin(stock_codes)]
            
            print('\n银行股票列表:')
            print(result_df.to_string())
            result_df.to_csv('bank_stocks.csv', index=False, encoding='utf-8-sig')
            print(f'\n数据已保存到 bank_stocks.csv，共 {len(result_df)} 只股票')
            
            # 下载年报PDF
            print('\n' + '='*50)
            print('开始下载2019-2025年年报PDF...')
            print('='*50)
            
            # 构建股票代码到名称的映射
            stock_names = dict(zip(result_df['ts_code'], result_df['name']))
            
            # 下载年报，保存到 pdf/银行 目录
            download_annual_reports(
                stock_codes=result_df['ts_code'].tolist(),
                stock_names=stock_names,
                years=[2019, 2020, 2021, 2022, 2023, 2024, 2025],
                save_dir='pdf/银行'
            )
            
            # 重组目录结构为 pdf/银行/{股票名称}-{code}/*.pdf
            print('\n重组目录结构...')
            reorganize_bank_dir(result_df)
            
            print('\n年报下载完成！')
    except Exception as e:
        print(f'获取成分股失败: {e}')