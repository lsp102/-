# FallGuard Platform · 平台说明

> 一套面向多智能体协作的**编排、调度、执行与验证平台**。
> 平台不认识任何业务；业务以 **scenario pack** 形式接入，装卸即用。

---

## 1. 定位：不是应用，是 harness

普通做法是把"跌倒响应"写成一个程序。这里反过来：

```
平台（引擎 + 注册表 + 验证器）
      ↑ 只认 workflow.json / agents.json / scenarios.json
业务 pack（跌倒响应、多源调研、……）
```

**判断标准很简单**：换一个完全无关的领域，要不要改平台代码？
本项目里答案是"不用"——`packs/fall-response`（串行 + 人工窗口 + 升级链）和
`packs/research-brief`（并行扇出 + 汇合 + 校验回退）跑在同一个引擎上，零改动。

---

## 2. 分层

| 层 | 文件 | 职责 |
|---|---|---|
| 表达式层 | `expr.py` | AST 白名单求值，工作流里所有条件/参数都用它 |
| 轨迹层 | `trace.py` | 每个节点一条记录，UI / 断言 / 复盘统一读它 |
| 知识层 | `knowledge.py` | 知识源注册中心（规则库 / 文档库 / 经验库） |
| 包层 | `pack.py` | pack 发现与加载，`tools.py` / `handlers.py` 动态导入 |
| 引擎层 | `engine.py` | 10 种节点原语 + 执行循环 + 权限校验 + 断言 |
| 图层 | `graph.py` | 把 workflow.json 转成节点图（分层布局 + 边） |
| 入口层 | `cli.py` | list / info / run / validate |
| 服务层 | `server.py` | HTTP API + 控制台（增量轨迹推送） |

---

## 3. 节点原语（编排的 10 个操作）

| 类型 | 语义 | 关键字段 |
|---|---|---|
| `input` | 工作流入口 | `next` |
| `agent` | 调一个已注册 Agent | `agent` `input` `out` `next` |
| `tool` | 调一个已注册工具（受 Agent 白名单约束） | `agent` `tool` `args` `out` |
| `condition` | 条件分支 | `when` `then` `else` |
| `parallel` | 并行扇出，全部完成后汇合 | `branches[]` `next` |
| `foreach` | 遍历 + 中断条件（升级链 / 重试链） | `over` `body` `break_when` `on_break` `on_exhausted` `max` |
| `wait` | 可取消等待（人工确认窗口 / 超时） | `duration` `on_cancel` `next` |
| `knowledge` | 知识库检索 | `query` `k` `out` |
| `transform` | 写入上下文 | `set{}` `next` |
| `end` | 终止并标记最终状态 | `status` |

每个节点都可以带 `actor`（写进轨迹，用于断言与展示）和 `on_error`（错误转移）。

**轨迹里的 `status` 是一等公民**：`ok` / `veto` / `cancel` / `error`。
handler 返回 `_status` 即可上报"我否决了一个动作"——护栏不是日志里的一句话，是可被断言的执行事实。

---

## 4. Pack 规范

```
packs/<id>/
├─ pack.json        元信息 + 默认参数 + workflow 映射
├─ agents.json      Agent 团队：角色 / 权限 / 工具白名单 / 驱动方式
├─ workflows/*.json 工作流定义
├─ data/*.json      领域数据（档案 / 规则 …），引擎按文件名暴露为 data.<name>
├─ kb/*.md          文档知识库（按 ## 切片）
├─ scenarios.json   验证场景 + 期望结果
├─ tools.py         TOOLS = {name: fn}
└─ handlers.py      HANDLERS = {name: fn}
```

`pack.json`：

```json
{
  "id": "fall-response",
  "name": "独居老人跌倒响应",
  "default_workflow": "response",
  "workflows": { "response": "workflows/response.json", "single": "workflows/single.json" },
  "params": { "ring_timeout_s": 30.0 }
}
```

一个 pack 可以挂多条工作流。跌倒包里 `response` 是多智能体主链路，
`single` 是单 Agent 对照组——**对照组不是另写一个程序，是另一张图**。

`agents.json`：

```json
{
  "agents": {
    "contact": {
      "name": "联络",
      "driver": "handler",
      "handler": "draft",
      "permissions": ["push"],
      "tools": ["push"]
    }
  }
}
```

- `driver`: `handler`（代码驱动，确定性）或 `llm`（LLM 驱动，读 `prompt`）
- `tools`: 工具白名单。调用未授权工具直接抛 `PermissionDenied`——权限不是文档，是运行时约束

---

## 5. 表达式语言

工作流里的 `when` / `args` / `set` 都是同一个表达式语言。

- 可用变量：`ctx` `event` `profile` `params` `data` `item` `index`
- 属性即字典键：`ctx.triage.level`、`profile.name`
- 白名单函数：`len str int float bool min max sum any all abs get`
- **禁用**：`eval`、下划线属性、非白名单调用、赋值、lambda

> 不用 `eval` 是有代价的（要自己写 AST 遍历），但它是能写进安全评审的选择。
> 工作流来自配置文件，如果直接 `eval`，等于允许配置文件执行任意代码。

---

## 6. 验证与断言

`scenarios.json` 里每个场景带 `expect`，跑完自动比对：

| 断言键 | 含义 |
|---|---|
| `final` | 最终状态 |
| `ctx.<path>` | 上下文取值，如 `ctx.triage.forbid_move` |
| `trace.veto` | 轨迹中某状态出现次数（`ok`/`veto`/`cancel`/`error`） |
| `trace.node:<id>` | 某节点执行次数 |
| `trace.actor:<name>` | 某角色产生了几步 |

```bash
python -m harness.cli validate fall-response               # 8/8
python -m harness.cli validate fall-response --workflow single   # 4/8（对照组）
python -m harness.cli validate research-brief              # 2/2
```

**验证套件是重构的安全网**：换引擎、改节点语义，跑一遍就知道有没有回归。

---

## 7. 两个 pack 的差异（解耦证据）

| | fall-response | research-brief |
|---|---|---|
| 拓扑 | 串行 + 遍历升级链 | 并行扇出 + 汇合 |
| 人工介入 | `wait` 撤销窗口 | 无 |
| 知识库 | 规则库 + 文档库 | 无 |
| 工具 | 3 个 | 0 个 |
| 断言重点 | `final` + `veto` | `final` + 汇合结果 |
| 引擎改动 | — | **0 行** |

---

## 8. 加第三个 pack（5 步）

1. 建目录 `packs/<id>/`
2. 写 `agents.json`：定义角色、权限、工具白名单
3. 写 `workflows/<name>.json`：用 10 种节点拼图
4. 写 `handlers.py` / `tools.py`：领域逻辑（平台不碰）
5. 写 `scenarios.json` + `expect`，跑 `validate`

**不需要改 `harness/` 下的任何文件。**

---

## 9. 控制台

```bash
python -m harness.server 8000     # http://127.0.0.1:8000
```

三块内容：

| 区域 | 显示什么 |
|---|---|
| 左侧 | pack 列表 + 场景列表（点一下就跑） |
| 中上 | **工作流图**：22 个节点、21 条边，按类型着色；执行到哪个节点，哪个节点发光 |
| 中下 | **实时轨迹**：每步的节点 / 类型 / 角色 / 状态 / 摘要，`veto` 高亮成紫色 |
| 底部条 | 结局、仿真耗时、步数、断言结果 |

「演示节奏」勾选后节点间插入 380ms 停顿（`pace_ms`），作用是让评委看清编排过程；
它只影响观感，**不影响仿真耗时指标**。

增量轨迹接口：`GET /api/run?id=<run>&since=<n>` 只返回新步，前端 110ms 轮询。
引擎通过 `on_step` 回调实时推送，不依赖跑完再取。

---

## 10. 与旧版 `app/` 的关系

`app/` 是第一版"把流程写死在 Python 里"的实现，保留作为对照：

- 旧版：流程是代码 → 换场景要改 `supervisor.py`
- 新版：流程是数据 → 换场景只加目录

旧版验证结果（8/8、4/8）与新版**完全一致**，说明迁移没有改变行为。
控制台已迁到新引擎；`app/` 现在只剩 `selfloop.py` 在用，等它迁完即可删除。

---

## 11. 命名说明

包名从 `platform` 改为 `harness`，原因是 **`platform` 会遮蔽 Python 标准库同名模块**，
任何第三方库 `import platform` 都会拿到我们的包。`harness` 既避免冲突，也正好是赛题用的词。
