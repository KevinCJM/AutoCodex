1. **AGENT初始化阶段** 
   - 指定工作的 **项目目录**
   - 由 [人类] 指定, 是否需要执行 AGENTS init 程序. 该程序用于生成项目路由层.
     - 指定执行位置, 可以在多个文件夹下执行, 并行执行
       - 例如: 在 canopy-api-v3/canopy-api-v3 下执行, 在 canopy-api-v3/canopy_api_v3/core/calculation 下执行
       - 由 [路由层规划] 智能体负责, [路由层规划] 智能体由系统创建, 使用YOLO模式.
       - 使用提示词 
         - `Prompt_01_RoutingLayerPlanning.create_routing_layer_file`
         - `Prompt_01_RoutingLayerPlanning.routing_layer_file_audit`
         - `Prompt_01_RoutingLayerPlanning.routing_layer_refine`
   - 如果启动时候, 在  **项目目录** 没有查到以下文件, 则强制在 **项目目录** 执行一次.
     - AGENTS.md
     - docs/ARCHITECTURE.md
     - docs/MODULE_INDEX.md
     - docs/TASK_ROUTING.md
     - docs/COMMON_PITFALLS.md
     - docs/repo_map.json

2. **需求录入阶段**
   - 如果 `{需求名}_原始需求.md` 不存在, 强制要跑需求录入阶段
   - 需求录入
     - 要求用户输入需求名称
     - 通过 文档/文字输入/Notion链接 等方式, 将原始需求写入 `{需求名}_原始需求.md`
       - 如果是 Notion 则启动一个 codex, GPT-5.4-Mini, high 临时智能体用 $notion-api-token-ops 这个技能读取需求. 提示词用 `Prompt_02_RequirementIntake.get_notion_requirement`

3. **需求澄清阶段**
   - 如果 `{需求名}_需求澄清.md` 不存在, 强制要跑需求澄清阶段
   - 需求澄清:
     1. [需求分析师] 智能体通过了解代码, 以及需求说明, 分析需求. 使用提示词 `Prompt_03_RequirementsClarification.requirements_understand`
     2. 如果 [需求分析师] 智能体认为信息不足以澄清需求, 则要求HITL人类提供更多信息. 将提问写入`{需求名}_与人类交流.md`, 由 [系统] 提交给人类
        2.1 将人类的答复返回给 [需求分析师] 智能体. 使用提示词 `Prompt_03_RequirementsClarification.hitl_bck`
        2.2 [需求分析师] 智能体基于新信息再次分析需求, 将人类反馈的信息写入`{需求名}人机交互澄清记录.md`. 判断信息是是否仍然缺失. 循环直到 [需求分析师] 认为信息完整.
     3. 如果 [需求分析师] 智能体认为信息足够, 则写入 `{需求名}_需求澄清.md`

4. **需求评审阶段**
     - 人类审核 `需求澄清.md`
      1. 如果审核通过, 则进入 需求评审阶段
         - [需求分析师] 智能体延用澄清阶段的智能体, 如果是跳入到本阶段(未经历)则需要resume重建
      2. 如果审核未通过, 则人类发出提问. 使用提示词 `Prompt_04_RequirementsReview.human_feed_bck` 如果是 resume 的, 则用 `Prompt_04_RequirementsReview.resume_ba`
        - [需求分析师] 智能体基于人类反馈, 修改 `{需求名}_需求澄清.md` 或者 向人类回复解释 (回到澄清阶段)
      3. 重复, 直到人类审核通过, 进入 智能体需求评审阶段
    - 审核智能体审核:
      1. 启动N个 [审核器] 智能体, 根据 `{需求名}_原始需求.md` + `{需求名}_人机交互澄清记录.md` 评审 `{需求名}_需求澄清.md` 文档, 每个 [审核器] 要有名称, 并行执行. 使用提示词 `Prompt_04_RequirementsReview.requirements_review_init`
         - [审核器] 智能体的人格定义是系统指定的, YOLO模式. 人类只能指定数量N,以及各个厂商,模型,推理强度,和代理端口.
         - [审核器] 智能体的评审意见写入 `{需求名}_需求评审记录_{名称}.md` 和 `{需求名}_评审记录_{名称}.md`
         - 检查各个 [审核器] 有没有按提示词要求更新 `{需求名}_需求评审记录_{名称}.md` 和 `{需求名}_评审记录_{名称}.json`, 如果没有则触发相应提示词. 用 `check_reviewer_job` 函数
         - 判断是否所有评审都通过: 1)合并所有 `{需求名}_需求评审记录_{名称}.md`, 2) 判断总md `{需求名}_需求评审记录.md`是否为空, 3)判断所有 `{需求名}_评审记录_{名称}.json` 是否true. 用 `task_done` 函数
      2. 如果所有 [审核器] 智能体都判断评审通过, 则在 `{需求名}_开发前期.json` 标记 "需求评审" 为 true, 并且进入 "详细设计" 阶段
      3. 评审若未通过, 则将审核结果交予 [需求分析师] 优化和修改. 使用提示词 `Prompt_04_RequirementsReview.review_feedback`
         3.1 如果有 [需求分析师] 认为无法决定的, 或者信息不足时, 触发HITL. 流程回到 需求澄清阶段
         3.2 如果信息足够, [需求分析师] 回复 [审核器] 智能体再次审核. 使用提示词 `Prompt_04_RequirementsReview.requirements_review_reply`
         3.3 重复, 直到所有 [审核器] 智能体判断审核通过, 然后可以进入 详细设计阶段

5. **详细设计阶段** 
   - 进入详细设计阶段前, 必须确保 `{需求名}_需求澄清.md` 存在. 否则要先需求澄清阶段
   - 如果 `{需求名}_详细设计.md` 不存在, 强制要跑详细设计阶段
   - 重建 [需求分析师] 或 延用需求评审阶段的 [需求分析师] 来生成详细设计文档
     - 如果是直接跳入到本阶段 (未执行需求评审), 则强制创建 [需求分析师], 然后先使用提示词 `Prompt_05_DetailedDesign.create_detailed_design_ba` 初始化, 再用提示词 `Prompt_05_DetailedDesign.detailed_design` 做详细设计生成 `{需求名}_详细设计.md`
     - 如果是执行完需求评审后, 进入到本阶段的, 则让人类选择是否重建 [需求分析师]
       - 如果重建 [需求分析师] 智能体, 则关闭原本的 [需求分析师] 智能体, 然后创建新的 [需求分析师] 智能体. 然后先使用提示词 `Prompt_05_DetailedDesign.create_detailed_design_ba` 初始化, 再用提示词 `Prompt_05_DetailedDesign.detailed_design` 做详细设计生成 `{需求名}_详细设计.md`
       - 如果延用需求评审的 [需求分析师] 智能体, 则直接用提示词 `Prompt_05_DetailedDesign.detailed_design` 做详细设计生成 `{需求名}_详细设计.md`
   - 设置各个不同的评审角色, 基于需求与澄清文档评审 `{需求名}_详细设计.md`, 并行执行.
     - 人类可以设置 <角色定位> 的智能体: 开发工程师, 测试工程师, 审核员, 等等 (有默认值). 并且可以给不同的 <角色定位> 设置角色定位提示词 (有默认值). 智能体名称仍然使用 "{角色定位}-{角色名称}" 的逻辑
     - 并行让各个智能体审核`{需求名}_详细设计.md`文档, 用提示词 `Prompt_05_DetailedDesign.review_detailed_design`
     - 各个智能体将评审结果写入各自的 `{需求名}_详设评审记录_{智能体名称}.md` 中, 再统一合并到 `{需求名}_详设评审记录.md` 中
       - 用 check_reviewer_job 函数检查智能体有没有按提示词要求更新, 如果没有则触发提醒提示词, 要求智能体更新文档
       - 用 task_done 函数判断是否所有评审都通过: 1)合并所有md, 2)判断总md是否为空, 3)判断所有json是否true
     - 如果 全部评审通过, 则进入 <任务拆分阶段>
     - 如果 评审未通过
       - 读取 `{需求名}_详设评审记录.md` 评审建议, 要求 [需求分析师] 根据评审记录对 `{需求名}_详细设计.md` 进行优化, 并解答疑问与歧义. 用 `Prompt_05_DetailedDesign.modify_detailed_design` 提示词
         - 如果 [需求分析师] 发起 HITL (`{需求名}_与人类交流.md`不为空), 然后将人类的反馈发给 [需求分析师] 用 `Prompt_05_DetailedDesign.hitl_relpy` 提示词. 
         - 如果 [需求分析师] 没有发起 HITL (`{需求名}_与人类交流.md`为空), 那么读取 `{需求名}_需求分析师反馈.md` 用 `Prompt_05_DetailedDesign.again_review_detailed_design` 提示词发给各个评审智能体. 
       - 各个审核智能体再次审核, 直到全部回复 "审核通过", 则进入 <任务拆分阶段>

6. **任务拆分阶段**
   - 进入任务拆分阶段前, 必须确保 `{需求名}_详细设计.md` 存在. 否则要先跑详细设计阶段
   - 如果 `{需求名}_任务单.json` 和 `{需求名}_任务单.md` 不存在, 强制要跑任务拆分阶段
   - 如果是直接跳入到本阶段 (之前执行过详细设计但是本轮未执行详细设计, 已经生成了 `{需求名}_详细设计.md`, 如果没有 `{需求名}_详细设计.md` 则必须先进行详细设计)
     - 强制创建 [需求分析师], 然后先使用提示词 `Prompt_06_TaskSplit.create_task_split_ba` 初始化, 再用提示词 `Prompt_06_TaskSplit.task_split` 做任务拆分生成 `{需求名}_任务单.md`
     - 强制创建各个不同的评审角色
       - 人类可以设置 <角色定位> 的智能体: 开发工程师, 测试工程师, 审核员, 等等 (有默认值). 并选择各个审核智能体的厂商/模型/推理强度/代理端口
       - 然后给各个审核智能体跑初始化提示词 `Prompt_06_TaskSplit.create_task_split_ba` 做初始化 (初始化这一步可以与 [需求分析师] 的初始化并发执行)
       - 等到 [需求分析师] 完成任务拆分生成 `{需求名}_任务单.md`, 给各个审核智能体跑审核提示词 `Prompt_06_TaskSplit.review_task`
       - 各个不同的评审角色对 `{需求名}_任务单.md` 做评审, 并发执行. 然后将评审结果写入各自的 `{需求名}_任务单评审记录_{agent_name}.md` 和 `{需求名}_评审记录_{agent_name}.json`. 用 check_reviewer_job 函数检查各个评审角色是否有按要求更新文件, 若未按要求则跑对应的体现提示词.
       - 用 task_done 函数, 对 `{需求名}_任务单评审记录_{agent_name}.md` 统一合并到 `{需求名}_任务单评审记录.md` 中, 判断总md是否为空, 判断所有json是否true
     - 如果 全部评审通过, 则进入 <任务开发阶段>
     - 如果评审未通过
       - [需求分析师] 基于  `{需求名}_任务单评审记录.md` 优化 `{需求名}_任务单.md`, 并解答疑问与歧义. 用 `Prompt_06_TaskSplit.modify_task` 提示词
       - 再次调用各个评审角色进行评审, 用 `Prompt_06_TaskSplit.again_review_task` 然后循环直到全部评审通过
   - 如果是执行完详细设计后, 进入到本阶段的
     - 延用详细设计的 [需求分析师] 智能体, 直接用提示词 `Prompt_06_TaskSplit.task_split` 做任务拆分生成 `{需求名}_任务单.md`
     - 延用详细设计的各个审核智能体, 等到 [需求分析师] 完成任务拆分生成 `{需求名}_任务单.md`, 给各个审核智能体跑审核提示词 `Prompt_06_TaskSplit.review_task`
       - 各个不同的评审角色对 `{需求名}_任务单.md` 做评审, 并发执行. 然后将评审结果写入各自的 `{需求名}_任务单评审记录_{agent_name}.md` 和 `{需求名}_评审记录_{agent_name}.json`. 用 check_reviewer_job 函数检查各个评审角色是否有按要求更新文件, 若未按要求则跑对应的体现提示词.
       - 用 task_done 函数, 对 `{需求名}_任务单评审记录_{agent_name}.md` 统一合并到 `{需求名}_任务单评审记录.md` 中, 判断总md是否为空, 判断所有json是否true
     - 如果评审未通过
       - [需求分析师] 基于 `{需求名}_任务单评审记录.md` 优化 `{需求名}_任务单.md`, 并解答疑问与歧义. 用 `Prompt_06_TaskSplit.modify_task` 提示词
       - 再次调用各个评审角色进行评审, 用 `Prompt_06_TaskSplit.again_review_task` 然后循环直到全部评审通过
     - 如果 全部评审通过, 则对 [需求分析师] 智能体调用 `Prompt_06_TaskSplit.task_md_to_json` 将 `{需求名}_任务单.md` 转为JSON格式的 `{需求名}_任务单.json`
       - 用 is_standard_task_initial_json 函数判断 `{需求名}_任务单.json` 是否符合要求
       - 若不符合要求, 则对 [需求分析师] 智能体使用 `Prompt_06_TaskSplit.re_task_md_to_json` 要求再次生成. 循环直到 `{需求名}_任务单.json` 符合要求.

7. **任务开发阶段**
   - 进入任务开发阶段前, 必须确保 `{需求名}_任务单.json` 和 `{需求名}_任务单.md` 存在. 否则要先跑任务拆分阶段
   - 创建 [开发工程师] 智能体, 可以指定角色定位提示词, 也可以用系统默认的角色定位提示词
     - 初始化 [开发工程师] 智能体, 使用 `Prompt_07_Development.init_developer` 做初始化
       - [开发工程师] 智能体认为 '准备就绪': `{需求名}_向人类提问.md` 为空
       - [开发工程师] 智能体认为 '阻断': 写 `{需求名}_向人类提问.md` 触发 HITL
         - 人类的回复使用 `Prompt_07_Development.human_reply` 回馈给 [开发工程师] 智能体
           - [开发工程师] 智能体认为 '准备就绪': `{需求名}_向人类提问.md` 为空
           - [开发工程师] 智能体认为 '阻断': 写 `{需求名}_向人类提问.md` 触发 HITL. 循环直到 [开发工程师] 智能体认为 '准备就绪' 并且 `{需求名}_向人类提问.md` 为空
   - 创建各个不同的评审角色
     - 人类可以设置 <角色定位> 的智能体: 需求分析师, 测试工程师, 审核员, 架构师, 等等 (有默认值). 并选择各个审核智能体的厂商/模型/推理强度/代理端口
     - 初始化评审智能体, 使用 `Prompt_07_Development.init_code_reviewer` 做初始化
     - 评审智能体的初始化与 [开发工程师] 智能体的初始化可以同步执行, 以节约时间
   - [开发工程师] 智能体认为 '准备就绪' 后, 让人类选择开发工程师可用的subagent数量. 然后使用 get_first_false_task 函数判断下一个需要开发的任务. 然后使用 `Prompt_07_Development.start_develop` 进行开发
     - 完成任务开发后, 对评审智能体用 `Prompt_07_Development.reviewer_review_code` 做代码评审. 全部完成后用 check_reviewer_job 函数检查各个评审角色是否有按要求更新文件, 若未按要求则跑对应的体现提示词.
     - 用 task_done 函数, 对 `{需求名}_代码评审记录_{agent_name}.md` 统一合并到 `{需求名}_代码评审记录.md` 中, 判断总md是否为空, 判断所有json是否true, 更新 `{需求名}_任务单.json`
     - 如果评审未通过
       - [开发工程师] 基于 `{需求名}_代码评审记录.md` 优化代码, 并解答疑问与歧义. 用 `Prompt_07_Development.refine_code` 提示词
       - 再次调用各个评审角色进行评审, 用 `Prompt_07_Development.re_review_code` 然后循环直到全部评审通过
     - 如果全部评审未通过
       - 使用 get_first_false_task 函数判断下一个需要开发的任务. 然后使用 `Prompt_07_Development.start_develop` 进行开发
       - 循环直到全部任务开发完成

8. **测试阶段**

9. **复合阶段**

10. **提交代码,上传文档**

11. **提交PR**



NOTE:
- 智能体要精确执行的动作:
  - 业务指令产出
    - 修改代码 (开发工程师)
    - 修改指定文件
      - 原始需求.md, 需求澄清.md, 人类澄清记录.md, 
  - 与系统相关的
    - 执行是否成功
      - 一般在 JSON 文件中更新写入, 用于标记某一个阶段是否完成
    - HITL 相关
      - 把要发给人类的话, 写入指定md文件, 系统读取后发给人类
    - runtime 的 JSON 文件状态变更
      - running 转为 done, 用于使系统知道, 智能体完成了当前指令
