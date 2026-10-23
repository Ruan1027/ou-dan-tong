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
     "qty": 5000, "unit_price": "3.2", "delivery_date": "2026-11-20", "status_hint": "已报价",
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
     "qty": 5000, "unit_price": "3.2", "delivery_date": "2026-11-20", "status_hint": "已报价",
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
]

# 新建：全新客户+产品，字段齐全 → 正常建单
SCENARIO_NEW_MOCK = [
    {"type": "order", "customer": "瑞安精工", "product": "轴承座", "spec": "UC205",
     "qty": 120, "unit_price": "5.5", "delivery_date": "2026-12-01", "status_hint": "沟通中",
     "sample_flag": "无", "source_excerpt": "瑞安精工 轴承座 UC205 120个 12月1号交"},
]

# 更新：与在途单同客户+产品，且含变更动词「改成」→ 正常更新（不冲突）
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
