# CodingAgent 复利工程（Compound Engineering）学习总结

更新时间：2026-05-04

## 一句话定义

CodingAgent 的复利工程不是“让 AI 多写代码”，而是把每一次开发任务里的经验、约束、偏好、失败和验证方法，沉淀成下一次 Agent 可读取、可复用、可执行的工程资产。

核心判断：每个 feature、bugfix、review、plan revision 结束时，都应该让下一次任务更容易，而不是让系统更复杂。

## 背景

传统软件工程里，功能越多，代码库通常越难改：上下文更多、隐含规则更多、历史决策更多、回归风险更高。CodingAgent 让“写代码”本身变快后，新的瓶颈变成：

- Agent 是否理解当前代码库的真实约束；
- Agent 是否会重复上一次犯过的错；
- 人的审美、偏好和架构判断是否只留在脑子里；
- 测试、文档、规则、hooks 是否能让 Agent 自己验证结果。

复利工程要解决的就是这些问题：把一次任务的学习转成系统资产，让未来任务继承这些学习。

## 主循环

典型闭环是：

```text
Plan -> Work -> Review -> Compound -> Repeat
```

### 1. Plan

先把意图变成可审查的计划。

要做的事：

- 理解需求：要做什么、为什么做、约束是什么；
- 研究代码库：类似功能在哪里、当前模式是什么；
- 研究外部资料：框架文档、最佳实践、已知坑；
- 设计方案：影响哪些文件、怎么改、怎么验证；
- 验证计划：是否覆盖边界条件和回归风险。

关键变化：计划文档变成重要产物。写代码前先固化判断，修计划比修代码便宜。

### 2. Work

Agent 按计划执行。

常见做法：

- 使用 branch 或 worktree 隔离任务；
- 按计划逐步实现；
- 每个关键步骤后跑测试、lint、typecheck；
- 记录已完成和未完成事项；
- 遇到失败时回到计划，更新路径。

如果计划足够可靠，人不需要盯每一行代码，而是监控进度和最终结果。

### 3. Review

Review 不只是找 bug，更是提取下一轮可复用的学习。

可使用多个专门 reviewer：

- 安全 reviewer；
- 性能 reviewer；
- 架构 reviewer；
- 数据完整性 reviewer；
- 前端交互 reviewer；
- 简洁性和可维护性 reviewer；
- agent-native reviewer。

Review 输出应分级：

- P1：必须修；
- P2：应该修；
- P3：可选优化。

更重要的是：每个重复性问题都要问一句，“下次怎么自动避免？”

### 4. Compound

这是复利工程和普通 AI 辅助开发的分界线。

传统开发通常在 Plan、Work、Review 后结束；复利工程还要把结果写回系统。

可沉淀的问题：

- 这次真正解决了什么；
- 哪个方案有效；
- 哪些尝试失败了；
- 这类问题下次如何更快识别；
- 哪条规则、测试、hook、skill、文档应该被更新；
- 这次 review 暴露了哪类可自动化检查。

Compound 的目标不是多写总结，而是让下次 Agent 真能检索、理解、执行这些经验。

## 什么东西会产生复利

常见可复利资产：

| 资产 | 作用 |
| --- | --- |
| `AGENTS.md` / `CLAUDE.md` | 项目级 Agent 指令、约束、工作流、偏好 |
| `docs/solutions/` | 已解决问题的可检索案例库 |
| plan 文件 | 保留意图、设计判断、边界条件 |
| tests / evals | 把经验变成可执行验证 |
| hooks | 把必须遵守的规则变成自动拦截 |
| skills | 把可重复流程封装成可调用能力 |
| subagents / review agents | 把专家判断变成可并行审查能力 |
| changelog / TODO | 让历史和后续工作不丢失 |

判断标准：如果一个经验只存在于聊天记录或人脑里，它不会稳定复利；如果它被写成 Agent 会读、会用、会验证的资产，它才可能复利。

## 在线学习和结束后学习

复利工程里的学习可以分成两种形式：在线学习和结束后学习。

### 在线学习（Online Learning / Hot Path Learning）

在线学习发生在任务执行过程中。

典型例子：

- 用户纠正 Agent 的实现偏好，Agent 立即更新记忆或当前计划；
- 测试失败后，Agent 立刻记录“这个模块必须先 mock 某个外部依赖”；
- Review 中发现某类风险，Agent 当场补充 checklist；
- Agent 执行中发现计划遗漏，马上修改计划并继续；
- 当前 session 直接把新规则写入 memory、`AGENTS.md` 或任务笔记。

优点：

- 立即生效；
- 当前任务后半段就能受益；
- 反馈链路短，适合明确、低争议的偏好或事实。

缺点：

- 增加当前任务延迟；
- Agent 需要一边做事一边整理记忆，容易分心；
- 未充分验证的经验可能被过早固化；
- 存在 memory poisoning 风险，外部内容或错误结论可能污染长期记忆。

适合在线学习的内容：

- 明确的用户偏好；
- 已被测试确认的项目事实；
- 当前任务必须遵守的临时约束；
- 小而确定的工作流修正。

### 结束后学习（Post-task Learning / Background Consolidation）

结束后学习发生在任务完成、PR 合并、bug 修复或一次 session 结束之后。

典型例子：

- 任务完成后生成 `docs/solutions/xxx.md`；
- 从 PR review 中提取通用规则；
- 把多个任务中反复出现的问题合并成一个 hook；
- 把一次成功流程沉淀成 skill；
- 定期由 consolidation agent 汇总近期对话和任务记录，更新长期记忆；
- 人审核后再把经验写入共享知识库。

优点：

- 不阻塞当前任务；
- 更适合做归纳和去重；
- 可以跨多个任务识别稳定模式；
- 通过人工审核降低错误经验污染长期记忆的风险。

缺点：

- 当前任务用不上，只能帮助下一次；
- 如果没有固定流程，很容易被跳过；
- 总结太泛或不可检索时，后续 Agent 实际用不到。

适合结束后学习的内容：

- 架构决策；
- 复杂 bug 的根因和防复发策略；
- 反复出现的 review 规则；
- 可复用工具、skill、agent；
- 长期项目规范和跨任务模式。

## 两种学习方式的差异

| 维度 | 在线学习 | 结束后学习 |
| --- | --- | --- |
| 发生时间 | 任务执行中 | 任务完成后或后台周期性执行 |
| 生效速度 | 立即生效 | 下一次任务生效 |
| 主要目标 | 快速适配当前反馈 | 提炼稳定经验 |
| 适合内容 | 明确偏好、即时约束、小修正 | 架构模式、复盘、长期规则 |
| 风险 | 过早固化、污染记忆、增加延迟 | 被跳过、总结不可用、反馈滞后 |
| 最佳控制 | 小范围写入、可回滚 | 人审、去重、分类、可检索 |

最佳实践不是二选一，而是组合使用：

- 在线学习负责“当前任务立刻变聪明”；
- 结束后学习负责“系统长期变聪明”。

## 实践模板

每次任务结束前，问下面几个问题：

```text
1. 本次任务解决了什么问题？
2. 哪个方案最终有效？
3. 哪些尝试失败了，为什么？
4. 下次遇到类似问题，Agent 应该先读什么？
5. 哪条规则应该写入 AGENTS.md / CLAUDE.md？
6. 哪个检查应该变成测试、hook 或 reviewer？
7. 是否需要新增 docs/solutions、skill、subagent？
8. 这个经验是否已经足够稳定，值得进入长期记忆？
```

可落地的最小版本：

```text
Plan: 写一个明确计划
Work: Agent 执行并跑验证
Review: 人和 Agent 检查结果
Compound: 至少沉淀一项资产
```

“至少沉淀一项资产”可以是：

- 一个测试；
- 一条 `AGENTS.md` 规则；
- 一篇 solution note；
- 一个 checklist 项；
- 一个 hook；
- 一个 skill；
- 一个可复用 prompt。

## 风险和反模式

### 反模式 1：只追求当次速度

表现：每次都让 Agent 快速写代码，但不更新规则、测试、文档和技能。

结果：每次 session 都像从零开始，只有线性收益，没有复利。

### 反模式 2：把聊天记录当长期记忆

聊天记录不稳定、不可检索、不可执行。真正的长期资产应该进入明确文件、测试、hook、skill 或数据库。

### 反模式 3：过早固化错误经验

一次失败不一定代表通用规则。进入长期记忆前应确认：

- 是否被测试验证；
- 是否适用于同类任务；
- 是否和现有规则冲突；
- 是否需要人审。

### 反模式 4：记忆膨胀

`AGENTS.md`、`CLAUDE.md` 或 memory 文件过长后，Agent 可能读不准重点。

处理方式：

- 拆分为分层文件；
- 保持规则短、明确、可执行；
- 定期删除过期规则；
- 把详细案例放到 solution docs，入口文件只保留索引和关键规则。

### 反模式 5：把人工 review 当唯一安全网

复利工程不是取消 review，而是把高频 review 判断变成自动化检查。

如果每次都靠人眼发现同一个问题，说明这个问题应该进入测试、hook、linter、review agent 或 checklist。

## 对 CodingAgent 的关键启发

1. 代码不是唯一产物，能持续产出好代码的系统更重要。
2. Agent 的上下文不是天生稳定的，必须通过文件、记忆、技能和测试来外化。
3. 人的职责从“逐行写代码”转向“定义意图、审查方向、沉淀判断、建设安全网”。
4. Plan 和 Review 的价值上升，纯执行的价值下降。
5. 每次任务都应该交付两类结果：功能结果和系统改进结果。

## 推荐落地路径

### 阶段 1：先协作

- 选定一个主要 CodingAgent 工具；
- 先让 Agent 解释代码，不急着改；
- 从测试、配置、样板代码等低风险任务开始；
- 记录有效 prompt。

### 阶段 2：让 Agent 进入代码库

- 允许 Agent 读写文件；
- 从单文件、小范围修改开始；
- 人逐步 review diff；
- 建立 `AGENTS.md` 或同类项目规则文件。

### 阶段 3：计划优先

- 先写 plan；
- 人审核 plan；
- Agent 按 plan 执行；
- 人从逐行 review 转向 PR 级 review；
- 每次结束后记录 plan 漏洞。

### 阶段 4：从目标到 PR

- 人描述结果，不手写详细步骤；
- Agent 自己研究、计划、实现、测试、自审、提交 PR；
- 人重点审查最终方向和风险；
- 把成功的目标描述沉淀成模板。

### 阶段 5：并行化

- 多个 Agent 并行处理不同任务；
- 使用队列管理任务；
- 用 review agents 和测试系统控制风险；
- 周期性做知识整理和 memory garbage collection。

## 最终结论

复利工程的目标不是“Claude/Codex/Cursor 帮我写代码”，而是“我的工程系统持续教会 CodingAgent 如何在这个代码库里更好地写代码”。

真正产生复利的不是一次生成了多少代码，而是每次任务结束后，系统是否多了一点可复用、可检索、可验证的工程知识。

## 资料来源

- Every 官方指南：<https://every.to/guides/compound-engineering>
- Every 文章：<https://every.to/chain-of-thought/compound-engineering-how-every-codes-with-agents>
- Agentic Coding Patterns：<https://aipatternbook.com/compound-engineering>
- Point North 总结：<https://www.pointnorth.no/writing/compound-engineering/>
- AGENTS.md 官方仓库：<https://github.com/agentsmd/agents.md>
- LangChain Deep Agents Memory：<https://docs.langchain.com/oss/python/deepagents/memory>
- VS Code Agents 概念文档：<https://github.com/microsoft/vscode-docs/blob/main/docs/copilot/concepts/agents.md>
