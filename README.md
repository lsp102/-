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
python -m harness.cli audit fall-response     # 死规则 / 不可达节点 / 权限缺声明
python -m harness.cli dump fall-response s3   # 轨迹导出（Markdown，加 --json 出 JSON）
python -m harness.cli learn fall-response --title "结论" --text "证据与理由"   # 经验回写
python -m unittest discover -s tests          # 51 个回归用例
powershell -File tools/shot.ps1               # 无头 Edge 重拍控制台截图到 shots/
```

零第三方依赖，Python 3.10+。

---

## 现在有什么

| 能力 | 状态 | 位置 |
|---|---|---|
| 工作流引擎（11 种节点原语） | ✅ | `harness/engine.py` |
| 受限表达式语言（AST 白名单） | ✅ | `harness/expr.py` |
| Agent 注册表 + 权限 / 工具白名单 | ✅ 权限由 handler 主动声明 | `packs/*/agents.json` |
| 知识库注册中心（规则 / 文档 / 经验） | ✅ | `harness/knowledge.py` |
| 执行轨迹（可断言、可回放） | ✅ | `harness/trace.py` |
| 验证套件（场景 + 断言） | ✅ | `harness/cli.py validate` |
| 静态体检（死规则 / 不可达节点 / 权限缺声明） | ✅ | `harness/cli.py audit` |
| 轨迹导出（Markdown / JSON） | ✅ | `harness/cli.py dump` |
| 经验回写（复盘结论 → 下次可检索） | ✅ | `harness/lessons.py` + `data/cases.json` |
| 持久记忆（memory 节点，**只持久化声明的键**） | ✅ | `harness/memory.py` + `.harness_state/` |
| 跌倒核验（多信号评分 + 分级升级） | ✅ | `data/fall_model.json` + `handlers.py: verify` |
| 工作流图自动布局 | ✅ | `harness/graph.py` |
| HTTP 控制台（工作流图 + 实时轨迹 + 评分卡） | ✅ | `harness/server.py` + `web/` |
| CI（3.10 / 3.12 × 测试 + 场景 + 静态体检） | ✅ | `.github/workflows/ci.yml` |
| Self-Loop 参数调优 | ⚠️ 旧版可用，待迁移 | `app/selfloop.py` |

---

## 验证结果（刚跑过）

| pack | 工作流 | 通过 |
|---|---|---|
| fall-response | `response`（多智能体） | **13 / 13** |
| fall-response | `single`（单 Agent 对照） | 4 / 13 |
| research-brief | `brief`（并行拓扑） | **2 / 2** |

对照组通过的恰好是**不需要编排的四件事**（普通跌倒、迟撤销、断网重试、通道降级）；
失败的恰好是**编排存在的理由**（分级、禁忌否决、撤销窗口、事件去重、夜间上调、
推送失败降级、误报否决、语音确认、多信号定性）。

`s9`（夜间跌倒）与 `s10`（推送彻底失败）覆盖了以前永远走不到的 `R-NIGHT`
与 `gate_push` 失败分支；`s11`～`s13` 是跌倒核验新增的三条：
手机掉落被硬否决、疑似跌倒走语音确认后撤销、多信号确认后按最短振铃升级。

`audit` 会指出仍然没有场景覆盖的规则（当前是 `R-AGE_80`、`R-NO_FAMILY`
——档案里没有 80 岁以上或没有直系联系人的人）。

---

## 判定与时效

**判定**（`data/fall_model.json`，全部权重/阈值可调）：

| 信号 | 权重 | 归一化基准 |
|---|---|---|
| 冲击峰值 `impact_g` | 0.28 | 6 g |
| 失重时长 `freefall_ms` | 0.22 | 300 ms |
| 跌倒后静止 `still_ms` | 0.20 | 5 s |
| 姿态变化 `orientation_delta_deg` | 0.10 | 60° |
| 端侧置信度 `confidence` | 0.20 | 1.0 |

分数落到四档 `confirmed / likely / suspected / negligible`，并被三类规则修正：

- **硬否决**：手机掉落（有冲击、无失重、几乎无静止）、坐下（轻冲击、小姿态变化、短静止）→ 直接 `negligible`，不打扰任何人；
- **安全偏置**：服用抗凝/抗血小板药的老人，`likely`/`suspected` 一律上提为 `confirmed`；
- **近期误报**：同人近期误报 ≥2 次，总分 −0.05。

只有 `suspected` 或置信度低于 0.80 才会触发 **3 秒语音确认**（老人按键即可撤销），
其余等级直接进入派发。

**时效**：振铃时长与升级总预算由判定等级决定，不再所有事件都等满 30 s。

| 等级 | 单次振铃 | 升级总预算 |
|---|---|---|
| confirmed | 13 s | 20 s |
| likely | 15 s | 22 s |
| suspected | 18 s | 25 s |

全部无人接听的场景从 **60.3 s → 20.3 s**（3×），接听路径不变（8.3 s）。

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
│  ├─ pack.py           pack 加载（mtime 缓存）
│  ├─ engine.py         工作流引擎 + 断言
│  ├─ runner.py         场景执行器（CLI / 服务端 / 体检共用）
│  ├─ audit.py          静态体检
│  ├─ memory.py         跨运行持久记忆
│  ├─ lessons.py        经验回写
│  ├─ graph.py          工作流图布局
│  ├─ cli.py            命令行
│  └─ server.py         HTTP 控制台
├─ packs/
│  ├─ fall-response/    串行 + 人工窗口 + 升级链 + 跌倒核验
│  └─ research-brief/   并行扇出 + 汇合 + 校验回退
├─ tests/               51 个回归用例
├─ web/index.html       控制台页面（工作流图 + 实时轨迹 + 评分卡）
├─ pyproject.toml       版本 / Python 版本要求的单一事实源
├─ .github/workflows/   CI
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
  "break_when": "ctx.call.answered or ctx.call.budget_hit",
  "on_break": "gate_call",
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
7. 跌倒核验的传感器特征（冲击 g 值 / 失重时长 / 静止时长 / 姿态变化）由场景注入，
   未接真实 IMU；权重与阈值是工程先验，未经现场数据标定

---

## 下一步

1. Self-Loop 迁到新引擎，调优目标从"振铃等待"泛化为"任意 pack 声明的参数"
2. 接真实 LLM，重跑对照实验
3. 第三个 pack：知识密集型任务，验证 `knowledge` + `foreach` 组合
4. 删除旧版 `app/` 与 `experiment.py`
5. 用真实 IMU 数据标定 `fall_model.json` 的权重与阈值，并把误报率作为回归指标
