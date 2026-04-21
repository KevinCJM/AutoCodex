# -*- encoding: utf-8 -*-
"""
@File: Prompt_07_Development.py
@Modify Time: 2026/4/10 17:42       
@Author: Kevin-Chen
@Descriptions: 复核阶段提示词
"""

from T04_common_prompt import task_start_prompt, state_machine_output
from pathlib import Path


# 全部任务开发完成后, 让 [开发工程师] 和各个 [审核员] 重新全面评估代码
def review_all_code(agent_role, hitl_record_md='name_人机交互澄清记录.md',
                    requirements_clear_md='name_需求澄清.md',
                    detailed_design_md='name_详细设计.md',
                    task_split_md='name_任务单.md',
                    review_md='name_代码评审记录_AgentName.md'):
    review_all_code_prompt = f"""## 角色定位
{agent_role}

## 背景
已经完成所有任务的代码开发，现在需要你重新完成以下三个维度的【逻辑锚定】：
1. **业务准绳 (Business Grounding)**：深度解析《{requirements_clear_md}》与《{hitl_record_md}》，锁定核心业务规则及人类最新的决策修正。
2. **架构约束 (Architectural Constraints)**：以《{detailed_design_md}》为拓扑结构，确保所有逻辑不偏离既定设计模式与系统分层。
3. **任务边界 (Task Scope)**：根据《{task_split_md}》确认当前任务的原子操作范围，防止过度审计或范围蔓延。

## 任务
- 对本次需要的代码改动做全面回归评审

## 审计准则
- **业务闭环**：是否完美覆盖需求？是否存在计算精度或边界溢出风险？
- **契约对齐**：是否破坏了既有的 API 签名、数据库 Schema 或系统层级依赖？
- **冗余控制**：是否严格遵循“最小改动原则”？禁止任何非任务相关的代码注入。
- **回归风险**：识别对现有稳定逻辑的副作用（Side Effects）。
- **四问约束**: 禁止为了最求代码简洁优雅而修改代码,禁止为了解决原代码技术债而修改代码,禁止主动修复自以为的BUG,回答四问：
    1. 是否属于当前需求？如果不是，不改。
    2. 不改是否阻塞？如果不会阻塞，不改。
    3. 是否可以最小改造？避免全面重构，而是只解决当前障碍，只做最小化的改动。
    4. 是否影响原有契约？禁止影响原有契约，包括 API、配置、日志、数据结构、下游依赖、用户工作流、兼容等等逻辑。

## 交互协议 (State Machine Output)

### 场景 A：审核通过 (Pass)
**触发条件**：代码逻辑无瑕疵，完全对齐需求与设计, 代码修改符合四问逻辑, 遵循最小改动原则。
**操作流水线**：
1. **清理报告**：创建/清空本地文件《{review_md}》（使其为空文件）。
2. **反馈**：**仅**返回字符串 `审核通过`。**禁止**返回其他文字内容。

### 场景 B：存在瑕疵 (Blocked)
**触发条件**：发现逻辑错误、需求遗漏、架构分歧、不符合四问逻辑、或其他潜在隐患。
**操作流水线**：
1. **审计定格**：将审计发现覆盖写入本地文件《{review_md}》(完全覆盖旧内容)。
   - **格式**：Bullet Point；高信息密度；AI机器友好。
   - **标签**：明确区分 `[Error]` (错误/遗漏) 与 `[Ambiguity]` (疑问/歧义)。
   - **内容**：指明具体问题。
2. **反馈**：**仅**返回字符串 `未通过`。**禁止**返回其他文字内容。

## 行为禁令
- **只读模式**：严禁修改除了《{review_md}》以外的任务文档、源代码、或配置。
- **输出静默**：禁止输出任何分析过程、解释说明。只能输出指定字符串。
    """
    return review_all_code_prompt
