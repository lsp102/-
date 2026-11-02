# FallGuard Harness

**面向多智能体协作的编排、调度、执行与验证平台。**

平台不认识任何业务：Agent 团队、工作流、工具、知识库全部以 **scenario pack** 形式接入。
目前挂了两个互不相干的 pack 来证明这一点——换领域不改引擎一行。

2026 第五届未来杯创新大赛 · 赛道一「Agent 编排创新应用」

---

## 快速开始

```bash
python -m harness.server 8000        # 控制台 http://127.0.0.1:8000
python -m harness.cli list
python -m harness.cli run fall-response s3
python -m harness.cli validate fall-response
python -m harness.cli validate fall-response --workflow single
python -m harness.cli validate research-brief
```

零第三方依赖，Python 3.10+。

---

## 现在有什么

| 能力 | 状态 | 位置 |
|---|---|---|
| 工作流引擎（10 种节点原语） | ✅ | `harness/engine.py` |
| 受限表达式语言（AST 白名单） | ✅ | `harness/expr.py` |
| Agent 注册表 + 权限 / 工具白名单 | ✅ | `packs/*/agents.json` |
| 知识库注册中心（规则 / 文档 / 经验） | ✅ | `harness/knowledge.py` |
| 执行轨迹（可断言、可回放） | ✅ | `harness/trace.py` |
| 验证套件（场景 + 断言） | ✅ | `harness/cli.py validate` |
| 工作流图自动布局 | ✅ | `harness/graph.py` |
| HTTP 控制台（工作流图 + 实时轨迹） | ✅ | `harness/server.py` + `web/` |
| Self-Loop 参数调优 | ⚠️ 旧版可用，待迁移 | `app/selfloop.py` |

---

## 验证结果（刚跑过）

| pack | 工作流 | 通过 |
|---|---|---|
| fall-response | `response`（多智能体） | **8 / 8** |
| fall-response | `single`（单 Agent 对照） | 4 / 8 |
| research-brief | `brief`（并行拓扑） | **2 / 2** |

对照组通过的恰好是**不需要编排的四件事**（普通跌倒、迟撤销、断网重试、通道降级）；
失败的恰好是**编排存在的理由**（分级、禁忌否决、撤销窗口、事件去重）。

---

## 控制台

```bash
python -m harness.server 8000
```

- 左侧：pack / 场景列表，点一下就跑
- 中上：**工作流图**，执行到哪个节点哪个节点发光（`veto` 亮紫、`error` 亮红）
- 中下：**实时轨迹**，每步的节点 / 类型 / 角色 / 状态 / 摘要
- 底部条：结局、仿真耗时、步数、断言结果

「演示节奏」会在节点之间插入 380ms 停顿，只为让评委看清编排过程，**不影响仿真耗时指标**。

---

## 目录

```
fall-guard/
├─ harness/             平台（与业务无关）
│  ├─ expr.py           受限表达式求值
│  ├─ trace.py          执行轨迹
│  ├─ knowledge.py      知识源注册中心
│  ├─ pack.py           pack 加载
│  ├─ engine.py         工作流引擎 + 断言
│  ├─ graph.py          工作流图布局
│  ├─ cli.py            命令行
│  └─ server.py         HTTP 控制台
├─ packs/
│  ├─ fall-response/    串行 + 人工窗口 + 升级链
│  └─ research-brief/   并行扇出 + 汇合 + 校验回退
├─ web/index.html       控制台页面（工作流图 + 实时轨迹）
├─ PLATFORM.md          平台规范（节点原语 / pack 格式 / 断言语法）
├─ app/                 旧版实现（selfloop 仍在用，其余已停用）
└─ experiment.py        旧版实验脚本
```

---

## 一分钟看懂差别

旧版 `app/supervisor.py` 里，流程是一段 Python：

```python
triage = ...
if cancelled: return
push(); for c in contacts: call(c)
```

新版 `packs/fall-response/workflows/response.json` 里，流程是一张图：

```json
"call_loop": {
  "type": "foreach",
  "over": "ctx.triage.contacts",
  "body": "call_one",
  "break_when": "ctx.call.answered",
  "on_break": "end_answered",
  "on_exhausted": "page_community"
}
```

**换场景 = 换目录，不是改代码。**

---

## 已知限制

1. 两个 pack 的业务逻辑都是 mock handler，未接真实 LLM / 真实外呼
2. 响应时延是离散事件仿真，不是真实计时
3. 单 Agent 基线是行为模拟，正式材料需用真实 API 重跑
4. 同一 Engine 实例不保证多线程并发跑多个场景
5. 知识检索是字面匹配，未做语义召回
6. 工作流图布局是简单分层，交叉边较多的图会略显拥挤

---

## 下一步

1. Self-Loop 迁到新引擎，调优目标从"振铃等待"泛化为"任意 pack 声明的参数"
2. 接真实 LLM，重跑对照实验
3. 第三个 pack：知识密集型任务，验证 `knowledge` + `foreach` 组合
4. 删除旧版 `app/` 与 `experiment.py`
