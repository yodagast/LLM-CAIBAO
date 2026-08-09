import finshare as fs

# 港股实时快照（东方财富数据源）
snapshot = fs.get_snapshot_data('00700.HK')  # 腾讯控股
print(f"最新价: {snapshot.last_price}")
print(f"涨跌额: {snapshot.change}")
print(f"涨跌幅: {snapshot.change_pct}%")

# 港股历史K线（东方财富数据源）
df = fs.get_historical_data('00700.HK', start='2024-01-01', end='2024-12-31')
print(df.head())

# 批量获取港股行情
hk_stocks = ['00700.HK', '09988.HK', '9988.HK']
snapshots = fs.get_batch_snapshots(hk_stocks)