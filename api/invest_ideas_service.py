"""精选思想: 投资大师/方法 skill, 按用户隔离, 可增删改查。

首次访问自动为该用户初始化预置人物 (PRESET_IDEAS), 覆盖 价值投资/成长/趋势/
量化/ETF套利/宏观策略 等流派; 之后用户可自由 增/删/改/查 自己的思想条目。
"""
import json

from . import pg_service

# ---------------------------------------------------------------------------
# 预置人物 / 方法 (首次自动初始化)
# ---------------------------------------------------------------------------
PRESET_IDEAS: list[dict] = [
    {
        "name": "本杰明·格雷厄姆", "school": "价值投资",
        "tags": ["安全边际", "内在价值", "市场先生"],
        "bio": "价值投资之父，著有《证券分析》《聪明的投资者》，奠定了价值投资的基本框架，被誉为华尔街教父。",
        "principles": "1. 安全边际：只在价格远低于内在价值时买入\n2. 内在价值：基于资产与盈利能力客观估算\n3. 市场先生：利用市场情绪而非被其左右\n4. 股票是企业的所有权凭证，而非交易筹码\n5. 区分防御型与进攻型投资者的投资策略",
    },
    {
        "name": "菲利普·费雪", "school": "价值投资",
        "tags": ["成长股", "长期持有", "15要点"],
        "bio": "成长股投资大师，著有《怎样选择成长股》，强调投资于具有长期成长潜力的优秀公司。",
        "principles": "1. 只买少数几家真正卓越的公司\n2. 成长股的15个要点：产品/管理/研发/利润率/成长空间\n3. 长期持有，与优秀公司共同成长\n4. 买入前充分调研，买入后长期陪伴\n5. 重视管理层的能力与诚信",
    },
    {
        "name": "沃伦·巴菲特", "school": "价值投资",
        "tags": ["护城河", "能力圈", "长期持有"],
        "bio": "伯克希尔·哈撒韦掌门人，格雷厄姆与费雪思想的集大成者，以复利与长期持有著称。",
        "principles": "1. 只在能力圈内投资\n2. 关注企业护城河与商业模式\n3. 好公司+好价格，长期持有\n4. 别人恐惧时贪婪，别人贪婪时恐惧\n5. 投资的第一条规则是不要亏钱",
    },
    {
        "name": "查理·芒格", "school": "价值投资",
        "tags": ["多元思维", "逆向思考", "避免愚蠢"],
        "bio": "巴菲特的黄金搭档，强调跨学科多元思维模型与逆向思考，一生坚持理性与诚实。",
        "principles": "1. 多元思维模型：用多学科知识分析问题\n2. 反过来想：想清楚如何会失败，然后避开\n3. Lollapalooza 效应：多种因素共振产生大机会\n4. 避免愚蠢比追求聪明更重要\n5. 买入伟大公司并长期持有，少做决策",
    },
    {
        "name": "沃尔特·施洛斯", "school": "价值投资",
        "tags": ["便宜股", "分散", "不预测"],
        "bio": "格雷厄姆最忠实的弟子之一，坚持买便宜股并长期持有，五十年年化约15%。",
        "principles": "1. 只买便宜的股票，不看预期\n2. 适度分散（同时持有一百只）\n3. 不预测市场，不调研管理层\n4. 长期持有，等待价值回归\n5. 关注资产负债表与净资产",
    },
    {
        "name": "塞斯·卡拉曼", "school": "价值投资",
        "tags": ["安全边际", "绝对收益", "催化剂"],
        "bio": "《安全边际》作者，Baupost 基金掌门人，坚持绝对收益与低估值投资。",
        "principles": "1. 安全边际是投资的核心\n2. 绝对收益导向，不与大盘比\n3. 低估值+催化剂：便宜且有好消息\n4. 不随波逐流，敢于逆向\n5. 深度研究，宁可错过不可买错",
    },
    {
        "name": "霍华德·马克斯", "school": "价值投资",
        "tags": ["周期", "第二层思维", "风险控制"],
        "bio": "橡树资本创始人，著有《投资最重要的事》《周期》，以周期与风险控制闻名。",
        "principles": "1. 第二层思维：想别人所想不到的\n2. 万物皆有周期，钟摆总在摆动\n3. 风险控制优先于收益追求\n4. 在别人恐惧时乐观，乐观时谨慎\n5. 便宜是硬道理，好资产买贵也有风险",
    },
    {
        "name": "李录", "school": "价值投资",
        "tags": ["中国实践", "能力圈", "长期"],
        "bio": "喜马拉雅资本创始人，查理·芒格家族资产管理人，价值投资在中国的践行者。",
        "principles": "1. 价值投资在中国同样适用\n2. 坚守能力圈，只投看得懂的生意\n3. 以股东视角长期持有优质公司\n4. 关注文明、现代化与经济增长的长期趋势\n5. 少而精，重仓少数优秀公司",
    },
    {
        "name": "段永平", "school": "价值投资",
        "tags": ["本分", "商业模式", "敢为天下后"],
        "bio": "步步高/OPPO/vivo 创始人，投资网易、苹果等，强调本分与商业模式。",
        "principles": "1. 本分、平常心，不投机不贪婪\n2. 商业模式与企业文化决定长期价值\n3. 敢为天下后，后中争先\n4. 不做空、不融资、不懂不做\n5. 买股票就是买公司，长期持有",
    },
    {
        "name": "杰西·利弗莫尔", "school": "趋势投资",
        "tags": ["趋势跟踪", "关键点", "顺势"],
        "bio": "投机之王，趋势交易鼻祖，《股票大作手回忆录》记述其顺势交易的一生。",
        "principles": "1. 顺势而为，跟随大趋势\n2. 关键点突破时入场\n3. 截断亏损，让利润奔跑\n4. 市场永不犯错，承认错误并及时离场\n5. 等待时机，耐心是交易的美德",
    },
    {
        "name": "威廉·欧奈尔", "school": "趋势投资",
        "tags": ["CANSLIM", "强势股", "笑傲股市"],
        "bio": "CANSLIM 选股系统创始人，著有《笑傲股市》，强调买入强势成长股。",
        "principles": "1. C当季盈利大幅增长 A年度盈利持续增长\n2. N新产品/新管理/新高\n3. S供给与需求（小盘强势）\n4. L领涨股而非补涨股\n5. I机构认同 M跟随大盘\n6. 在突破买点买入，破位止损",
    },
    {
        "name": "乔治·索罗斯", "school": "趋势投资",
        "tags": ["反身性", "宏观", "泡沫"],
        "bio": "量子基金创始人，反身性理论提出者，擅长宏观趋势与泡沫交易。",
        "principles": "1. 反身性：认知与现实的相互影响\n2. 把握趋势的自我强化阶段\n3. 宏观分析：利率、汇率、政策\n4. 在泡沫形成中顺势，在崩溃前离场\n5. 敢于重仓，敢于认错",
    },
    {
        "name": "詹姆斯·西蒙斯", "school": "量化投资",
        "tags": ["量化", "信号驱动", "大奖章"],
        "bio": "文艺复兴科技创始人，大奖章基金多年年化约66%，纯量化、信号驱动的代表。",
        "principles": "1. 数据驱动：让模型而非情绪做决策\n2. 统计套利：捕捉市场统计规律\n3. 高频与分散：无数小优势累积\n4. 严格风控与执行纪律\n5. 招募顶尖科学家，不雇佣主观交易员",
    },
    {
        "name": "爱德华·索普", "school": "量化投资",
        "tags": ["凯利公式", "概率优势", "量化先驱"],
        "bio": "量化投资先驱，数学家与21点算牌之父，将概率与凯利公式用于投资。",
        "principles": "1. 寻找正期望值的下注机会\n2. 凯利公式确定最优仓位\n3. 利用统计与数学建立优势\n4. 分散与对冲降低风险\n5. 永远不做负期望的赌局",
    },
    {
        "name": "ETF 套利", "school": "ETF套利",
        "tags": ["折溢价", "申赎", "一二级市场"],
        "bio": "ETF 特有套利方法：利用一二级市场价差与申赎机制获取无风险/低风险收益，多为机构参与。",
        "principles": "1. 一二级市场申赎套利：折价买入/溢价卖出\n2. 折溢价套利：跟踪盘中 IOPV 与现价差\n3. 跨市场/跨品种套利\n4. 需考虑申赎门槛、费用与流动性\n5. 套利窗口转瞬即逝，需程序化执行",
    },
    {
        "name": "瑞·达利欧", "school": "宏观策略",
        "tags": ["全天候", "债务周期", "风险平价"],
        "bio": "桥水基金创始人，著有《原则》《债务危机》，提出全天候策略与风险平价配置。",
        "principles": "1. 全天候策略：多资产多场景配置\n2. 债务周期驱动宏观经济\n3. 风险平价：按风险而非市值配置\n4. 原则化决策：把经验写成原则\n5. 拥抱不确定性，保持系统性",
    },
    {
        "name": "吉姆·罗杰斯", "school": "宏观策略",
        "tags": ["商品", "宏观趋势", "独立思考"],
        "bio": "量子基金联合创始人，商品投资大师，环游世界寻找宏观投资机会。",
        "principles": "1. 独立思考，不随大流\n2. 投资于你了解的领域\n3. 关注供需与长期趋势\n4. 商品周期与资源稀缺性\n5. 在无人问津处寻找机会",
    },
]


def ensure_invest_ideas(user_id: int) -> None:
    """该用户无任何精选思想时, 自动初始化预置人物 (幂等)。"""
    pg_service.init_invest_ideas_schema()
    if pg_service.count_invest_ideas(user_id) == 0:
        for p in PRESET_IDEAS:
            pg_service.create_invest_idea(
                user_id, p["name"], p["school"],
                json.dumps(p["tags"], ensure_ascii=False),
                p["bio"], p["principles"])


def list_ideas(user_id: int) -> list[dict]:
    """确保预置并返回该用户全部精选思想 (tags 反序列化为列表)。"""
    ensure_invest_ideas(user_id)
    items = pg_service.list_invest_ideas(user_id)
    for it in items:
        try:
            it["tags"] = json.loads(it.get("tags") or "[]")
        except Exception:
            it["tags"] = []
    return items


def create_idea(user_id: int, name: str, school: str = "", tags: list | None = None,
                bio: str = "", principles: str = "") -> int:
    sid = pg_service.create_invest_idea(
        user_id, name, school,
        json.dumps(tags or [], ensure_ascii=False), bio, principles)
    return sid


def update_idea(sid: int, user_id: int, name: str | None = None, school: str | None = None,
                tags: list | None = None, bio: str | None = None,
                principles: str | None = None) -> int:
    return pg_service.update_invest_idea(
        sid, user_id, name=name, school=school,
        tags=json.dumps(tags, ensure_ascii=False) if tags is not None else None,
        bio=bio, principles=principles)


def delete_idea(sid: int, user_id: int) -> int:
    return pg_service.delete_invest_idea(sid, user_id)
