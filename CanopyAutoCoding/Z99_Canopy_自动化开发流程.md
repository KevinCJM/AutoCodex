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
   - 需求录入
     - 要求用户输入需求名称
     - 通过 文档/文字输入/Notion链接 等方式, 将原始需求写入 `{需求名}_原始需求.md`
       - 如果是 Notion 则启动一个 codex, GPT-5.4-Mini, high 临时智能体用 $notion-api-token-ops 这个技能读取需求. 提示词用 `Prompt_02_RequirementIntake.get_notion_requirement`

3. **需求澄清阶段**
   - 需求澄清:
     1. [需求分析师] 智能体通过了解代码, 以及需求说明, 分析需求. 使用提示词 `Prompt_03_RequirementsClarification.requirements_understand`
     2. 如果 [需求分析师] 智能体认为信息不足以澄清需求, 则要求HITL人类提供更多信息. 将提问写入`{需求名}_与人类交流.md`, 由 [系统] 提交给人类
        2.1 将人类的答复返回给 [需求分析师] 智能体. 使用提示词 `Prompt_03_RequirementsClarification.hitl_bck`
        2.2 [需求分析师] 智能体基于新信息再次分析需求, 将人类反馈的信息写入`{需求名}人机交互澄清记录.md`. 判断信息是是否仍然缺失. 循环直到 [需求分析师] 认为信息完整.
     3. 如果 [需求分析师] 智能体认为信息足够, 则写入 `{需求名}_需求澄清.md`

4. **需求澄清评审阶段**
     - 人类审核 `需求澄清.md`
      1. 如果审核通过, 则进入 需求评审阶段
      2. 如果审核未通过, 则人类发出提问. 使用提示词 `Prompt_04_RequirementsReview.human_feed_bck`
        - [需求分析师] 智能体基于人类反馈, 修改 `{需求名}_需求澄清.md` 或者向人类回复解释
      3. 重复, 直到人类审核通过, 进入 需求评审阶段
    - 审核智能体审核:
      1. 启动N个 [审核器] 智能体, 根据 `{需求名}_原始需求.md` + `{需求名}_人机交互澄清记录.md` 评审 `{需求名}_需求澄清.md` 文档, 每个 [审核器] 要有编号, 并行执行. 使用提示词 `Prompt_04_RequirementsReview.requirements_review_init`
         - [审核器] 智能体的人格定义是系统指定的, YOLO模式. 人类只能指定数量N,厂商,和模型.
         - [审核器] 智能体的评审意见写入 `{需求名}_需求评审记录_{编号}.md`, 然后系统合并所有 `{需求名}_需求评审记录_*.md` 到 `{需求名}_需求评审记录.md`
      2. 如果所有 [审核器] 智能体都判断评审通过, 则进入 详细设计阶段
      3. 评审若未通过, 则将审核结果交予 [需求分析师] 优化和修改. 使用提示词 `Prompt_04_RequirementsReview.review_feedback`
         3.1 如果有 [需求分析师] 认为无法决定的, 或者信息不足时, 触发HITL. 流程回到 需求澄清阶段 的 (2) 
         3.2 如果信息足够, [需求分析师] 回复 [审核器] 智能体再次审核. 使用提示词 `Prompt_04_RequirementsReview.requirements_review_reply`
         3.3 重复, 直到所有 [审核器] 智能体判断审核通过, 然后可以进入 详细设计阶段

5. **详细设计阶段** 
   - [需求分析师] 分析完需求拿到所有必要信息并通过评审后, 写详细设计文档. 提示词见 `Prompt_05_DetailedDesign.detailed_design`
   - 设置各个不同的评审角色, 基于需求与澄清文档评审 `详细设计.md`, 并行执行.
     - 人类可以设置各种背景的智能体: 开发工程师, 测试工程师, 审核员, 等等.
     - 用评审提示词 `Prompt_05_DetailedDesign.review_detailed_design`
     - 各个智能体将评审结果写入各自的 `详设评审记录_{智能体名称}.md` 中, 再统一合并到 `详设评审记录.md` 中
     - [需求分析师] 根据 `详设评审记录.md` 对 `详细设计.md` 进行优化, 并解答疑问与歧义
     - 各个智能体再次审核, 直到全部回复 "审核通过"
   - 换一个评审提示词: `Prompt_05_DetailedDesign.second_review_detailed_design`, 重新要求各个智能体进行评审, 直到全部回复 "审核通过"
   - 如果 [需求分析师] 智能体发现有一个问题在各个智能体之间讨论了3轮以上, 则中断流程强制开启HITL. 由 [需求分析师] 向人类发起提问
     - 人类就该问题点可以与 [需求分析师] 可以进行多轮对答, 可以提出反问. 直到 [需求分析师] 认为该问题得到解决后继续评审流程.
     - HITL人机交互澄清记录需要由 [调度器] 智能体记入 `人机交互澄清记录.md` 文档

6. **任务拆分阶段**
   - [需求分析师] 基于 `详细设计.md` + `原始需求.md` + `人机交互澄清记录.md` + `需求澄清.md`, 进行里程碑和任务单的拆分. 写入 `任务单.md`
   - 各个不同的评审角色对 `任务单.md` 做评审, 并发执行. 然后将评审结果写入各自的 `任务单评审记录_{agent_name}.md`. 然后再统一合并到 `任务单评审记录.md` 中
   - [需求分析师] 基于  `任务单评审记录.md` 优化 `任务单.md`, 并解答疑问与歧义
   - 各个智能体再次审核, 直到全部回复 "审核通过"
   - [需求分析师] 基于 `任务单.md` 输出 `任务单.json`. 用提示词 `Prompt_06_TaskSplit.task_md_to_json`

7. **任务开发阶段**

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
