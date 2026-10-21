"""模块五 · 测试支撑夹具（队友B 自用，仅供测试 import，不参与生产链路）。

做两件事：
1. 校验模块一数据集 `03_演示数据/data/chat_logs.json`：
   - 符合契约 1（每条消息含 group/speaker/time/text 四键，组内按时间升序）；
   - 覆盖五类场景（新建/迭代更新/缺失/冲突/异常）每类 ≥2 组，且含 ≥1 组动态演进。
2. 把每个场景 `expected` 元数据转成「理想抽取器输出」（抽取行 list[dict]），
   使场景走查在无 API Key 环境下也能驱动 parse_chat 全链路。

口径说明：真实运行时这一段由 LLM（模块二·extractor）完成。本夹具只表达
「抽取正确」这一前提下的走查意图，用于验证下游 校验/存储/编排 是否符合
模块一标注的 `validator_target`，**不替代对模块二抽取能力本身的评测**
（那需配置 DEEPSEEK_API_KEY 后跑真实模式）。
"""
import os
import re
import json

_HERE = os.path.dirname(os.path.abspath(__file__))
CHAT_LOGS_PATH = os.path.normpath(
    os.path.join(_HERE, "..", "..", "03_演示数据", "data", "chat_logs.json"))

# 场景走查统一使用固定「今天」，保证异常/临期等日期类判定可复现
FIXED_NOW = "2026-10-21T10:00:00"
FIXED_DATE = FIXED_NOW[:10]

# 五类场景（模块一·1 覆盖清单）
SCENE_TYPES = ["新建订单", "多次迭代更新", "缺失信息", "矛盾冲突", "明显异常"]

# 实体主键（客户 + 产品），与模块一 expected.entity_key_hint 对齐
C_YONGJIA = "王总（永嘉精密机械）"
P_FLANGE = "304法兰盘DN80"
C_OUYUE = "李姐（瓯越五金贸易）"
P_DZ47 = "DZ47-63 三相断路器"
P_MOTOR = "电机壳 Y2-132"


# ---------------------------------------------------------------------------
# 模块一数据集加载与契约校验
# ---------------------------------------------------------------------------
def load_chat_logs(path: str = CHAT_LOGS_PATH):
    if not os.path.exists(path):
        raise FileNotFoundError(f"模块一数据集缺失：{path}（模块五·2 依赖模块一数据）")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else data.get("groups", [])


def check_contract1(groups):
    """返回问题列表；空列表表示数据集符合契约 1 与覆盖要求。"""
    problems = []
    by_type = {}
    for g in groups:
        sid = g.get("scene_id", "?")
        msgs = g.get("messages") or []
        if not msgs:
            problems.append(f"{sid}: 无消息")
            continue
        for i, m in enumerate(msgs):
            for k in ("group", "speaker", "time", "text"):
                if k not in m:
                    problems.append(f"{sid}: 第{i+1}条消息缺契约1字段「{k}」")
        # 组内时间升序（动态演进的前提）
        times = [m.get("time", "") for m in msgs]
        if times != sorted(times):
            problems.append(f"{sid}: 消息时间非升序 {times}")
        t = g.get("scene_type")
        by_type.setdefault(t, []).append(sid)

    for t in SCENE_TYPES:
        n = len(by_type.get(t, []))
        if n < 2:
            problems.append(f"场景「{t}」仅 {n} 组，不满足每类≥2 组")
    if len(by_type.get("动态演进", [])) < 1:
        problems.append("缺少「动态演进」样本（要求≥1 组）")
    return problems, by_type


# ---------------------------------------------------------------------------
# 场景 → 抽取行夹具
# ---------------------------------------------------------------------------
def _order(customer, product, qty=None, price="待报价", date=None,
           status="沟通中", spec="", excerpt=""):
    return {"type": "order", "customer": customer, "product": product, "spec": spec,
            "qty": qty, "unit_price": price, "delivery_date": date,
            "status_hint": status, "sample_flag": "无", "source_excerpt": excerpt}


def _task(content, owner="", deadline=None):
    return {"type": "task", "task_content": content, "owner": owner,
            "task_deadline": deadline}


def _norm_date(s):
    """模块一 expected 里交期写作 '2026-10-28' / '10-24' / '两批：11-08（300件）/11-15（200件）'。

    统一取**最先出现**的那段日期（动态演进场景取第一批交期）。
    """
    if not s:
        return None
    s = str(s)
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return m.group(0)
    m = re.search(r"(?<!\d)(\d{1,2})-(\d{1,2})(?!\d)", s)
    if m:
        return f"2026-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return None


# scene_id -> 期望判定（取自模块一 expected.validator_target）
EXPECTED_TARGET = {
    "S1-G1": "ok", "S1-G2": "ok",
    "S2-G1": "ok", "S2-G2": "ok",
    "S3-G1": "missing", "S3-G2": "missing",
    "S4-G1": "conflict", "S4-G2": "conflict",
    "S5-G1": "abnormal", "S5-G2": "abnormal",
    "S6-E1": "ok",
}

# scene_id -> 走查轮次（每轮=一次群聊导入；多轮=动态演进）
# 字段取自模块一各场景的 expected.fields / notes，source_excerpt 保留原文关键措辞
SCENE_ROUNDS = {
    "S1-G1": [[_order(C_YONGJIA, P_FLANGE, 250, "18.5", "2026-10-28", "已确认",
                      spec="DN80", excerpt="法兰 DN80 250 件，交期 28 号，价格还是上次那个 18.5")]],
    "S1-G2": [[_order(C_OUYUE, P_DZ47, 50, "26", "2026-10-27", "已确认",
                      spec="三相", excerpt="DZ47-63 三相 50 个，单价 26，下周二 27 号发货")]],
    # 迭代更新：命中 S1-G1 主键，含变更动词 → 应 UPDATE 不新建
    "S2-G1": [[_order(C_YONGJIA, P_FLANGE, 300, "18.5", "2026-10-24", "已确认",
                      spec="DN80", excerpt="法兰那单数量加 50 凑 300；交期改 10-24")]],
    # 迭代更新：数量 50→80→65、交期 27→24，拆三轮验证履历累积
    "S2-G2": [
        [_order(C_OUYUE, P_DZ47, 80, "26", "2026-10-27", "已确认",
                spec="三相", excerpt="DZ47 那个加到 80 个")],
        [_order(C_OUYUE, P_DZ47, 65, "26", "2026-10-27", "已确认",
                spec="三相", excerpt="算了还是 65 个，客户临时砍了")],
        [_order(C_OUYUE, P_DZ47, 65, "26", "2026-10-24", "已确认",
                spec="三相", excerpt="改周五 24 号，客户催得急")],
    ],
    # 缺失：复用 S1-G1 主键但缺数量/交期；实体已存在 → 走「不变更」而非追问
    "S3-G1": [[_order(C_YONGJIA, P_FLANGE, None, "待报价", None, "沟通中",
                      spec="DN80", excerpt="法兰那批的货再帮我排上一批，我这边急")]],
    # 缺失：全新实体 NMX-60，缺数量 → 应追问并占位
    "S3-G2": [[_order(C_OUYUE, "NMX-60", None, "待报价", None, "沟通中",
                      excerpt="NMX-60 再给我走一批")]],
    # 冲突：无变更动词的 280，与台账 300 打架 → 不覆盖、待确认
    "S4-G1": [[_order(C_YONGJIA, P_FLANGE, 280, "18.5", "2026-10-24", "已确认",
                      spec="DN80", excerpt="我早上记的是 280 个，你怎么跟车间说 300")]],
    # 冲突：交期 10-28 与台账 10-24 矛盾（三方口径 25/28/29）→ 不覆盖
    "S4-G2": [[_order(C_OUYUE, P_DZ47, 65, "26", "2026-10-28", "已确认",
                      spec="三相", excerpt="交期先按 28 号备着")]],
    # 异常：负数量 → 拦截不写入
    "S5-G1": [[_order(C_OUYUE, P_DZ47, -300, "26", "2026-10-24", "已确认",
                      spec="三相", excerpt="DZ47 那个数量给我改成 -300")]],
    # 异常：交期 10-19 早于当天 10-21（倒挂）→ 拦截不写入
    "S5-G2": [[_order(C_YONGJIA, P_FLANGE, 300, "18.5", "2026-10-19", "已确认",
                      spec="DN80", excerpt="交期给我写 10-19，今天都 21 号了")]],
    # 动态演进：数量 400→500，交期 11-09→11-05→11-08（分批），履历 ≥3 条
    "S6-E1": [
        [_order(C_YONGJIA, P_MOTOR, 400, "待报价", "2026-11-09", "已确认",
                excerpt="Y2-132 电机壳 400 个，11-09 交，给您排上")],
        [_order(C_YONGJIA, P_MOTOR, 500, "待报价", "2026-11-05", "已确认",
                excerpt="电机壳改 500 个，交期最好赶 11 月 5 号")],
        [_order(C_YONGJIA, P_MOTOR, 500, "待报价", "2026-11-08", "已确认",
                excerpt="交期改成 11-08 先出 300，11-15 出 200")],
    ],
}


def iter_scene_rounds(groups):
    """按模块一数据文件顺序产出 (scene, round_index, rounds, mock)。"""
    for g in groups:
        sid = g.get("scene_id")
        rounds = SCENE_ROUNDS.get(sid)
        if rounds is None:
            continue          # 模块一新增场景未配夹具时跳过，由走查报告提示
        for i, mock in enumerate(rounds):
            yield g, i, mock
