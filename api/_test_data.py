"""临时测试脚本: 验证 data_service 与 backtest_engine。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api import data_service as ds
from api import backtest_engine as be

# 1) 解析代码
for code in ["000858", "000858.SZ", "600036.SH", "513050.SH", "510300"]:
    try:
        print("resolve:", code, "->", ds.resolve_code(code))
    except Exception as e:
        print("resolve FAIL:", code, "->", repr(e))

# 2) fund_basic 列检查
funds = ds._fund_basic()
print("\nfund_basic cols:", list(funds.columns))

# 3) 搜索
print("\nsearch 五粮:", ds.search_stock("五粮", 5))

# 4) 拉取五粮液日线并跑回测
df = ds.get_daily("000858.SZ", "stock", "20170101", "20260730")
print("\ndaily rows:", len(df), "first:", df['trade_date'].iloc[0], "last:", df['trade_date'].iloc[-1])
res = be.run_backtest(df, 100000, buy_price=75, sell_price=150, stop_loss=60, lookback_days=20)
for s in res:
    print(f"\n[{s['name']}] points={len(s['dates'])} metrics={s['metrics']}")
