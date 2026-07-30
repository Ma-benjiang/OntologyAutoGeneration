# Agent Skill README 与安装方式调研

调研日期：2026-07-30

## 结论摘要

本仓库首先是一个可安装的 **Agent Skill**，README 首屏应明确写出这一身份，并把安装命令放到普通 Python 开发安装之前。

推荐主安装命令：

```bash
npx skills add Ma-benjiang/OntologyAutoGeneration/ontology-auto-generation
```

它会把 GitHub 仓库根目录下的 `ontology-auto-generation/` 作为 Skill 根目录。调研时使用当前 `skills` CLI 执行：

```bash
npx -y skills add Ma-benjiang/OntologyAutoGeneration/ontology-auto-generation --list
```

CLI 成功解析出：

```text
Source: https://github.com/Ma-benjiang/OntologyAutoGeneration.git (ontology-auto-generation)
Found 1 skill
ontology-auto-generation
```

更显式、与官方 README 示例完全同形的等价命令：

```bash
npx skills add https://github.com/Ma-benjiang/OntologyAutoGeneration/tree/master/ontology-auto-generation
```

README 建议紧接一个可复制的触发示例：

```text
使用 ontology-auto-generation，将 docs/orders.md 重建为包含 TBox 和 ABox 的 OWL 本体，
输出到 ontology-output，并保留逐事实证据和拒绝原因。
```

对本项目最合适的 README 主线是：

> Agent Skill 身份 + 一句话输入/输出 → `npx skills add` → 一条触发提示词 → 实际产物树与 OWL/evidence 对照 → 七角色流程和确定性边界 → 支持的 agents / 前置条件 → 验证与开发 → 致谢 / MIT

## 1. `npx skills add` 的准确语法

### 1.1 官方命令模型

`skills` 官方 README 将安装写成：

```bash
npx skills add <source>
```

`<source>` 支持 GitHub `owner/repo`、完整 GitHub URL、指向仓库内某个 Skill 的 `tree/<branch>/<path>` URL、其他 Git URL和本地路径。官方给出的“仓库内直接路径”示例为：

```bash
npx skills add https://github.com/vercel-labs/agent-skills/tree/main/skills/web-design-guidelines
```

来源：[vercel-labs/skills README：Source Formats](https://github.com/vercel-labs/skills/blob/7cb7db64dc1201052dea305e508a2fc490f7e5e2/README.md#L28-L48)

常用参数：

| 参数 | 含义 | 本项目建议 |
|---|---|---|
| 无 scope 参数 | 项目级安装，默认行为 | README 主命令采用 |
| `-g, --global` | 安装到用户级目录 | 作为可选命令 |
| `-a, --agent codex` | 只面向 Codex 安装 | 作为明确目标 agent 的示例 |
| `-y, --yes` | 跳过确认 | 只用于 CI / 自动化示例 |
| `-l, --list` | 只列出发现的 Skills | 用于安装前验证 |
| `--all` | 所有 Skills 安装到所有 agents，并跳过确认 | 不适合作为本项目主命令 |

来源：[官方参数与示例](https://github.com/vercel-labs/skills/blob/7cb7db64dc1201052dea305e508a2fc490f7e5e2/README.md#L50-L91) · [安装范围](https://github.com/vercel-labs/skills/blob/7cb7db64dc1201052dea305e508a2fc490f7e5e2/README.md#L95-L100)

### 1.2 仓库子目录的两种可靠写法

完整 GitHub URL 是官方 README 直接展示的方式：

```bash
npx skills add https://github.com/OWNER/REPO/tree/BRANCH/path/to/skill
```

CLI 同时支持更短的 `owner/repo/path/to/skill`。官方解析器把第三段及之后的路径解析为 `subpath`，官方测试也明确断言 `owner/repo/skills/my-skill` 会解析成仓库 URL加子目录：

- [source-parser.ts：`owner/repo/path/to/skill`](https://github.com/vercel-labs/skills/blob/7cb7db64dc1201052dea305e508a2fc490f7e5e2/src/source-parser.ts#L433-L462)
- [source-parser.test.ts：子目录简写测试](https://github.com/vercel-labs/skills/blob/7cb7db64dc1201052dea305e508a2fc490f7e5e2/tests/source-parser.test.ts#L167-L180)
- [source-parser.ts：GitHub tree URL 解析](https://github.com/vercel-labs/skills/blob/7cb7db64dc1201052dea305e508a2fc490f7e5e2/src/source-parser.ts#L351-L360)

因此本仓库的推荐命令可使用简短形式：

```bash
npx skills add Ma-benjiang/OntologyAutoGeneration/ontology-auto-generation
```

如需全局安装且只写入 Codex：

```bash
npx skills add Ma-benjiang/OntologyAutoGeneration/ontology-auto-generation \
  --global \
  --agent codex
```

### 1.3 为什么不能只写仓库根路径

本仓库的 `SKILL.md` 位于：

```text
OntologyAutoGeneration/
└── ontology-auto-generation/
    └── SKILL.md
```

`ontology-auto-generation/` 不是 CLI 默认列出的 `skills/`、`.agents/skills/` 等 Skill 容器目录，仓库根目录本身也没有 `SKILL.md`。官方发现规则说明：默认搜索根 `SKILL.md` 和已知容器；其他位置需要 `--full-depth`。

来源：[vercel-labs/skills README：Skill Discovery](https://github.com/vercel-labs/skills/blob/7cb7db64dc1201052dea305e508a2fc490f7e5e2/README.md#L376-L445)

所以不应把下面这条作为唯一安装命令：

```bash
npx skills add Ma-benjiang/OntologyAutoGeneration
```

直接指定 `ontology-auto-generation` 子目录更短、更确定，也无需让用户理解 `--full-depth`。

## 2. 高可见度 Skill 仓库 README 样本

选择 8 个成熟或高 Star 的公开 Skill / Skills / Skill Marketplace 仓库。Star 数由 GitHub Repository API 在调研日读取，只是规模快照，不代表 README 与流行度之间存在因果关系。

| 仓库 | Stars 快照 | 首屏定位 | 安装与 agents | 触发 / 产物展示 | 开发 / 验证 |
|---|---:|---|---|---|---|
| [anthropics/skills](https://github.com/anthropics/skills/blob/b29e7cf65e5cb78a5ac33d582270551bc74a14eb/README.md#L1-L90) | [165,086](https://api.github.com/repos/anthropics/skills) | 先定义 Skill，再解释仓库是 Claude 的示例实现 | Claude Code marketplace、Claude.ai、Claude API | 给出一句自然语言触发示例；不在根 README 展示产物 | 给最小 `SKILL.md` 模板并提醒关键场景需自行测试 |
| [vercel-labs/agent-skills](https://github.com/vercel-labs/agent-skills/blob/7c180d9044c9ae2b442b567aad4e42a28dd5ed62/README.md#L1-L228) | [29,612](https://api.github.com/repos/vercel-labs/agent-skills) | 两句话说明“给 AI coding agents 的指令与脚本集合” | 首屏后直接 `npx skills add vercel-labs/agent-skills` | 每个 Skill 都列 `Use when`；给实际 prompt；部署 Skill 展示成功输出 | 根 README 只说明 `SKILL.md` / `scripts/` / `references/` 结构，无测试命令 |
| [addyosmani/agent-skills](https://github.com/addyosmani/agent-skills/blob/7829ffd90d973b6325f5f12f1b1226dcace74443/README.md#L1-L177) | [80,929](https://api.github.com/repos/addyosmani/agent-skills) | “production-grade engineering skills” + 开发生命周期图 | `npx skills add` 是 any-agent 最短路径；折叠展示各 harness 原生安装方式 | `/spec`、`/plan`、`/test` 等命令映射到生命周期；同时说明自动触发 | README 尾部给 Skill 质量原则和贡献文档入口，但无根级测试命令 |
| [obra/superpowers](https://github.com/obra/superpowers/blob/44c9b2d6e889982ac18c27d05a19fefe335194e1/README.md#L1-L259) | [263,400](https://api.github.com/repos/obra/superpowers) | 一句定位为“完整开发方法论”，随后解释自动工作流 | 按 Claude、Codex、Cursor、Gemini、Copilot、OpenCode、Pi 等 harness 分列 | 用 7 步基本工作流说明各 Skill 何时自动激活、保存什么、如何验证 | 明确贡献流程、行为 eval 与基础设施测试入口 |
| [wshobson/agents](https://github.com/wshobson/agents/blob/c4b82b0ad771190355eb8e204b1329732a18449a/README.md#L1-L140) | [38,358](https://api.github.com/repos/wshobson/agents) | 首句给插件 / agent / skill / command 数量和多 harness 边界 | 首屏按 Claude、Codex、Cursor、OpenCode、Gemini、Copilot 分流 | 根 README 偏目录与能力矩阵，不强调自然语言触发示例 | `make generate-all`、`make validate`、`make garden`，另有静态 / LLM / Monte Carlo 三层评估 |
| [K-Dense-AI/scientific-agent-skills](https://github.com/K-Dense-AI/scientific-agent-skills/blob/ab2f84ab10597c59fac186ecda6d5edd5dcc8b92/README.md#L1-L142) | [32,130](https://api.github.com/repos/K-Dense-AI/scientific-agent-skills) | 徽章后给数量、领域范围、标准和支持 agents | `npx skills add`；另列 GitHub CLI、手工和不同 hosts | 多组“Goal → Prompt → Skills Used”，适合复杂领域 Skill | 有结构契约、逐 Skill 测试、隔离全量测试、CI 和安全扫描命令 |
| [OthmanAdi/planning-with-files](https://github.com/OthmanAdi/planning-with-files/blob/b04ffd9c8f9f93919649d197e5d4ec1bfc06fa14/README.md#L1-L190) | [25,840](https://api.github.com/repos/OthmanAdi/planning-with-files) | Hero + 强问题句“上下文会死，计划不会” | Claude plugin + 其他 60+ agents 的 `npx skills add` | 首屏直接做 before / after，对照展示三个真实 Markdown 产物；写明 `/plan`、自然语言和自动触发 | 展示 benchmark、测试数、方法局限、仓库结构与贡献入口 |
| [mattpocock/skills](https://github.com/mattpocock/skills/blob/2ab958093e83e0ec752e6c1c5932da465bf23e0c/README.md#L1-L84) | [194,851](https://api.github.com/repos/mattpocock/skills) | 个人化强定位：“real engineering, not vibe coding” | Claude 官方 plugin；Codex 和其他 agents 用 `npx skills@latest add` | 以四类真实失败模式解释 Skill 价值；明确区分用户触发与模型触发 | 鼓励复制后自行修改，根 README 未提供仓库验证命令 |

### 样本中的具体可借鉴点

#### Anthropic：先回答“Skill 是什么”

Anthropic README 首段先定义 Skill 是“instructions, scripts, resources”的文件夹，再介绍仓库范围；安装后只需在对话中提及 Skill，并给出“用 PDF Skill 提取表单字段”的完整句子。这比只把 `SKILL.md` 藏在文档链接中更容易让新用户建立正确心智模型。

来源：[定义、安装与触发示例](https://github.com/anthropics/skills/blob/b29e7cf65e5cb78a5ac33d582270551bc74a14eb/README.md#L5-L61)

#### Vercel：`Use when` + 可复制 prompt + 可见输出

Vercel 为每个 Skill 列出明确的 `Use when`，并给出用户真实会说的话；部署 Skill 还展示 Preview URL 与 Claim URL 的成功输出。README 不是只描述内部流程，而是把“何时触发”和“得到什么”放在一起。

来源：[触发条件](https://github.com/vercel-labs/agent-skills/blob/7c180d9044c9ae2b442b567aad4e42a28dd5ed62/README.md#L9-L54) · [输出、安装与 prompt](https://github.com/vercel-labs/agent-skills/blob/7c180d9044c9ae2b442b567aad4e42a28dd5ed62/README.md#L160-L224)

#### Addy Osmani：安装命令前先把 Skill 映射到用户工作流

README 用 `DEFINE → PLAN → BUILD → VERIFY → REVIEW → SHIP` 图和 slash commands 表说明产品表面，再提供一条 any-agent 安装命令。各 agent 的复杂安装差异放进折叠区，不阻塞主路径。

来源：[定位、命令与 Quick Start](https://github.com/addyosmani/agent-skills/blob/7829ffd90d973b6325f5f12f1b1226dcace74443/README.md#L1-L62) · [多 harness 安装](https://github.com/addyosmani/agent-skills/blob/7829ffd90d973b6325f5f12f1b1226dcace74443/README.md#L63-L177)

#### Superpowers：解释“自动触发后的完整旅程”

Superpowers 不只罗列 Skills，而是用基本工作流说明从 brainstorming、plan、TDD、review 到 branch completion 的触发顺序和检查点；安装则按 harness 明确分流。其 README 还把 Skill 行为 eval 与插件基础设施测试分开。

来源：[自动工作方式与安装](https://github.com/obra/superpowers/blob/44c9b2d6e889982ac18c27d05a19fefe335194e1/README.md#L1-L195) · [基本工作流](https://github.com/obra/superpowers/blob/44c9b2d6e889982ac18c27d05a19fefe335194e1/README.md#L196-L239) · [贡献与测试](https://github.com/obra/superpowers/blob/44c9b2d6e889982ac18c27d05a19fefe335194e1/README.md#L249-L261)

#### Wshobson：当支持多个 agents 时给能力矩阵与验证入口

该 README 首屏明确支持的 harness，随后区分每个 harness 生成什么原生文件，并把结构验证、漂移检查和质量评估命令放在根 README。适合多适配器仓库；单 Skill 项目无需照搬庞大矩阵，但应明确“已验证”和“仅因标准推定兼容”的区别。

来源：[首屏、安装和能力矩阵](https://github.com/wshobson/agents/blob/c4b82b0ad771190355eb8e204b1329732a18449a/README.md#L1-L106) · [验证与质量评估](https://github.com/wshobson/agents/blob/c4b82b0ad771190355eb8e204b1329732a18449a/README.md#L98-L122)

#### K-Dense：复杂领域 Skill 用“Goal → Prompt → Skills Used”

长 prompt 并非总是坏事。科学工作流需要说清数据源、步骤、安全边界和交付物，因此该 README 用多组完整 prompt 展示组合方式；工程可信度则由结构契约、逐 Skill tests、隔离测试和安全扫描支撑。

来源：[安装](https://github.com/K-Dense-AI/scientific-agent-skills/blob/ab2f84ab10597c59fac186ecda6d5edd5dcc8b92/README.md#L130-L205) · [Quick Examples](https://github.com/K-Dense-AI/scientific-agent-skills/blob/ab2f84ab10597c59fac186ecda6d5edd5dcc8b92/README.md#L290-L384) · [开发与验证](https://github.com/K-Dense-AI/scientific-agent-skills/blob/ab2f84ab10597c59fac186ecda6d5edd5dcc8b92/README.md#L633-L700)

#### Planning with Files：单 Skill 最强模式是 before / after + 真实文件

它先用一句痛点定位，再直接展示没有 Skill 与使用 Skill 后的对话差异，并把 `task_plan.md`、`findings.md`、`progress.md` 三个落盘产物画出来；安装命令旁同时写 `/plan`、自然语言和自动触发方式。后续 benchmark 明确披露模型、日期、测量目标和局限。

来源：[首屏、before / after、产物与安装](https://github.com/OthmanAdi/planning-with-files/blob/b04ffd9c8f9f93919649d197e5d4ec1bfc06fa14/README.md#L1-L190) · [Benchmark 与产物结构](https://github.com/OthmanAdi/planning-with-files/blob/b04ffd9c8f9f93919649d197e5d4ec1bfc06fa14/README.md#L367-L466)

#### Matt Pocock：先解释为什么，再区分用户触发与模型触发

README 先给 30 秒安装，再通过“没理解需求、太啰嗦、代码不工作、架构变泥球”四类失败模式说明 Skills 的价值。目录明确区分 user-invoked 与 model-invoked，降低“需要输入 slash command，还是会自动触发”的歧义。

来源：[定位与安装](https://github.com/mattpocock/skills/blob/2ab958093e83e0ec752e6c1c5932da465bf23e0c/README.md#L1-L84) · [触发模型与 Skill 目录](https://github.com/mattpocock/skills/blob/2ab958093e83e0ec752e6c1c5932da465bf23e0c/README.md#L184-L228)

## 3. 跨仓库共性

### 3.1 首屏先完成四个判断

成熟 Skill README 通常让读者在前两屏判断：

1. **这是不是 Agent Skill？**
2. **它解决什么具体问题？**
3. **我的 agent 能不能安装？**
4. **装完后该说什么、会得到什么？**

本项目当前 README 很好地解释了 OWL 工程价值，但对第 1、3、4 点出现得太晚或没有显式回答。

### 3.2 安装分“用户安装 Skill”和“维护者开发仓库”

常见顺序是：

1. `npx skills add ...` 或原生 plugin 安装；
2. 一条触发 prompt；
3. 需要修改 Skill 或运行测试的人，再 `git clone`、装 Python 依赖、执行测试。

对普通用户，把 `git clone` + virtualenv 放在第一安装路径，会让项目看起来像只能手工运行的 Python 工具，而不是可被 Agent 自动发现的 Skill。

### 3.3 触发示例应写成用户语言

好示例不是“运行 `ontology_pipeline.py run start`”，而是：

```text
使用 ontology-auto-generation，将 docs/orders.md 重建为 OWL，
输出到 ontology-output，并保留每个事实的原文证据。
```

脚本命令适合放在“开发 / 调试”或折叠的“运行生命周期”中。

### 3.4 产物展示比能力形容词更可信

对本项目，最强证据已经存在：

- `ontology.owl` 的 RDF/XML 片段；
- 对应的 `evidence.jsonl`；
- `rejections.jsonl`；
- `qa_report.json` 三 Gate 状态；
- 完整产物目录树。

这些应紧跟安装和触发示例，形成“安装 → 发出请求 → 得到这些文件”的闭环。

### 3.5 支持 agents 要分清“明确验证”和“标准兼容”

可写：

- 使用开放 Agent Skills 格式；
- `skills` CLI 可安装到 Codex、Claude Code、Cursor 等受支持 hosts；
- 本项目在哪些 hosts 实际做过验证。

如果仓库没有跨 host 测试证据，不应把 CLI 的 70+ 安装目标等同于本 Skill 已在 70+ agents 完成行为验证。

### 3.6 验证应同时覆盖格式和行为

高可信 README 通常至少给一种可运行验证：

- 结构：frontmatter、链接、脚本语法、文件布局；
- 行为：真实输入能否产生预期产物和 QA；
- 若有 benchmark：披露模型、日期、数据集、指标和局限。

本项目已有 Python 测试与真实 OWL / evidence 样例，因此无需自造 benchmark；把现有测试命令、测试范围和预期 `OK` 写清即可。

## 4. 对本仓库 README 的推荐结构

```text
Hero / 名称
Agent Skill 身份 + 一句话定位
[MIT · Agent Skills]

## Install
npx skills add Ma-benjiang/OntologyAutoGeneration/ontology-auto-generation
可选：--global --agent codex

## Use
一条最小中文 prompt
一条多文档 prompt
明确：只处理用户显式选择的 Markdown

## What you get
ontology.owl + artifacts/ 目录树
OWL 与 evidence 对照
QA PASS 示例

## Why this Skill
Schema Card 锁定
稳定 NamedIndividual 身份
逐事实证据与拒绝原因
三 Gate QA 和可恢复运行

## How it works
七角色图
LLM 提议 / Python 准入、序列化与验证边界

## Supported agents
开放 Agent Skills 格式
已验证 hosts
其他 hosts 的兼容性措辞

## Requirements
Python 版本与 requirements

## Development and verification
git clone
pip install
unittest
确定性冒烟测试 / 预期结果

## Documentation
SKILL.md、CONTEXT.md、ADR

## Acknowledgements
实现参考项目

## License
MIT
```

## 5. 最小修改优先级

1. **P0：首屏明确写“Agent Skill”。**
2. **P0：在第一屏附近加入准确的 `npx skills add Ma-benjiang/OntologyAutoGeneration/ontology-auto-generation`。**
3. **P0：安装命令后给一条真实自然语言触发示例。**
4. **P1：把现有 OWL / evidence / QA 展示改成“使用后会得到什么”的闭环。**
5. **P1：把 `git clone`、virtualenv 和单元测试重新标成“开发 / 本地验证”，不是主要用户安装方式。**
6. **P1：说明支持 agents 的依据，避免把可安装误写成全部行为已验证。**
7. **P2：如要添加徽章，只放能核验的 License、测试状态、Agent Skills；不要添加无对应基础设施的版本、下载量或兼容性徽章。**

## 研究局限

- Star 数是 2026-07-30 的 GitHub API 快照，会继续变化。
- 样本包含单 Skill、Skills 集合和 Plugin Marketplace；借鉴的是信息架构，不代表本项目需要复制其规模或全部安装适配器。
- README 只能声明仓库真实提供并验证的能力。高 Star 仓库中的“production-ready”“70+ agents”“benchmark”等表述不能直接迁移到本项目。
- CLI 行为依据 `vercel-labs/skills` 固定提交 `7cb7db6`；未来版本可能扩展参数或发现路径。
