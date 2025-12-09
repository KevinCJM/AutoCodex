# -*- encoding: utf-8 -*-
"""
@File: agent_utils.py
@Modify Time: 2025/12/5 14:41
@Author: Kevin-Chen
@Descriptions: 智能体通用工具函数
"""
import os
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from codex_utils import init_codex, resume_codex
from other_utils import CURRENT_DIR, is_valid_json_data, json_key_exists, session_name_json

# 终端颜色配置与按需分配
AGENT_COLORS = {}
COLOR_CYCLE = ["\033[95m", "\033[94m", "\033[96m", "\033[92m", "\033[93m", "\033[91m"]
COLOR_RESET = "\033[0m"


# 将本轮对话打印到终端，按智能体分配不同颜色
def print_dialogue(agent_name, prompt, answer):
    """
    将本轮对话打印到终端，按智能体分配不同颜色。

    参数:
        agent_name (str): 智能体名称
        prompt (Any): 输入提示词
        answer (Any): 智能体回复（列表或字符串）
    """
    if agent_name not in AGENT_COLORS:
        color = COLOR_CYCLE[len(AGENT_COLORS) % len(COLOR_CYCLE)]
        AGENT_COLORS[agent_name] = color
    color = AGENT_COLORS.get(agent_name, "")

    def _to_text(content):
        if is_valid_json_data(content):
            return json.dumps(content, ensure_ascii=False, indent=2)
        return str(content)

    prompt_text = _to_text(prompt)
    if isinstance(answer, list):
        answer_text = "\n".join(_to_text(item) for item in answer)
    else:
        answer_text = _to_text(answer)

    print(f"{color}[{agent_name}] \nprompt:\n{prompt_text}\nreply:\n{answer_text}{COLOR_RESET}\n---\n")


# 保存智能体的对话记录到 Markdown 文件中。每个对话记录包括提示词(prompt)、处理过程(process)和回答(answer)
def save_agent_dialogue(session_folder, agent_name, prompt, process, answer, thread_id, save_process=True):
    """
    保存智能体的对话记录到 Markdown 文件中。每个对话记录包括提示词(prompt)、处理过程(process)和回答(answer)。

    参数:
        session_folder (str): 对话记录存储的文件夹路径。
        agent_name (str): 智能体名称，用于命名对应的 Markdown 文件。
        prompt (Any): 用户输入的提示内容。
        process (Any): 智能体的中间推理或处理过程（可选）。
        answer (Any): 智能体最终的回答内容。
        thread_id (Optional[Any]): 对话线程 ID，用于标识同一会话的不同轮次。
        save_process (bool, optional): 是否保存处理过程，默认为 True。
    返回值:
        无返回值。该函数将对话信息追加写入指定的 Markdown 文件中。
    """

    # 确保 session_folder 目录存在，如果不存在则创建
    os.makedirs(session_folder, exist_ok=True)

    # 构建 Markdown 文件路径，并准备头部信息（包含 thread_id）
    md_path = os.path.join(session_folder, f"{agent_name}.md")
    thread_text = str(thread_id) if thread_id is not None else "未提供"
    header = f"# {agent_name} 对话记录\n- thread_id: {thread_text}\n\n"

    # 如果文件不存在，则新建并写入头部信息
    if not os.path.exists(md_path):
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(header)
    else:
        # 如果文件已存在但前几行中没有 thread_id 标记，则在顶部插入 header
        with open(md_path, "r", encoding="utf-8") as f:
            existing = f.read()
        if "thread_id:" not in "\n".join(existing.splitlines()[:5]):
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(header + existing)

    # 内部辅助函数：将任意类型的内容转换为字符串格式，如果是合法 JSON 数据则美化输出
    def _to_text(content):
        if is_valid_json_data(content):
            return json.dumps(content, ensure_ascii=False, indent=2)
        return str(content)

    # 获取当前时间戳作为对话记录的时间标签
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

    # 构造要写入的 Markdown 块内容列表
    blocks = [
        f"## 记录时间: {timestamp}\n",
        "### prompt\n",
        f"{_to_text(prompt)}\n\n"
    ]

    # 如果需要保存 process，则将其加入 blocks 中
    if save_process:
        blocks.extend([
            "### process\n",
            f"{_to_text(process)}\n\n"
        ])

    # 添加 answer 部分以及分割线
    blocks.extend([
        "### answer\n",
        f"{_to_text(answer)}\n\n",
        "---\n\n"
    ])

    # 将构造好的内容追加写入 Markdown 文件
    with open(md_path, "a", encoding="utf-8") as f:
        f.writelines(blocks)


# 初始化一个对话 session，并创建多个智能体并执行通用初始化提示流程
def start_session(session_name, work_folder, general_prompt_list, agent_name_and_role_dict,
                  model_name="gpt-5.1-codex-mini", reasoning_effort="low", timeout=300):
    """
    初始化一个对话 session，并创建多个智能体并执行初始化及通用提示流程。

    参数:
        session_name (str): 当前会话的名称，用于标识唯一 session。
        work_folder (str): 智能体工作目录路径。
        general_prompt_list (list of str): 所有智能体需要依次执行的通用提示列表。
        agent_name_and_role_dict (dict): key 为智能体名称，value 为其对应的角色描述。
        model_name (str, optional): 使用的语言模型名称，默认为 "gpt-5.1-codex-mini"。
        reasoning_effort (str, optional): 推理强度设置（如："low", "medium", "high"），默认为 "low"。
        timeout (int, optional): 请求超时时间（秒），默认为 300 秒。

    返回:
        bool: 成功完成所有初始化和通用提示后返回 True。

    异常:
        ValueError: 若 session_name 已存在对应的文件夹或 JSON 键，则抛出异常。
    """
    ''' 0. 辅助函数 =============================================================================================== '''

    # 初始化智能体代理
    def _init_agent(agent_name, role):
        """
        初始化智能体代理
        """
        responses, agent_message, t_id = init_codex(role, work_folder, model_name,
                                                    reasoning_effort, timeout)
        print_dialogue(agent_name, role, agent_message)
        save_agent_dialogue(session_folder, agent_name, role, responses,
                            agent_message, t_id)
        return agent_name, t_id, responses, agent_message

    # 执行智能体的通用提示词逻辑
    def _run_general_prompt(agent_name, t_id, g_prompt):
        """
        执行智能体的通用提示词逻辑
        """
        responses, agent_message, t_id = resume_codex(t_id, work_folder, g_prompt,
                                                      model_name, reasoning_effort, timeout)
        print_dialogue(agent_name, g_prompt, agent_message)
        save_agent_dialogue(session_folder, agent_name, g_prompt, responses, agent_message, t_id)
        return agent_name, t_id

    ''' 1. 创建对话记录文件夹与JSON ----------------------------------------------------------------------------------'''
    # 判断 session_name 是否已经存在 (1-判断是否有该文件夹, 2-判断是否有该JSON键)
    session_folder = os.path.join(CURRENT_DIR, session_name)
    if os.path.exists(session_folder) and json_key_exists(CURRENT_DIR, "session_name.json", session_name):
        raise ValueError(f"{session_name} 已经存在. 更改对话名称, 或者手动删除文件夹与JSON键.")
    # 创建 session_folder 文件夹
    os.makedirs(session_folder, exist_ok=False)

    ''' 2. 创建智能体 --------------------------------------------------------------------------------------------- '''
    agent_id_dict = {}
    max_workers = max(1, min(8, len(agent_name_and_role_dict)))
    # 并发初始化所有智能体
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        init_futures = {
            executor.submit(_init_agent, agent_name, role): agent_name
            for agent_name, role in agent_name_and_role_dict.items()
        }
        for future in as_completed(init_futures):
            agent_name, thread_id, _, _ = future.result()
            agent_id_dict[agent_name] = thread_id  # 保存智能体ID

    ''' 3. 将对话以及智能体信息记录到 session_name.json -------------------------------------------------------------- '''
    # 读取 session_name.json 文件, 确保 session_name.json 文件存在
    json_path = session_name_json(CURRENT_DIR, "session_name.json")
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            session_map = json.load(f)  # 读取 session_name.json 文件
            if not isinstance(session_map, dict):
                session_map = {}
    except (FileNotFoundError, json.JSONDecodeError):
        session_map = {}
    # 添加 session_name 和 agent_id_dict 到 session_map 中 & 保存更新后的 session_name.json 文件
    session_map[session_name] = agent_id_dict
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(session_map, f, ensure_ascii=False, indent=2)

    ''' 4. 执行通用提示任务 ----------------------------------------------------------------------------------------- '''
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for general_prompt in general_prompt_list:
            futures = {
                executor.submit(_run_general_prompt, agent_name, thread_id, general_prompt): agent_name
                for agent_name, thread_id in agent_id_dict.items()
            }
            for future in as_completed(futures):
                agent_name, thread_id = future.result()
                agent_id_dict[agent_name] = thread_id
    return True


if __name__ == "__main__":
    w_folder = "/Users/chenjunming/Desktop/KevinGit/GienTechWork/CIB/CIB_wealth_manage"
    general_p_list = [
        """记住:
    1) 使用中文进行对话和文档编写;
    2) 使用 "/Users/chenjunming/Desktop/myenv_312/bin/python3.12" 命令来执行python代码""",
        """深度理解以 local_test_3.py 为入口的代码全链路逻辑. 禁止修改代码.""",
        """我有一个新需求:
* 当前优化器的求解速度太慢, 而且是产品池中的产品越多速度越慢. 我希望加快求解速度.
* 我计划在进行 MIQP 之前先做一次权重的连续求解, 然后拿出 top-k 个产品. 然后将这k个产品来进行 MIQP 求解. 以此来加速. 我知道这可能导致解非最优, 但这是合理的取舍.
* 连续松弛：**快**，不和 MIQP 搞复杂时间切分，只要 “能解 / 解不出来就当没发生”。
* Top‑K：
  * **只在组合推荐场景用**；
  * 连续阶段和 Top‑K 评估的 Universe = **products ∪ userHoldingDict 全集**；
    * 例如在连续松弛模型求解阶段只用了 products 内的产品, 并且top-K选择了 products 内的 A 产品, 但是 MIQP 的时候模型实际投资的是 userHoldingDict 内的 B 产品. 也就是说, MIQP 的产品池其实是不需要将 A 产品放入的.
  * **真裁剪的只有“无持仓新品”**；
  * 同时满足：**大类覆盖 + 单品上限 + 最小产品数 + min_count/max_count**。
    * 例如一个大类目标权重 0.01%. 那么我们就不能把这个产品下的所有产品都剔除掉.
    * 再例如个大类目标权重 50%, 并且该大类下单产品权重不能超过20%, 那么该大类下我们就必须保留至少三只产品.
* 整体流程需要区分 "组合推荐" vs "一键配平" 模式. 由于 "一键配平" 模式下, adjustedHoldingDict 下面的所有产品都要进入最终组合, 因此不做 top-k. 只保留可选的连续 warm‑start.
---
理解我上述需求. 不要修改代码, 不要修改文档.
我基于需求写了详细设计文档 二段优化算法详细设计.md 阅读该文档. 然后分析其是否有逻辑错误或逻辑遗漏.
"""
    ]
    agent_role_dict = {
        "需求分析师": """你是一名专业的 需求分析师（Business Analyst），负责理解业务目标、约束、流程，并输出结构化、可执行的需求文档。

    核心职责
    1. 将用户需求拆分为明确的业务目标、输入输出格式、约束条件、异常场景。
    2. 编写 高质量需求文档（Requirements.md），保证结构化、可测试、可实现。
    3. 输出 验收标准，用于测试工程师自动化验证。

    额外职责：审核算法工程师的代码, 当算法工程师提供新代码时：
    1. 审查代码是否满足需求文档。
    2. 检查逻辑是否遗漏业务规则。
    3. 标记潜在风险、未对齐点、可优化点。
    4. 必要时要求算法工程师补充文档或修改实现。

    文风要求:
    1. 直接、务实、无废话。
    2. 输出结构化 Markdown 文档。
    3. 使用表格归纳输入/输出/约束。
    4. 避免推测需求，严格基于提供的信息。""",
        "算法工程师": """你是一名资深 算法工程师（Algorithm Engineer / Software Engineer），擅长 Python、NumPy、Pandas、Numba。

    核心职责:
    1. 根据需求分析师文档实现算法与代码。
    2. 输出可运行的 Python 代码，结构清晰，可测试，可扩展。
    3. 注重性能、内存占用、数据结构合理性。
    4. 遵守项目代码规范。

    额外职责：审核需求文档
    1. 当需求分析师提交 Requirements 文档时：
    2. 判断需求是否完整、可实现、无矛盾。
    3. 标记模糊部分、不合理约束、潜在性能风险。
    4. 必要时要求需求分析师完善文档。

    文风要求
    1. 输出代码时必须可直接运行。
    2. 解释算法逻辑时必须简洁精准。
    3. 若需求不合理，必须指出并提出方案。""",
        "测试工程师": """你是一名严格的 测试工程师（QA / Test Engineer），熟悉自动化测试、边界分析、黑盒/白盒测试策略。

            核心职责
            1. 根据要求生成 测试计划 TestPlan.md。
            2. 设计覆盖率高的测试用例，包括：
             - 正常流程
             - 边界条件
             - 异常输入
             - 性能压力测试
            3. 输出 自动化可执行的测试代码（pytest / unittest）。

            额外职责：审核需求分析师的文档
            1. 检查需求是否可测试、可验证、有明确验收标准。
            2. 标记未测试的隐性需求、模糊描述。

            文风要求
            1. 测试用例必须编号、结构化。
            2. 测试代码必须可直接运行。
            3. 不允许遗漏关键边界条件。""",
        "代码审核员1": """你是一名 代码审核员（Code Reviewer / Software Architect），负责质量把关。

            核心职责
            1. 审核算法工程师的代码：
             - 逻辑正确性
             - 性能、内存效率
             - 安全性、鲁棒性
             - 可维护性、可扩展性
            2. 给出明确修改建议或确认通过。
            3. 必要时可以提供完整的改进版本。

            额外职责：双向审核
            1. 审核需求分析师的文档
             - 检查是否具备可开发性
             - 检查是否存在歧义、不完整、不一致
             - 标记可能造成实现困难的需求
            2. 审核算法工程师写的最新代码
             - 深入阅读、分析性能、检查边界
             - 发现问题必须逐条指出，可定位到行号/模块
             - 必要时要求重新提交代码或补充验证数据

            文风要求
            1. 直接指出问题，不拐弯抹角
            2. 给出清晰 actionable 改进建议
            3. 严格，不留隐患""",
        "代码审核员2": """你是一名 代码审核员（Code Reviewer / Software Architect），负责质量把关。

            核心职责
            1. 审核算法工程师的代码：
             - 逻辑正确性
             - 性能、内存效率
             - 安全性、鲁棒性
             - 可维护性、可扩展性
            2. 给出明确修改建议或确认通过。
            3. 必要时可以提供完整的改进版本。

            额外职责：双向审核
            1. 审核需求分析师的文档
             - 检查是否具备可开发性
             - 检查是否存在歧义、不完整、不一致
             - 标记可能造成实现困难的需求
            2. 审核算法工程师写的最新代码
             - 深入阅读、分析性能、检查边界
             - 发现问题必须逐条指出，可定位到行号/模块
             - 必要时要求重新提交代码或补充验证数据

            文风要求
            1. 直接指出问题，不拐弯抹角
            2. 给出清晰 actionable 改进建议
            3. 严格，不留隐患""",
    }
    start_session("测试用", w_folder, general_p_list,
                  agent_role_dict)
