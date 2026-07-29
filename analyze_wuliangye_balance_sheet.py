#!/usr/bin/env python3
"""
分析五粮液2020-2024年资产负债表变化情况
"""
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
from pathlib import Path
import numpy as np

# 设置中文字体
import matplotlib.font_manager as fm
_candidate_fonts = [
    "PingFang HK", "PingFang SC", "Heiti TC", "Heiti SC",
    "Songti SC", "Hiragino Sans GB", "Hiragino Sans",
    "Source Han Sans CN", "Apple SD Gothic Neo",
    "WenQuanYi Micro Hei", "Noto Sans CJK SC", "SimHei",
]
_chosen = None
for _fn in _candidate_fonts:
    try:
        _fp = fm.findfont(_fn, fallback_to_default=False)
        if "DejaVuSans" in _fp:
            continue
        _chosen = _fn
        break
    except Exception:
        continue
if _chosen is None:
    _chosen = "DejaVu Sans"
plt.rcParams['font.sans-serif'] = [_chosen]
plt.rcParams['axes.unicode_minus'] = False

# 读取数据
df = pd.read_csv("/Users/huangyong/git/llm-caibao/五粮液资产负债表_2020-2024.csv")

print("=" * 80)
print("五粮液（000858）2020-2022年资产负债表分析报告")
print("=" * 80)

# 1. 资产结构分析
print("\n【一、资产结构分析】\n")

# 主要资产项目
asset_items = {
    "货币资金": "货币资金",
    "应收票据": "应收票据",
    "应收款项融资": "应收款项融资",
    "应收账款": "应收账款",
    "存货": "存货",
    "其他应收款": "其他应收款",
    "固定资产": "固定资产",
    "无形资产": "无形资产",
    "长期股权投资": "长期股权投资",
}

print("主要资产项目变化（单位：亿元）：")
print("-" * 80)

asset_data = []
for item_name, col_name in asset_items.items():
    row = df[df['项目'] == col_name]
    if not row.empty:
        values = []
        for year in [2020, 2021, 2022]:
            val = row[f'{year}_期末'].values[0]
            values.append(val / 1e8 if pd.notna(val) else 0)  # 转换为亿元
        
        asset_data.append({
            '项目': item_name,
            '2020': values[0],
            '2021': values[1],
            '2022': values[2],
            '增长额(20-22)': values[2] - values[0],
            '增长率(%)': ((values[2] / values[0] - 1) * 100) if values[0] > 0 else 0
        })

asset_df = pd.DataFrame(asset_data)
print(asset_df.to_string(index=False))

# 2. 负债结构分析
print("\n\n【二、负债结构分析】\n")

liability_items = {
    "应付账款": "应付账款",
    "应付票据": "应付票据",
    "合同负债": "合同负债",
    "应付职工薪酬": "应付职工薪酬",
    "应交税费": "应交税费",
    "其他应付款": "其他应付款",
    "负债合计": "负债合计",
}

print("主要负债项目变化（单位：亿元）：")
print("-" * 80)

liability_data = []
for item_name, col_name in liability_items.items():
    row = df[df['项目'] == col_name]
    if not row.empty:
        values = []
        for year in [2020, 2021, 2022]:
            val = row[f'{year}_期末'].values[0]
            values.append(val / 1e8 if pd.notna(val) else 0)
        
        liability_data.append({
            '项目': item_name,
            '2020': values[0],
            '2021': values[1],
            '2022': values[2],
            '增长额(20-22)': values[2] - values[0],
            '增长率(%)': ((values[2] / values[0] - 1) * 100) if values[0] > 0 else 0
        })

liability_df = pd.DataFrame(liability_data)
print(liability_df.to_string(index=False))

# 3. 所有者权益分析
print("\n\n【三、所有者权益分析】\n")

equity_items = {
    "股本": "股本",
    "资本公积": "资本公积",
    "盈余公积": "盈余公积",
    "未分配利润": "未分配利润",
}

print("所有者权益项目变化（单位：亿元）：")
print("-" * 80)

equity_data = []
for item_name, col_name in equity_items.items():
    row = df[df['项目'] == col_name]
    if not row.empty:
        values = []
        for year in [2020, 2021, 2022]:
            val = row[f'{year}_期末'].values[0]
            values.append(val / 1e8 if pd.notna(val) else 0)
        
        equity_data.append({
            '项目': item_name,
            '2020': values[0],
            '2021': values[1],
            '2022': values[2],
            '增长额(20-22)': values[2] - values[0],
            '增长率(%)': ((values[2] / values[0] - 1) * 100) if values[0] > 0 else 0
        })

equity_df = pd.DataFrame(equity_data)
print(equity_df.to_string(index=False))

# 4. 关键财务指标计算
print("\n\n【四、关键财务指标分析】\n")

# 计算关键指标
metrics_data = []

for year in [2020, 2021, 2022]:
    # 获取数据
    货币资金 = df[df['项目'] == '货币资金'][f'{year}_期末'].values[0] / 1e8
    存货 = df[df['项目'] == '存货'][f'{year}_期末'].values[0] / 1e8
    固定资产 = df[df['项目'] == '固定资产'][f'{year}_期末'].values[0] / 1e8
    负债合计 = df[df['项目'] == '负债合计'][f'{year}_期末'].values[0] / 1e8
    未分配利润 = df[df['项目'] == '未分配利润'][f'{year}_期末'].values[0] / 1e8
    
    # 计算资产总计（从负债合计 + 所有者权益估算）
    股本 = df[df['项目'] == '股本'][f'{year}_期末'].values[0] / 1e8
    资本公积 = df[df['项目'] == '资本公积'][f'{year}_期末'].values[0] / 1e8
    盈余公积 = df[df['项目'] == '盈余公积'][f'{year}_期末'].values[0] / 1e8
    
    所有者权益合计 = 股本 + 资本公积 + 盈余公积 + 未分配利润
    资产总计 = 负债合计 + 所有者权益合计
    
    metrics_data.append({
        '年份': year,
        '资产总计(亿元)': round(资产总计, 2),
        '负债合计(亿元)': round(负债合计, 2),
        '所有者权益(亿元)': round(所有者权益合计, 2),
        '资产负债率(%)': round(负债合计 / 资产总计 * 100, 2),
        '流动资产占比(%)': round((货币资金 + 存货) / 资产总计 * 100, 2),
        '货币资金占比(%)': round(货币资金 / 资产总计 * 100, 2),
    })

metrics_df = pd.DataFrame(metrics_data)
print(metrics_df.to_string(index=False))

# 5. 可视化分析
print("\n\n【五、可视化分析】")
print("正在生成图表...")

fig, axes = plt.subplots(2, 2, figsize=(14, 10))
fig.suptitle('五粮液（000858）资产负债表分析（2020-2022）', fontsize=16)

# 图1：资产结构变化
ax1 = axes[0, 0]
years = [2020, 2021, 2022]
货币资金_data = asset_df[asset_df['项目'] == '货币资金'][['2020', '2021', '2022']].values[0]
存货_data = asset_df[asset_df['项目'] == '存货'][['2020', '2021', '2022']].values[0]
固定资产_data = asset_df[asset_df['项目'] == '固定资产'][['2020', '2021', '2022']].values[0]

ax1.plot(years, 货币资金_data, marker='o', label='货币资金', linewidth=2)
ax1.plot(years, 存货_data, marker='s', label='存货', linewidth=2)
ax1.plot(years, 固定资产_data, marker='^', label='固定资产', linewidth=2)
ax1.set_xlabel('年份')
ax1.set_ylabel('金额（亿元）')
ax1.set_title('主要资产项目变化趋势')
ax1.legend()
ax1.grid(True, alpha=0.3)

# 图2：负债结构变化
ax2 = axes[0, 1]
合同负债_data = liability_df[liability_df['项目'] == '合同负债'][['2020', '2021', '2022']].values[0]
应付账款_data = liability_df[liability_df['项目'] == '应付账款'][['2020', '2021', '2022']].values[0]
其他应付款_data = liability_df[liability_df['项目'] == '其他应付款'][['2020', '2021', '2022']].values[0]

ax2.plot(years, 合同负债_data, marker='o', label='合同负债', linewidth=2)
ax2.plot(years, 应付账款_data, marker='s', label='应付账款', linewidth=2)
ax2.plot(years, 其他应付款_data, marker='^', label='其他应付款', linewidth=2)
ax2.set_xlabel('年份')
ax2.set_ylabel('金额（亿元）')
ax2.set_title('主要负债项目变化趋势')
ax2.legend()
ax2.grid(True, alpha=0.3)

# 图3：资产负债率变化
ax3 = axes[1, 0]
资产负债率 = metrics_df['资产负债率(%)'].values
ax3.bar(years, 资产负债率, color=['#3498db', '#2ecc71', '#e74c3c'])
ax3.set_xlabel('年份')
ax3.set_ylabel('资产负债率（%）')
ax3.set_title('资产负债率变化')
ax3.set_ylim(0, max(资产负债率) * 1.2)
for i, v in enumerate(资产负债率):
    ax3.text(years[i], v + 0.5, f'{v}%', ha='center', va='bottom')

# 图4：所有者权益结构
ax4 = axes[1, 1]
未分配利润_data = equity_df[equity_df['项目'] == '未分配利润'][['2020', '2021', '2022']].values[0]
盈余公积_data = equity_df[equity_df['项目'] == '盈余公积'][['2020', '2021', '2022']].values[0]

x = np.arange(len(years))
width = 0.35
ax4.bar(x - width/2, 未分配利润_data, width, label='未分配利润', color='#3498db')
ax4.bar(x + width/2, 盈余公积_data, width, label='盈余公积', color='#2ecc71')
ax4.set_xlabel('年份')
ax4.set_ylabel('金额（亿元）')
ax4.set_title('所有者权益主要项目')
ax4.set_xticks(x)
ax4.set_xticklabels(years)
ax4.legend()

plt.tight_layout()
output_chart = "/Users/huangyong/git/llm-caibao/五粮液资产负债表分析_2020-2022.png"
plt.savefig(output_chart, dpi=150, bbox_inches='tight')
print(f"图表已保存到: {output_chart}")

# 6. 主要发现
print("\n\n【六、主要发现】\n")

print("1. 资产规模变化：")
资产总计_2020 = metrics_df[metrics_df['年份'] == 2020]['资产总计(亿元)'].values[0]
资产总计_2022 = metrics_df[metrics_df['年份'] == 2022]['资产总计(亿元)'].values[0]
资产增长 = ((资产总计_2022 / 资产总计_2020 - 1) * 100)
print(f"   - 总资产从2020年的{资产总计_2020:.2f}亿元增长到2022年的{资产总计_2022:.2f}亿元")
print(f"   - 两年增长率为 {资产增长:.2f}%")

print("\n2. 资产结构特点：")
货币资金_2022 = asset_df[asset_df['项目'] == '货币资金']['2022'].values[0]
print(f"   - 货币资金占比极高，2022年达到{货币资金_2022:.2f}亿元")
print(f"   - 货币资金占总资产比例从2020年的{metrics_df[metrics_df['年份']==2020]['货币资金占比(%)'].values[0]:.1f}%")
print(f"     上升到2022年的{metrics_df[metrics_df['年份']==2022]['货币资金占比(%)'].values[0]:.1f}%")
print("   - 说明公司现金流非常充裕")

print("\n3. 负债水平：")
资产负债率_2020 = metrics_df[metrics_df['年份'] == 2020]['资产负债率(%)'].values[0]
资产负债率_2022 = metrics_df[metrics_df['年份'] == 2022]['资产负债率(%)'].values[0]
print(f"   - 资产负债率从2020年的{资产负债率_2020:.2f}%下降到2022年的{资产负债率_2022:.2f}%")
print("   - 财务风险较低，偿债能力强")

print("\n4. 盈利能力积累：")
未分配利润_2020 = equity_df[equity_df['项目'] == '未分配利润']['2020'].values[0]
未分配利润_2022 = equity_df[equity_df['项目'] == '未分配利润']['2022'].values[0]
print(f"   - 未分配利润从{未分配利润_2020:.2f}亿元增长到{未分配利润_2022:.2f}亿元")
print(f"   - 增长了 {((未分配利润_2022/未分配利润_2020-1)*100):.2f}%")
print("   - 显示公司持续盈利且利润留存较多")

print("\n" + "=" * 80)
print("分析完成！")
print("=" * 80)
