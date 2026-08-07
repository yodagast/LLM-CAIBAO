from tickflow import TickFlow

# 使用免费服务（无需 API key）
tf = TickFlow.free()

# 查询日K线数据
df = tf.klines.get("00700.HK", period="1d", count=100, as_dataframe=True)
print(df.tail())

# 查询标的信息
instruments = tf.instruments.batch(symbols=["600000.SH", "000001.SZ","0700.HK"])
print(instruments)
