# -*- encoding: utf-8 -*-
"""
@File: agents.py
@Modify Time: 2026/1/11 13:44       
@Author: Kevin-Chen
@Descriptions: 需求分析&详细设计 工作流
"""
import os
import re
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from B00_agent_config import (
    ANALYST_NAME,
    format_agent_skills,
    HUMAN_QUESTION_TRIGGER,
    MAX_HUMAN_QA_ROUND,
    REQUIREMENT_CLARIFICATION_MD,
    agent_names_list,
    analysis_agent_init_prompt,
    design_md,
    requirement_str,
    run_agent,
    today_str,
    working_path,
)
from B02_log_tools import Colors, log_message
from B03_init_function_agents import init_agent, parse_director_response

print_lock = threading.Lock()

# [详细设计模式] 各个智能体的 skills 技能标签
agent_skills_dict = {
    '需求分析师': ['$Product Manager'],
    '审核员': ['$System Architect'],
    '测试工程师': ['$Business Analyst'],
    '开发工程师': ['$Developer'],
}


# [详细设计模式] 调度智能体的 prompt 主体
base_director_prompt = f"""你是一个专业的调度智能体.
现在有: {agent_names_list} 这{len(agent_names_list)}个智能体.
当前阶段为 需求分析&详细设计 阶段. 当前的需求描述如下:
```
{requirement_str}
```
你需要先让需求分析师进行需求理解与需求澄清（必要时向人类提问）.
需求澄清完成后, 再由需求分析师编写详细设计文档 {design_md}.
之后再让其他智能体对该文档进行检查, 并结合输出让需求分析师迭代优化.
直到所有智能体都返回检查通过.

---
主要流程如下:

0) 先调度需求分析师进行需求理解与需求澄清.
- 若{ANALYST_NAME}使用触发词 {HUMAN_QUESTION_TRIGGER} 提问, 说明其需要人类澄清.
- 人类回答后, 继续{ANALYST_NAME}完成澄清.
- 澄清完成后再进入后续步骤.
- 其他智能体在审核前必须参考需求澄清记录文件: {REQUIREMENT_CLARIFICATION_MD}

1) 通知 审核员智能体, 测试工程师智能体, 开发工程师智能体 进行文档审核.

1.1) 调用 审核员智能体 时, prompt 模板如下:
```
你是一个专业的代码与架构审核员. 现在有以下需求补充:
{requirement_str}

需求分析师已经根据需求补充, 进行了详细设计, 并且写入了 {design_md} 文档中.
以专业代码与架构审核员的角度, 根据以上需求补充 结合 当前代码, 审查 {design_md} 中的详细设计.
检查是否存在设计错误或者设计遗漏. 如果有疑问或歧义也可以提出.
返回你发现的错误/遗漏点 以及 疑问/歧义点. 如果没有任何错误或疑问, 则返回 '检查通过'.
不要返回多余的信息. 不要修改代码与文档.
```

1.2) 调用 测试工程师 时, prompt 模板如下:
```
你是一个专业的测试工程师. 现在有以下需求补充:
{requirement_str}

需求分析师已经根据需求补充, 进行了详细设计, 并且写入了 {design_md} 文档中.
以专业测试工程师的角度, 根据以上需求补充 结合 当前代码, 审查 {design_md} 中的详细设计.
检查是否存在设计错误或者设计遗漏. 如果有疑问或歧义也可以提出.
返回你发现的错误/遗漏点 以及 疑问/歧义点. 如果没有任何错误或疑问, 则返回 '检查通过'.
不要返回多余的信息. 不要修改代码与文档.
```
    
1.3) 调用 开发工程师 时, prompt 模板如下:
```
你是一个专业的Python开发工程师. 现在有以下需求补充:
{requirement_str}

需求分析师已经根据需求补充, 进行了详细设计, 并且写入了 {design_md} 文档中.
以专业开发工程师的角度, 根据以上需求补充 结合 当前代码, 审查 {design_md} 中的详细设计.
检查是否存在设计错误或者设计遗漏. 如果有疑问或歧义也可以提出.
返回你发现的错误/遗漏点 以及 疑问/歧义点. 如果没有任何错误或疑问, 则返回 '检查通过'.
不要返回多余的信息. 不要修改代码与文档.
```

---
2) 收集 审核员智能体, 测试工程师智能体, 开发工程师智能体 的审核结果.

2.1) 如果所有智能体都没有提出问题或者有疑问点, 则认为 需求分析&详细设计 已经完成, 直接到步骤5, 返回 {{"success": "详细设计完成"}}

2.2) 如果有任一智能体提出错误点或者有疑问点, 则认为该需求详细设计未完成. 需要通知 需求分析师智能体 进行修复. prompt 模板如下:
```
你是一个专业 需求分析师, 针对 {design_md} 文档, 专家团队发现一些错误/遗漏点, 或者存在疑问/歧义点:

审核员发现以下错误/遗漏点:
{{具体内容}}

审核员有以下疑问/歧义点:
{{具体内容}}

测试工程师发现以下错误/遗漏点:
{{具体内容}}

测试工程师有以下疑问/歧义点:
{{具体内容}}

开发工程师发现以下错误/遗漏点:
{{具体内容}}

开发工程师有以下疑问/歧义点:
{{具体内容}}

- 总结并去重上面提到错误/遗漏点, 疑问/歧义点.
- 详细分析这些 错误/遗漏点 是否属实.
- 对这些疑问/分歧点进行深度设计.
- 对属实的问题进行修复. 并且说明对应的假设.
- 修改并补充说明 {design_md} 文档. 

修改完成后, 返回以下内容:
1) 说明属实的问题是哪些
2) 对疑问/分歧点的回答与设计是什么
3) 你对 {design_md} 文档做了哪些修改
注意: 只返回上面3点内容, 不要返回其他无关的内容.
```

---
3) 需求分析师智能体 修改完 详细设计文档, 然后你需要告诉各个其他智能体修改内容是什么, 并且让各个智能体再次检查.

3.1) 调用 审核员智能体 时, prompt 模板如下:
```
你是一个专业的代码与架构审核员. 
刚刚 需求分析师修改了 {design_md} 文档. 其修改内容如下:
{{修改内容}}

需求分析师对于疑问/歧义点的回答与设计如下:
{{回答与设计内容}}

以专业代码与架构审核员的角度, 再次审查 {design_md} 中的详细设计.
结合代码以及原始需求说明: {requirement_str}

检查是否存在设计错误或者设计遗漏. 如果有疑问或歧义也可以提出.
返回你发现的错误/遗漏点 以及 疑问/歧义点. 如果没有任何错误或疑问, 则返回 '检查通过'.
不要返回多余的信息. 不要修改代码与文档.
```

3.2) 调用 测试工程师 时, prompt 模板如下:
```
你是一个专业的测试工程师. 
刚刚 需求分析师修改了 {design_md} 文档. 其修改内容如下:
{{修改内容}}

需求分析师对于疑问/歧义点的回答与设计如下:
{{回答与设计内容}}

以专业测试工程师的角度, 再次审查 {design_md} 中的详细设计.
结合代码以及原始需求说明: {requirement_str}

检查是否存在设计错误或者设计遗漏. 如果有疑问或歧义也可以提出.
返回你发现的错误/遗漏点 以及 疑问/歧义点. 如果没有任何错误或疑问, 则返回 '检查通过'.
不要返回多余的信息. 不要修改代码与文档.
```

3.3) 调用 开发工程师 时, prompt 模板如下:
```
你是一个专业的开发工程师. 现在有以下需求补充:
刚刚 需求分析师修改了 {design_md} 文档. 其修改内容如下:
{{修改内容}}

需求分析师对于疑问/歧义点的回答与设计如下:
{{回答与设计内容}}

以专业开发工程师的角度, 再次审查 {design_md} 中的详细设计.
结合代码以及原始需求说明: {requirement_str}

检查是否存在设计错误或者设计遗漏. 如果有疑问或歧义也可以提出.
返回你发现的错误/遗漏点 以及 疑问/歧义点. 如果没有任何错误或疑问, 则返回 '检查通过'.
不要返回多余的信息. 不要修改代码与文档.
```

---
4) 收集 审核员智能体, 测试工程师智能体, 开发工程师智能体 的审核结果.

4.1) 如果所有智能体都没有提出问题或者有疑问点, 则认为该任务开发完成, 直接到步骤5, 返回 {{"success": "详细设计完成"}}

4.2) 如果有任一智能体提出错误点或者有疑问点, 则认为该需求详细设计未完成. 需要通知 需求分析师智能体 进行修复. 回到 2.2)

---
5) 当所有智能体都认为当前需求分析与详细设计没有问题了, 则结束整个流程. 返回如下JSON:
```
{{"success": "详细设计完成"}}
```

---
6) 如果{ANALYST_NAME}对需求拿不准, 或者对需求有不明白的地方, 需要先询问人类.
{ANALYST_NAME}提问时必须使用触发词: {HUMAN_QUESTION_TRIGGER}
示例:
```
{HUMAN_QUESTION_TRIGGER} 这个测试项目当前优先支持命令行运行还是 HTTP API 运行?
```
要求:
- 问题必须明确、具体, 一次只问一个关键问题.
- 不允许在需求不明确时自行假设并推进.
- 在拿到人类回答后, 继续按照上述流程调度.
- 只有{ANALYST_NAME}可以向人类提问.
- 需要澄清时, 必须通过调度{ANALYST_NAME}来发起提问.

---
返回JSON格式的数据, 格式需要是 {{智能体名称: 提示词}} 格式如下:
{{"需求分析师": "相关提示词..."}}
{{"审核员": "相关提示词...", "测试工程师": "相关提示词...", "开发工程师": "相关提示词...", }}
注意JSON内部双引号需要有转义符, 保证JSON合法

最终回复必须是一个固定字段的 JSON 对象, 且只能输出 JSON 本身, 禁止解释说明:
```json
{{"success":"","需求分析师":"","审核员":"","测试工程师":"","开发工程师":""}}
```
规则:
- 5个字段必须全部出现
- 当前不使用的字段必须填空字符串 ""
- 如果流程结束, 只能填写 success, 其他4个字段必须为空字符串
- 如果要调度智能体, success 必须为空字符串, 其余需要调度的智能体字段填写 prompt, 不调度的智能体字段填空字符串

---
智能体名称必须是: 开发工程师, 需求分析师, 审核员, 测试工程师
返回的格式必须严格满足JSON格式要求.
"""


# [详细设计模式] 与人类交互: 打印问题并读取输入回答
def ask_human(question, log_file_path):
    """
    与人类交互: 打印问题并读取输入回答

    :param question: 需要人类回答的问题
    :param log_file_path: 日志文件路径
    :return: 人类回答
    """
    question = str(question or "").strip()
    if not question:
        raise ValueError("ask_human 问题不能为空")

    with print_lock:
        log_message(
            log_file_path=log_file_path,
            message=f"[ask_human] 问题:\n{question}",
            color=Colors.CYAN,
        )

    print("\n" + "=" * 100)
    print("[需要人类确认] 以下问题请你回答:")
    print(question)
    print("=" * 100)

    try:
        answer = input("请输入你的回答: ").strip()
    except EOFError:
        answer = "人类未提供输入（EOF）。"
    except KeyboardInterrupt:
        answer = "人类中断了输入（KeyboardInterrupt）。"

    if not answer:
        answer = "人类未提供有效回答（空输入）。"

    with print_lock:
        log_message(
            log_file_path=log_file_path,
            message=f"[ask_human] 回答:\n{answer}",
            color=Colors.MAGENTA,
        )
    return answer


def extract_human_question(text):
    """
    从智能体回复中抽取需要人类回答的问题.
    仅识别触发词 {HUMAN_QUESTION_TRIGGER}
    """
    content = str(text or "").strip()
    if not content:
        return None

    matched = re.search(rf"{re.escape(HUMAN_QUESTION_TRIGGER)}\s*(.+)", content, flags=re.S)
    if matched:
        question = matched.group(1).strip()
        if question:
            return question
    return None


def append_clarification_to_requirement_doc(question, human_answer, analyst_reply, log_file_path):
    """
    将问答澄清记录追加到需求说明文档中
    """
    requirement_doc_path = os.path.join(working_path, REQUIREMENT_CLARIFICATION_MD)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    section = f"""
## 需求澄清记录 {timestamp}

- 需求分析师问题: {question}
- 人类回答: {human_answer}
- 需求分析师处理结果:
{analyst_reply}

"""
    with open(requirement_doc_path, "a", encoding="utf-8") as file:
        file.write(section)

    with print_lock:
        log_message(
            log_file_path=log_file_path,
            message=f"[clarification] 已将问答追加到需求说明文档: {requirement_doc_path}",
            color=Colors.GREEN,
        )


def prepare_agent_prompt(agent_name, agent_prompt):
    skills_prefix = format_agent_skills(agent_name, agent_skills_dict)
    if agent_name != ANALYST_NAME:
        clarification_rule = f"""
评审前置规则:
1) 你必须优先阅读并参考需求澄清记录文件: {REQUIREMENT_CLARIFICATION_MD}
2) 文件绝对路径: {working_path}/{REQUIREMENT_CLARIFICATION_MD}
3) 若文件存在, 你的评审结论必须结合澄清记录中的结论;
4) 若文件不存在, 说明“暂无人类澄清记录”, 再基于现有信息继续评审.
"""
        return f"{skills_prefix} {clarification_rule}\n{agent_prompt}".strip()
    analyst_rule = f"""
规则:
1) 只有你可以向人类提问;
2) 你需要人类回答时, 必须使用触发词: {HUMAN_QUESTION_TRIGGER};
3) 人类输入可能是回答, 也可能是反问, 你需要判断是继续提问/解答还是进入下一阶段.
"""
    return f"{skills_prefix} {analyst_rule}\n{agent_prompt}".strip()


def resolve_analyst_question(session_id, agent_log_file_path, initial_msg):
    """
    仅处理需求分析师向人类提问的闭环
    """
    msg = initial_msg
    for _ in range(MAX_HUMAN_QA_ROUND):
        question = extract_human_question(msg)
        if not question:
            return msg

        human_answer = ask_human(f"{ANALYST_NAME} 提问: {question}", agent_log_file_path)
        followup_prompt = f"""
你刚刚使用触发词 {HUMAN_QUESTION_TRIGGER} 向人类提出了以下问题:
{question}

人类输入如下（可能是回答, 也可能是反问）:
{human_answer}

请你先判断该输入属于:
1) 已回答: 则继续推进需求分析/详细设计;
2) 反问: 先解答, 再决定是否继续提问.

如果你仍需要向人类提问, 必须继续使用触发词 {HUMAN_QUESTION_TRIGGER} 开头提出且一次只问一个关键问题.
如果不再需要提问, 请明确写出“需求澄清已完成”.
"""
        msg, _ = run_agent(
            ANALYST_NAME,
            agent_log_file_path,
            followup_prompt,
            init_yn=False,
            session_id=session_id,
        )
        append_clarification_to_requirement_doc(question, human_answer, msg, agent_log_file_path)

    with print_lock:
        log_message(
            log_file_path=agent_log_file_path,
            message=f"[warn] 需求分析师连续提问超过 {MAX_HUMAN_QA_ROUND} 轮, 已停止本轮问答闭环.",
            color=Colors.YELLOW,
        )
    return msg


# [详细设计模式] 需求分析&详细设计 工作流 主函数
def main():
    """
    需求分析&详细设计 工作流 主函数
    :return:
    """
    ''' 1) 初始化 各个功能型智能体 ------------------------------------------------------------------------------------ '''
    agent_session_id_dict = dict()
    # 使用线程池并发初始化多个agent，将agent名称和对应的session ID存储到字典中
    with ThreadPoolExecutor(max_workers=len(agent_names_list)) as executor:
        futures = [executor.submit(init_agent, agent_name) for agent_name in agent_names_list]
        for future in as_completed(futures):
            a_name, s_id = future.result()
            agent_session_id_dict[a_name] = s_id
    # 个性化初始化 需求分析师 智能体
    analyst_prompt = analysis_agent_init_prompt + """
如果你在需求分析或详细设计过程中遇到不明确点、冲突点、信息缺失点:
1) 不要自行猜测;
2) 向人类提问时, 必须使用触发词 [[ASK_HUMAN]];
3) 问题要具体, 一次一个关键问题;
4) 先完成“需求理解与需求澄清”, 澄清完成后再进入详细设计文档编写.
"""
    analyst_log_file_path = f"{working_path}/agent_需求分析师_{today_str}.log"
    analyst_msg, _ = run_agent(
        ANALYST_NAME,
        analyst_log_file_path,
        analyst_prompt,
        init_yn=False,
        session_id=agent_session_id_dict[ANALYST_NAME],
    )
    resolve_analyst_question(
        session_id=agent_session_id_dict[ANALYST_NAME],
        agent_log_file_path=analyst_log_file_path,
        initial_msg=analyst_msg,
    )

    ''' 2) 初始化 调度器智能体 --------------------------------------------------------------------------------------- '''
    director_agent_name = "调度器"
    director_log_file_path = f"{working_path}/agent_{director_agent_name}_{today_str}.log"
    init_director_prompt = f"""
    ---
    现在开始你的调度任务.
    """
    init_director_prompt = base_director_prompt + init_director_prompt
    msg, director_session_id = run_agent(director_agent_name, director_log_file_path,
                                         init_director_prompt, init_yn=True, session_id=None)
    msg_dict = parse_director_response(msg, director_log_file_path)
    first_agent_name = list(msg_dict.keys())[0]

    ''' 3) 调用 各个功能型智能体 ------------------------------------------------------------------------------------- '''
    while first_agent_name != 'success':
        if first_agent_name not in ['需求分析师', '审核员', '测试工程师', '开发工程师']:
            raise ValueError(f"调度器智能体返回了未知的智能体名称: {first_agent_name}")
        # 并发执行智能体
        agent_items = list(msg_dict.items())
        what_agent_replay_dict = dict()
        with ThreadPoolExecutor(max_workers=len(agent_items)) as executor:
            futures = {
                executor.submit(
                    run_agent,
                    agent_name,
                    f"{working_path}/agent_{agent_name}_{today_str}.log",
                    prepare_agent_prompt(agent_name, agent_prompt),
                    False,
                    agent_session_id_dict[agent_name],
                ): agent_name
                for agent_name, agent_prompt in agent_items
            }
            for future in as_completed(futures):
                agent_name = futures[future]
                msg, _ = future.result()
                agent_log_file_path = f"{working_path}/agent_{agent_name}_{today_str}.log"
                if agent_name == ANALYST_NAME:
                    msg = resolve_analyst_question(
                        session_id=agent_session_id_dict[agent_name],
                        agent_log_file_path=agent_log_file_path,
                        initial_msg=msg,
                    )
                elif extract_human_question(msg):
                    msg = (
                        f"违规: 只有 {ANALYST_NAME} 可以使用触发词 {HUMAN_QUESTION_TRIGGER} 向人类提问.\n"
                        f"请由调度器改为调度需求分析师处理此澄清.\n\n原始回复:\n{msg}"
                    )
                what_agent_replay_dict[agent_name] = msg
        what_agent_just_use = [agent_name for agent_name, _ in agent_items]

        ''' 4) 调用 调度器智能体 ------------------------------------------------------------------------------------ '''
        # 合并处理结果, 生成 新的调度器提示词
        what_agent_replay = ''
        for agent_name, msg in what_agent_replay_dict.items():
            what_agent_replay += f"""
    {agent_name}: 
    ```
    {msg}
    ```
            """
        doing_director_prompt = f"""
    ---
    你刚刚调用的智能体为 {what_agent_just_use} 返回内容如下:
    {what_agent_replay}
    ---
    继续按照上述流程进行调度.
        """
        doing_director_prompt = base_director_prompt + doing_director_prompt

        # 调用 调度器智能体
        msg, session_id = run_agent(director_agent_name, director_log_file_path, doing_director_prompt,
                                    init_yn=False, session_id=director_session_id)
        msg_dict = parse_director_response(msg, director_log_file_path)
        first_agent_name = list(msg_dict.keys())[0]


if __name__ == '__main__':
    main()
