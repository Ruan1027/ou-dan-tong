"""模拟业务群聊数据 + 对应的抽取 Mock（无 Key 也能跑全管线）。
真实运行时由 LLM 产出同样结构的 list[dict]，Mock 仅用于测试/演示。"""

SCENARIO1_CHAT = """王工：李经理，NMX-60 断路器外壳 5000 个，什么时候能给报个价？客户挺急的
李经理：稍等，我算一下，明天上午给你
（第二天）
李经理：王工，NMX-60 外壳 5000 个，单价 3.2，11 月 20 号交，含 13% 税
王工：行，先安排打样 20 个发过来确认
万博机电：李经理，TND-3 稳压器 200 台麻烦排一下，下周要
李经理：好的，TND-3 下周一起排产，28 号前出货"""

SCENARIO1_MOCK = [
    {"type": "order", "customer": "王工（XX电气）", "product": "NMX-60 断路器外壳", "spec": "",
     "qty": 5000, "unit_price": "3.2", "delivery_date": "2026-11-20", "status_hint": "沟通中",
     "sample_flag": "打样", "source_excerpt": "NMX-60 外壳 5000 个，单价 3.2，11 月 20 号交…先安排打样 20 个"},
    {"type": "order", "customer": "万博机电", "product": "TND-3 稳压器", "spec": "",
     "qty": 200, "unit_price": "待报价", "delivery_date": "2026-10-28", "status_hint": "生产中",
     "sample_flag": "大货", "source_excerpt": "TND-3 下周一起排产，28 号前出货"},
    {"type": "task", "task_content": "安排打样 20 个", "owner": "李经理", "task_deadline": "2026-10-22"},
]

SCENARIO3_CHAT = "王工：李经理，NMX-60 那批数量改一下，改成 6000 个"
SCENARIO3_MOCK = [
    {"type": "order", "customer": "王工（XX电气）", "product": "NMX-60 断路器外壳",
     "qty": 6000, "source_excerpt": "NMX-60 那批数量改一下，改成 6000 个"},
]

# 缺信息陷阱：无客户/产品，应被跳过
MISSING_TRAP_MOCK = [
    {"type": "order", "customer": "", "product": ""},
]

# —— 网页体验增强：五类场景示例（覆盖模块二 缺失/冲突/异常/更新/新建）——
# 演示启动时会先 seed 以下在途单，使冲突/更新/异常场景能命中 existing（模块二可见），
# 并含「万博机电 TND-3」以便平台汇总页「查万博那单」能命中真实结果。
SEED_MOCK = [
    {"type": "order", "customer": "温州机电", "product": "法兰盘", "spec": "DN50",
     "qty": 300, "unit_price": "8.0", "delivery_date": "2026-11-25", "status_hint": "沟通中",
     "sample_flag": "无", "source_excerpt": "温州机电 法兰盘 DN50 300个 11月25号交"},
    {"type": "order", "customer": "王工（XX电气）", "product": "NMX-60 断路器外壳", "spec": "",
     "qty": 5000, "unit_price": "3.2", "delivery_date": "2026-11-20", "status_hint": "沟通中",
     "sample_flag": "打样", "source_excerpt": "王工 NMX-60 外壳 5000个 单价3.2 11月20号交"},
    {"type": "order", "customer": "万博机电", "product": "TND-3 稳压器", "spec": "",
     "qty": 200, "unit_price": "待报价", "delivery_date": "2026-10-28", "status_hint": "生产中",
     "sample_flag": "大货", "source_excerpt": "万博机电 TND-3 稳压器 200台 下周排产 28号前出货"},
]

# —— 网页体验增强（对齐模块一 chat_logs.json 真实实体）——
# 演示启动 seed 以下在途单，使「改数量/冲突/异常」按钮能命中 existing。
# 实体来源：模块一 S1-G1 / S1-G2 / S3-G3
#   · 王总（永嘉精密机械）/ 304法兰盘 DN80  → 承接「信息缺失 / 数据冲突」按钮
#   · 李姐（瓯越五金贸易）/ DZ47-63 三相断路器 → 承接「改数量 / 异常拦截」按钮
#   · 陈老板（瑞安汽配）/ 轴承 6204 → 仅作台账丰富（不与其他按钮实体撞名，避免「法兰」歧义）
# 新增订单按钮（阿珍/鹿城鞋材/EVA 鞋底）故意不 seed，以演示「新建」。
WEB_DEMO_SEED = [
    {"type": "order", "customer": "王总（永嘉精密机械）", "product": "304法兰盘 DN80", "spec": "",
     "qty": 250, "unit_price": "18.5", "delivery_date": "2026-10-28", "status_hint": "已确认",
     "sample_flag": "无", "source_excerpt": "王总 304法兰盘 DN80 250个 18.5 10-28交"},
    {"type": "order", "customer": "李姐（瓯越五金贸易）", "product": "DZ47-63 三相断路器", "spec": "",
     "qty": 50, "unit_price": "26", "delivery_date": "2026-10-27", "status_hint": "已确认",
     "sample_flag": "无", "source_excerpt": "李姐 DZ47-63 三相 50个 26 10-27交"},
    {"type": "order", "customer": "陈老板（瑞安汽配）", "product": "轴承 6204", "spec": "",
     "qty": 150, "unit_price": "7.0", "delivery_date": "2026-11-01", "status_hint": "已确认",
     "sample_flag": "无", "source_excerpt": "陈老板 轴承6204 150个 7.0 11-01交"},
    # 下面两单用于演示「同一台账并存多种状态」（模块二状态机 沟通中→已确认→生产中→部分交付→已交付），
    # 否则台账里清一色「已确认」，评审看不到状态机在跑。
    {"type": "order", "customer": "温州华信电器", "product": "继电器 JQX-13F", "spec": "",
     "qty": 300, "unit_price": "待报价", "delivery_date": "2026-11-05", "status_hint": "沟通中",
     "sample_flag": "无", "source_excerpt": "华信 继电器JQX-13F 300个 交期还没定"},
    {"type": "order", "customer": "瑞安汽配", "product": "刹车片 ECE-900", "spec": "",
     "qty": 80, "unit_price": "42", "delivery_date": "2026-10-30", "status_hint": "生产中",
     "sample_flag": "无", "source_excerpt": "瑞安汽配 刹车片ECE-900 80个 42元 10-30交"},
]

# 新建：全新客户+产品，字段齐全 → 正常建单
SCENARIO_NEW_MOCK = [
    {"type": "order", "customer": "瑞安精工", "product": "轴承座", "spec": "UC205",
     "qty": 120, "unit_price": "5.5", "delivery_date": "2026-12-01", "status_hint": "沟通中",
     "sample_flag": "无", "source_excerpt": "瑞安精工 轴承座 UC205 120个 12月1号交"},
]

# 更新：与在途单同客户+产品，且含变更动词「改成」→ 正常更新（不冲突）
# 注：本套 SCENARIO_*_MOCK 配对 SEED_MOCK（温州机电/王工 XX电气），供 test_pipeline 等
# 测试套件使用，**不要改动实体**；网页演示另用下方 WEB_SCENARIO_MOCKS（配 WEB_DEMO_SEED）。
SCENARIO_UPDATE_MOCK = [
    {"type": "order", "customer": "王工（XX电气）", "product": "NMX-60 断路器外壳",
     "qty": 6000, "source_excerpt": "NMX-60 那批数量改成 6000 个"},
]

# 缺失：新客户+产品，但数量/交期为空 → 登记占位单（模块二·7）
SCENARIO_MISSING_MOCK = [
    {"type": "order", "customer": "龙湾机械", "product": "齿轮箱", "spec": "",
     "qty": None, "unit_price": "待报价", "delivery_date": None, "status_hint": "沟通中",
     "sample_flag": "无", "source_excerpt": "龙湾机械 齿轮箱 再走一批（数量和交期都没说）"},
]

# 冲突：与在途单同客户+产品，交期矛盾且无变更动词 → 不覆盖，待人工确认（模块二·8）
SCENARIO_CONFLICT_MOCK = [
    {"type": "order", "customer": "温州机电", "product": "法兰盘",
     "delivery_date": "2026-11-28", "source_excerpt": "温州机电 法兰盘 交期 11-28"},
]

# 异常：数量为负数 → 直接拦截，不写入（模块二·9）
SCENARIO_ABNORMAL_MOCK = [
    {"type": "order", "customer": "温州机电", "product": "法兰盘",
     "qty": -50, "source_excerpt": "法兰盘 数量 -50（手滑多打了负号）"},
]


# ---------------------------------------------------------------------------
# 网页演示专用场景数据：实体与 WEB_DEMO_SEED 严格对齐
# ---------------------------------------------------------------------------
# 用途：① 模拟模式按场景取预置抽取；② 真实模式下大模型网关超时时，用它降级重跑，
#      让模块二的规则判定（新建/更新/缺失/冲突/异常）照常产出结论，而不是甩 TimeoutError。
# 若实体与 WEB_DEMO_SEED 不一致，find_active 命中不了在途单，更新/冲突会被误判成
# 「信息不全占位」——改这里必须同步核对上面的 WEB_DEMO_SEED。
WEB_SCENARIO_MOCKS = {
    "new": [
        {"type": "order", "intent": "new", "customer": "阿珍（鹿城鞋材）", "product": "EVA 鞋底", "spec": "秋季款发泡",
         "qty": 800, "unit_price": "待报价", "delivery_date": "2026-11-10", "status_hint": "沟通中",
         "sample_flag": "无", "source_excerpt": "老周，秋季那款运动鞋鞋底 EVA 发泡的，先给我 800 双，最好 11 月 10 号前到"},
    ],
    "update": [
        {"type": "order", "intent": "update", "customer": "李姐（瓯越五金贸易）", "product": "DZ47-63 三相断路器",
         "qty": 80, "source_excerpt": "小陈，DZ47 那个加到 80 个"},
    ],
    # 缺失：客户说「再帮我排上一批」——「一批」是模糊量词，数量必须按缺失处理。
    # 语义上是追加（在途单还在），intent=append；数量未明确。
    # 2026-10-24 修复漏单：旧逻辑「等人工确认数量后再挂原单」→ 此间台账没有这笔，
    # 汇总少计一笔。现改为立即登记「待补充」追加占位单（挂原单、is_append、进台账/汇总），
    # 数量补齐后再补全这张单，保证不漏单、不重复建单。
    "missing": [
        {"type": "order", "intent": "append", "customer": "王总（永嘉精密机械）", "product": "304法兰盘 DN80", "spec": "",
         "qty": None, "unit_price": "待报价", "delivery_date": None, "status_hint": "沟通中",
         "sample_flag": "无", "source_excerpt": "小陈，法兰那批的货再帮我排上一批，我这边急"},
    ],
    # 追加：数量明确 → 新开一条追加单挂到原单 ORD-01 下，原单数量不变。
    # 话术要点（2026-10-23 用户口径）：追加的标志是「另外排一批 / 分开走」；
    # 「再加 200 个，跟上次一起发」= 在原单上加量，算改数量不算追加。
    "append": [
        {"type": "order", "intent": "append", "customer": "王总（永嘉精密机械）", "product": "304法兰盘 DN80", "spec": "",
         "qty": 300, "unit_price": "18.5", "delivery_date": "2026-11-15", "status_hint": "已确认",
         "sample_flag": "无", "source_excerpt": "小陈，法兰那批的货再排一批 300 个的，单价还是 18.5，11-15 交，跟原来那单分开走"},
    ],
    "conflict": [
        {"type": "order", "intent": "update", "customer": "王总（永嘉精密机械）", "product": "304法兰盘 DN80",
         "unit_price": "18", "source_excerpt": "小陈，法兰 DN80 那单单价你按 18 算，上次说好的"},
    ],
    "abnormal": [
        {"type": "order", "intent": "update", "customer": "李姐（瓯越五金贸易）", "product": "DZ47-63 三相断路器",
         "qty": -300, "source_excerpt": "小陈，DZ47 那个数量给我改成 -300"},
    ],
}
