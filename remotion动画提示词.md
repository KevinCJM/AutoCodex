要彻底让 AI 摆脱“PPT 式渲染”，你需要建立一套**模块化、强约束的 Prompt 体系**。不要试图用一段话让 AI 写完整个复杂的视频，而是把它当成一个“流水线工人”，针对不同组件喂给它专门的指令。

以下为你整理的一套工业级的 Remotion 提示词库。如果你正在构建自动化的多智能体系统（比如类似 AutoCodex 这样的代码生成架构），建议将第一段作为 System Prompt 全局挂载，后续的作为具体任务 Prompt 分发。

---

### 1. 全局系统指令 (System Prompt)
**作用：** 设定 AI 的角色基调，封杀所有劣质动画 API，强制引入 Remotion 物理引擎。
**用法：** 每次对话的开头，或者写入智能体的 `system_message` 中。

```md
**System Role:** 你是一位拥有像素级强迫症的顶级 Motion Graphics 工程师，精通 React 和 Remotion。你的任务是编写极客风格、数据驱动的视频组件。

**Core Constraints (绝对不可违反):**
1. **禁用静态加载：** 组件绝对不能在第 0 帧瞬间出现。必须有进场动画。
2. **封杀 CSS Transition：** 绝对禁止使用 CSS 的 `transition` 或 `animation` 属性。
3. **强制物理引擎：** 所有的位移 (Translate)、缩放 (Scale) 必须由 Remotion 的 `spring` API 驱动。必须导入 `import { spring, useCurrentFrame, useVideoConfig } from 'remotion';`。
4. **强制时间轴编排：** 复杂组件必须使用 `<Sequence from={...}>` 来控制元素的出场先后顺序，绝不能所有元素同帧进场。
```

---

### 2. 核心动效模板 (Task Prompts)
当需要 AI 生成具体的组件时，直接将对应的数据结构和以下 Prompt 喂给它。

#### 模板 A：极客终端打字机 (Terminal Typewriter)
**适用场景：** 视频大标题、代码展示、日志输出。

```md
### 终端打字机组件
**Task:** 基于提供的内容 `[此处填入你的文本]`，编写一个 Remotion 组件 `<TerminalTitle>`。
**Animation Rules:**
1. 字体强制使用 `monospace`，颜色设置为 `[#ff7b72或你的主色调]`。
2. 必须手写打字逻辑：读取 `useCurrentFrame()`，每 2 帧（或 3 帧）渲染一个新字符。使用 `text.slice(0, Math.floor(frame / 2))` 实现。
3. 添加一个跟随光标 `_`。光标的透明度（Opacity）必须通过 `Math.floor(frame / 15) % 2 === 0 ? 1 : 0` 实现每 15 帧闪烁一次的硬切效果。
4. 整个文本框在打字开始前，需要有一个极短的 `spring` 缩放进场（从 scale 0.95 到 1.0），`spring` 的 config 设为 `{ damping: 12, stiffness: 100 }`。
```

#### 模板 B：错层瀑布流列表 (Staggered Data List)
**适用场景：** 排行榜、数据库检索结果展示、多节点状态反馈。

```md
### 错层瀑布流列表组件
**Task:** 基于提供的数组数据 `[此处填入 JSON 数组]`，编写一个 Remotion 列表组件 `<StaggeredLeaderboard>`。
**Animation Rules:**
1. 列表中的每一个 Item 绝对不能同时出现。在 `map` 循环中，必须利用 `index` 设置延迟。
2. 帧延迟计算：`const delay = index * 8;` (每个条目比上一个晚 8 帧出场)。
3. Item 进场动效：必须结合 `delay` 计算真实的动画帧 `const adjustedFrame = Math.max(0, frame - delay);`。
4. 使用 `spring` 驱动进场：Y 轴方向从 `translateY(30px)` 运动到 `0px`，Opacity 从 0 运动到 1。不要用线性 interpolate 做透明度，让它带有一点弹性。
```

#### 模板 C：金融级数字滚动 (Data Counter / Interpolation)
**适用场景：** Token 消耗量、资金收益率、性能跑分等核心指标。

```md
### 数字滚动组件
**Task:** 编写一个展示核心指标的组件 `<RollingMetric value={ [目标数值] } />`。
**Animation Rules:**
1. 禁止直接渲染静态的最终数值。
2. 必须使用 Remotion 的 `interpolate` 函数，将时间转换为数值的增长。
3. 设置动画区间：在组件挂载的第 10 帧到第 40 帧之间，数值从 0 滚动到目标 `value`。
4. `interpolate` 的 `extrapolateRight` 必须设置为 `'clamp'`，确保数字到达目标值后停止。
5. 必须对渲染的数字进行格式化处理（如保留两位小数，或添加千位分隔符）。
```

---

### 💡 执行建议

如果你希望生成的质量极高，可以采取 **“先生成组件，再组合 Sequence”** 的策略：
1. 先发送 **模板 A**，让 AI 写出 `<TerminalTitle>` 并保存。
2. 发送 **模板 B** 和 **模板 C**，让 AI 结合起来写出带数字滚动的 `<StaggeredLeaderboard>`。
3. 最后，给出一个组合指令：“*编写主 `<Composition>`，使用 `<Sequence>`，在第 0 帧渲染 `<TerminalTitle>`，在第 45 帧（打字机结束后）渲染 `<StaggeredLeaderboard>`。*”

这套极其具体的“导演级”指令，能够彻底压榨出 AI 结合 Remotion 的渲染潜力。