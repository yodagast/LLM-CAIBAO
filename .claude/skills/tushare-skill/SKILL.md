---
name: tushare-skill
description: 使用tushare获取中国股市金融数据，包括股票基本信息、申万行业成分股、财务数据等。当用户需要获取股票列表、行业分类、财务报表数据时使用此技能。
---

# Tushare 金融数据获取

## 快速开始

### 安装与配置

```bash
pip install tushare
```

### 初始化

```python
import tushare as ts

# 设置token（从环境变量获取更安全）
ts.set_token(os.getenv('TUSHARE_TOKEN'))
pro = ts.pro_api(os.getenv('TUSHARE_TOKEN'))
```

**获取Token**: 注册 https://tushare.pro 获取API token

---

## 常用API

### 获取股票基本信息

```python
# 获取全部上市公司基本信息
stock_basic = pro.stock_basic(
    exchange='', 
    list_status='L',
    fields='ts_code,symbol,name,area,industry,market,list_date'
)

# 按行业筛选
coal_stocks = stock_basic[stock_basic['industry'].str.contains('煤炭', na=False)]
```

### 获取申万行业成分股

```python
# 获取行业指数成分股
# 银行: 801780.SI
# 煤炭: 801950.SI
# 白色家电: 330100.SI（使用index_member_all）

stocks_df = pro.index_member(index_code='801780.SI')
current_stocks = stocks_df[stocks_df['is_new'] == 'Y']
stock_codes = current_stocks['con_code'].tolist()
```

**注意**: 部分行业代码可能返回空结果，此时需改用industry字段筛选：

```python
# 备选方案：通过行业名称筛选
stock_basic = pro.stock_basic(exchange='', list_status='L')
industry_stocks = stock_basic[stock_basic['industry'].str.contains('机场', na=False)]
```

### 获取日线数据

```python
# 获取单个股票日线
daily = pro.daily(ts_code='000001.SZ', start_date='20250101', end_date='20250430')

# 获取多个股票
daily = pro.daily(ts_code='000001.SZ,600000.SH', start_date='20250101')
```

### 获取财务数据

```python
# 获取利润表
income = pro.income(ts_code='000001.SZ', start_date='20240101')

# 获取资产负债表
balancesheet = pro.balancesheet(ts_code='000001.SZ', start_date='20240101')

# 获取现金流量表
cashflow = pro.cashflow(ts_code='000001.SZ', start_date='20240101')
```

---

## 申万行业代码参考

| 行业 | 代码 | 说明 |
|-----|------|------|
| 银行 | 801780.SI | 正常返回成分股 |
| 煤炭 | 801950.SI | 正常返回成分股 |
| 白色家电 | 330100.SI | 使用index_member_all |
| 机场 | 851751.SI | 可能返回空，改用industry筛选 |

---

## 常见问题处理

### 1. Token验证失败

```
错误: 您的token不对，请确认。
解决: 检查token是否正确设置，建议使用环境变量
```

### 2. 行业成分股返回空

```
问题: pro.index_member(index_code='851751.SI') 返回0条记录
解决: 改用 industry 字段筛选
```

### 3. 接口调用限制

```
问题: 频率限制导致请求失败
解决: 添加适当延时，批量请求分批处理
```

---

## 最佳实践

1. **Token安全**: 使用环境变量而非硬编码
2. **数据缓存**: 将获取的数据保存到CSV避免重复请求
3. **批量处理**: 分批获取数据，避免触发频率限制
4. **异常处理**: 添加try-except处理网络异常
5. **备选方案**: index_member失败时改用industry字段筛选

---

## 项目中的应用示例

```python
# 获取行业股票列表并保存
stocks_df = pro.index_member(index_code='801780.SI')
current_stocks = stocks_df[stocks_df['is_new'] == 'Y']
stock_codes = current_stocks['con_code'].tolist()

# 获取详细信息
stock_basic = pro.stock_basic(exchange='', list_status='L')
result_df = stock_basic[stock_basic['ts_code'].isin(stock_codes)]
result_df.to_csv('bank_stocks.csv', index=False, encoding='utf-8-sig')
```