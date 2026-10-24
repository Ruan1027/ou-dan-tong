# 瓯单通 · 可运行网页智能体

把「碎片化沟通中的订单与任务协同」实现为本地可运行 Python 原型：**零第三方依赖**（仅标准库），自带网页界面，可直接演示与验收。

## 目录结构

```
ou_dan_tong/
├── web_app.py          零依赖网页服务（HTTP + 静态页）
├── static/index.html   网页界面（微信沟通页 / 平台汇总台账 / 四维筛选）
├── main.py             CLI 入口
├── agent.py            编排：parse_chat / query / briefing / handle_message 意图路由
├── extractor.py        LLM 结构化抽取（支持 mock）
├── llm.py              极简 OpenAI 兼容调用（零依赖）
├── prompts.py          提示词骨架 + 卡片 / 查询 / 简报话术
├── validator.py        确定性校验 + 变更比对（缺失 / 冲突 / 异常 四级）
├── contracts.py        契约单一真相源（消息 / 变更 / 订单任务 / 校验结果）
├── store.py            本地 JSON 台账（订单 / 任务 / 状态机 / 批次）
├── sim_chats.py        模拟群聊 + 场景 Mock
├── config.py           模型与运行配置（Key 走 .env / 环境变量）
├── .env.example        配置模板（复制为 .env 后填 Key）
├── runtime/            运行时数据（orders.json / tasks.json，自动生成，已被 .gitignore）
├── test_pipeline.py    全链路断言（154 条）
├── test_scenarios.py   场景走查（30 轮）
├── test_defects.py     缺陷回归（14 项）
├── test_real_mode.py   真实模式链路（100 项，零网络）
├── test_e2e.py         端到端 HTTP 验收（22 项）
├── test_module4.py     模块四专项（--smoke）
├── test_store.py / test_validator.py / test_fixtures.py
├── test_colloquial_routing.py / test_sender_injection.py
├── run_all_tests.py    一键跑全部
└── 模块二_演示输出.txt / 模块五_测试报告.md / 模块四_迭代优化清单.md / collect_failures.md
```

## 一、启动（网页，推荐）

```bash
cd ou_dan_tong
python web_app.py
# 浏览器打开 http://127.0.0.1:8000
```

- 未配 Key → **模拟模式**（预置数据，免费演示全流程）。
- 配 Key → **真实模式**（抽取任意群聊）。

## 二、配置真实模型

```bash
cp .env.example .env
# 编辑 .env：
# OPENAI_BASE_URL=https://api.siliconflow.cn/v1
# OPENAI_API_KEY=你的Key
# OPENAI_MODEL=deepseek-ai/DeepSeek-V4.1-Flash
python web_app.py
```

- 支持任意 OpenAI 兼容端点（硅基流动 / 智谱 / 通义 / MiniMax 等）。
- 也可直接用环境变量 `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `OPENAI_MODEL`。
- **Key 不入库**：`.env` 已被 `.gitignore` 忽略，仓库只放 `.env.example`。

## 三、测试

```bash
python run_all_tests.py
# 或单跑：test_pipeline.py / test_scenarios.py / test_defects.py / test_real_mode.py / test_e2e.py
```

五套全绿：pipeline 154 / scenarios 30 / defects 14 / real_mode 100 / e2e 22。

## 四、设计要点

- **防幻觉**：所有回复带 `source_excerpt` 原文依据；简报数字全部来自台账查询，LLM 不编数。
- **判定分层**：LLM 只做结构化抽取；缺失 / 冲突 / 异常四级判定、状态流转与话术全部为本地规则，可解释、可回归。
- **变更留痕**：同客户+产品在途单更新时追加 `changes` 履历；状态只进不退。
- **追加单**：客户说「再排一批」而数量模糊时，登记「待补充」追加单挂原单，不漏单。

## 五、模块状态

模块一（抽取）/ 二（判定）/ 三（台账）/ 四（查询简报）/ 五（测试）/ 六（网页）**均已完成**，五套测试把关。
