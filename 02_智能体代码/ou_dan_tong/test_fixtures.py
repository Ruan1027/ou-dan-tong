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
    "S1-G1": "ok", "S1-G2": "ok", "S1-G3": "ok",
    "S2-G1": "ok", "S2-G2": "ok", "S2-G3": "ok",
    "S3-G1": "missing", "S3-G2": "missing", "S3-G3": "missing",
    "S4-G1": "conflict", "S4-G2": "conflict", "S4-G3": "conflict",
    "S5-G1": "abnormal", "S5-G2": "abnormal", "S5-G3": "abnormal",
    "S6-E1": "ok", "S6-E2": "ok",
    "S7-T1": "ok", "S7-T2": "missing",
    "S8-C1": "ok", "S8-P1": "ok",
    "S9-Q1": "ok",
}

# 按轮覆盖期望：(scene_id, round) -> 判定
# 模块一的 expected.validator_target 描述的是「整个场景走完后的最终态」，
# 而走查是逐轮判定的。多轮场景中，中间轮的真实语义往往与场景级期望不同，
# 此处按**轮次语义**覆盖，避免把「合理的中间态」误判为失败。
ROUND_EXPECT = {
    # S9-Q1 第1轮是「询价、数量还没定」→ 缺数量属实，应追问占位（missing 才是正确行为）
    ("S9-Q1", 0): "missing",
}

# 场景类型 → 走查关注面（判断依据）
SCENE_FOCUS = {
    "S8-C1": "取消订单", "S8-P1": "部分交付", "S9-Q1": "价格报价",
}

# scene_id -> 走查轮次（每轮=一次群聊导入；多轮=动态演进）
# 字段取自模块一各场景的 expected.fields / notes，source_excerpt 保留原文关键措辞
SCENE_ROUNDS = {
    "S1-G1": [[_order(C_YONGJIA, P_FLANGE, 250, "18.5", "2026-10-28", "已确认",
                      spec="DN80", excerpt="法兰 DN80 250 件，交期 28 号，价格还是上次那个 18.5")]],
    "S1-G2": [[_order(C_OUYUE, P_DZ47, 50, "26", "2026-10-27", "已确认",
                      spec="三相", excerpt="DZ47-63 三相 50 个，单价 26，下周二 27 号发货")]],
    # 新建：全新客户（阿珍），一次确认建单
    "S1-G3": [[_order("阿珍（鹿城××鞋材）", "EVA 发泡鞋底", 800, "12.5",
                      "2026-11-10", "已确认", excerpt="EVA 发泡鞋底 800 双，单价 12.5，11-10 前发货")]],
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
    # 迭代更新：命中 S1-G3 主键，数量 800→1000、交期提前
    "S2-G3": [[_order("阿珍（鹿城××鞋材）", "EVA 发泡鞋底", 1000, "12.5",
                      "2026-11-05", "已确认", excerpt="鞋底那单加量凑 1000，交期赶 11-05")]],
    # 缺失：复用 S1-G1 主键但缺数量/交期；实体已存在 → 走「不变更」而非追问
    "S3-G1": [[_order(C_YONGJIA, P_FLANGE, None, "待报价", None, "沟通中",
                      spec="DN80", excerpt="法兰那批的货再帮我排上一批，我这边急")]],
    # 缺失：全新实体 NMX-60，缺数量 → 应追问并占位
    "S3-G2": [[_order(C_OUYUE, "NMX-60", None, "待报价", None, "沟通中",
                      excerpt="NMX-60 再给我走一批")]],
    # 缺失：全新实体轴承6204，缺数量/交期（老规矩=沿用上批，抽取器无从取值）
    "S3-G3": [[_order("陈老板（瑞安××汽配）", "轴承6204", None, "待报价", None,
                      "沟通中", excerpt="轴承 6204 再给我发一车，老规矩")]],
    # 冲突：无变更动词的 280，与台账 300 打架 → 不覆盖、待确认
    "S4-G1": [[_order(C_YONGJIA, P_FLANGE, 280, "18.5", "2026-10-24", "已确认",
                      spec="DN80", excerpt="我早上记的是 280 个，你怎么跟车间说 300")]],
    # 冲突：交期 10-28 与台账 10-24 矛盾（三方口径 25/28/29）→ 不覆盖
    "S4-G2": [[_order(C_OUYUE, P_DZ47, 65, "26", "2026-10-28", "已确认",
                      spec="三相", excerpt="交期先按 28 号备着")]],
    # 冲突：命中 S1-G1 实体的单价，来单 18 与台账 18.5 矛盾 → 不覆盖，人工核对
    "S4-G3": [[_order(C_YONGJIA, P_FLANGE, 250, "18", "2026-10-28", "已确认",
                      spec="DN80", excerpt="法兰 DN80 单价按 18 算，上次说好的")]],
    # 异常：负数量 → 拦截不写入
    "S5-G1": [[_order(C_OUYUE, P_DZ47, -300, "26", "2026-10-24", "已确认",
                      spec="三相", excerpt="DZ47 那个数量给我改成 -300")]],
    # 异常：交期 10-19 早于当天 10-21（倒挂）→ 拦截不写入
    "S5-G2": [[_order(C_YONGJIA, P_FLANGE, 300, "18.5", "2026-10-19", "已确认",
                      spec="DN80", excerpt="交期给我写 10-19，今天都 21 号了")]],
    # 异常：命中 S1-G2 实体，数量 5000 疑似多打零（要求改为 50）→ 拦截，等澄清
    "S5-G3": [[_order(C_OUYUE, P_DZ47, 5000, "26", "2026-10-27", "已确认",
                      spec="三相", excerpt="DZ47 数量写成 5000 了吧，我明明要 50")]],
    # 动态演进：数量 400→500，交期 11-09→11-05→11-08（分批），履历 ≥3 条
    "S6-E1": [
        [_order(C_YONGJIA, P_MOTOR, 400, "待报价", "2026-11-09", "已确认",
                excerpt="Y2-132 电机壳 400 个，11-09 交，给您排上")],
        [_order(C_YONGJIA, P_MOTOR, 500, "待报价", "2026-11-05", "已确认",
                excerpt="电机壳改 500 个，交期最好赶 11 月 5 号")],
        [_order(C_YONGJIA, P_MOTOR, 500, "待报价", "2026-11-08", "已确认",
                excerpt="交期改成 11-08 先出 300，11-15 出 200")],
    ],
    # 动态演进：数量 500→900；交期 11-01→11-03→两批（11-01 500 / 11-03 400）
    "S6-E2": [
        [_order("阿珍（鹿城××鞋材）", "PU 皮鞋面", 500, "待报价", "2026-11-01",
                "已确认", excerpt="PU 皮 500 米，11-01 到")],
        [_order("阿珍（鹿城××鞋材）", "PU 皮鞋面", 900, "待报价", "2026-11-03",
                "已确认", excerpt="鞋面加量改 900，客户爆单了")],
        [_order("阿珍（鹿城××鞋材）", "PU 皮鞋面", 900, "待报价", "2026-11-01",
                "已确认", excerpt="分两批：11-01 出 500，11-03 出 400")],
    ],
    # 任务派发：内容+负责人+期限齐全 → ok
    "S7-T1": [[_task("跟进物流单号", "老周", "2026-10-21 17:00")]],
    # 任务派发：缺负责人/期限 → 追问并占位
    "S7-T2": [[_task("安排 DZ47 质检报告", "", None)]],
    # 取消订单：命中已有实体，状态改为已取消 → 应 UPDATE（当前 STATUS_ORDER 无此态，待验证）
    "S8-C1": [
        [_order("陈老板（瑞安××汽配）", "轴承6204", 100, "6.0", "2026-11-10",
                "已确认", excerpt="轴承 6204 100 个，11-10 交")],
        [_order("陈老板（瑞安××汽配）", "轴承6204", 100, "6.0", "2026-11-10",
                "已取消", excerpt="6204 那单先取消吧，客户订单黄了")],
    ],
    # 部分交付：数据集 entity_key_hint 指向 S1-G1 实体（王总 + 304法兰盘DN80），
    # 走查到此处台账已存在（数量 300、交期 10-24），故只走一轮「发货进度更新」：
    # 数量/交期沿用台账现值，聚焦「部分交付」语义是否落地（BUG-11）。
    "S8-P1": [
        [_order(C_YONGJIA, P_FLANGE, 300, "18.5", "2026-10-24", "部分交付",
                spec="DN80", excerpt="300 个先发了 120 个出去，剩下月底凑")],
    ],
    # 价格报价：询价（数量待定）→ 报价（200 个 × 9.8）→ 应 UPDATE 单价与状态
    "S9-Q1": [
        [_order("阿强", "304不锈钢法兰盖", None, "待报价", None, "沟通中",
                excerpt="304 不锈钢法兰盖先问下价，数量还没定")],
        [_order("阿强", "304不锈钢法兰盖", 200, "9.8", None, "已报价",
                excerpt="先 200 个，单价 9.8 锁死")],
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
