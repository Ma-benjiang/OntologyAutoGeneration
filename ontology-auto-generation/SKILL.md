---
name: ontology-auto-generation
description: "Generate constrained OWL TBox ontologies and extract grounded ABox instances from one or more explicitly selected Markdown documents. Use for ontology generation, ontology population, schema-guided entity/fact extraction, stable OWL NamedIndividual identities, evidence sidecars, and validated RDF/XML delivery through the CQ→SRD→Schema Card→ABox→QA→Fixer workflow."
---

# OntologyAutoGeneration

将用户明确选择的 Markdown 文档完整重建为受约束的 OWL TBox+ABox。最终交付 `ontology.owl`，同时旁路保存每个实例与断言的原文证据和拒绝原因。

| Role | 职责 | 产物 |
|---|---|---|
| 1. CQ Generator | 提取能力问题 | `cqs.md` |
| 2. 领域分析师 | 归一化语义需求 | `srd.md` |
| 3. 本体架构师 | 锁定唯一 TBox 与抽取白名单 | `schema_card.json` |
| 4. 实例抽取师 | 先实体、后断言地提取候选 ABox | `abox_candidates.json` |
| 5. 本体构建师 | 确定性身份解析、事实准入与 RDF/XML 构建 | `resolved_instances.json`、OWL |
| 6. QA 审查员 | Gate 1 语义审查；Python 汇总 RDF/OWL-RL 与 bundle/SHACL | `qa_report.json` |
| 7. 修复专家 | 只完整替换 CQ、SRD 或 Schema Card 并重建 | replacement JSON |

## 前置条件

- 输入：用户明确指定的一个或多个 workspace 内 Markdown 文件；禁止自动发现文件。
- 输出目录：一个目录代表一个持续存在的 Ontology Project。
- 依赖：在本 Skill 根目录运行 `pip install -r requirements.txt`。
- 所有 Source Document 必须能表示为 workspace-relative POSIX path；拒绝 workspace 外文件。

## 产物

```text
output/
├── ontology.owl                 # 唯一正式交付，TBox+ABox 合并图
├── schema.owl                   # TBox 中间片段
├── instances.owl                # ABox 中间片段
└── artifacts/
    ├── cqs.md
    ├── srd.md
    ├── schema_card.json         # 唯一可信 TBox/抽取约束
    ├── abox_candidates.json
    ├── resolved_instances.json
    ├── evidence.jsonl
    ├── rejections.jsonl
    ├── qa_report.json
    └── qa_rounds/
```

`schema.owl` 和 `instances.owl` 是调试片段；只有 `ontology.owl` 包含唯一 `owl:Ontology` 声明并作为完整本体使用。

## 可恢复运行外壳

正常 Full Rebuild 通过公共 `run` 命令创建和恢复；`resolve`、`build` 保留为开发调试入口：

```bash
python3 <skill-root>/scripts/ontology_pipeline.py run start \
  --workspace <workspace-root> \
  --output <output> \
  --source <workspace-relative-source.md>
python3 <skill-root>/scripts/ontology_pipeline.py run status --output <output> --json
python3 <skill-root>/scripts/ontology_pipeline.py run submit \
  --output <output> --work-item-id <id> --input-digest <sha256> --result <result-file>
python3 <skill-root>/scripts/ontology_pipeline.py run resume --output <output>
python3 <skill-root>/scripts/ontology_pipeline.py run abort --output <output>
```

运行状态直接持久化于 `<output>/project.json`、`ledger.json`、`.staging/` 和 `releases/`。
所有公共 `run` 命令在项目级短事务锁内读取、校验并原子持久化状态；crash 后 `resume`
只恢复通过 project/config/source/contract digest 校验的 staging 或已验证的 terminal commit。
终态先发布 content-addressed Release Snapshot，再依次更新 `latest_attempt.json`、
`latest_delivery.json` 和 ledger；Latest Attempt 可在 crash boundary 暂时领先，Latest Delivery
只允许指向已验证的 `PASS`/`FORCED_WITH_ERRORS` snapshot。主动中止只更新 Latest Attempt，
并发布 `ORCHESTRATION_ABORTED` 的 `FAILED` Release Snapshot。

## 核心不变量

1. 严格按 Role 1→2→3→4→5→6→(7⇄6) 执行，每次切换打印 `### [Role N] <角色名>`。
2. 切换前重新读取该 Role 的依赖文件，不依赖对话记忆代替文件。
3. `schema_card.json` 是 TBox 和 ABox 白名单的唯一可信来源；OWL 始终由脚本派生，禁止手工编辑。
4. 原文证据只写入 `evidence.jsonl`/`rejections.jsonl`，禁止写入 OWL 自定义注解。
5. 只接纳原文明示的现实业务实体与事实；拒绝示例、假设、模板、建议、否定事实和无法定位原文的推断。
6. 每次运行都是 Full Rebuild：覆盖所有抽取产物，不复用旧 candidates/resolved 作为缓存。
7. 输出位置已有 `schema_card.json` 时，先读取其身份信息：保留 `ontology_iri`，并为语义无歧义匹配的旧 Ontology Term 复用 IRI。
8. QA/Fixer 最多 20 轮；每轮都重跑完整 3-Gate。

## 身份规则

### Ontology Project 与术语

- 第一次在输出位置运行时，生成一个不带 `#` 的稳定 IRI，例如 `https://example.org/ontology/sales-order`。
- `entity_namespace` 必须严格等于 `ontology_iri + "#"`。
- 新输出位置创建新 Ontology Project；同一输出位置 Full Rebuild 时保留项目 IRI。
- Class 使用 PascalCase，Property 使用 camelCase；本地名匹配 `^[A-Za-z][A-Za-z0-9_]*$`。
- 旧术语语义无歧义匹配时复用 IRI；歧义时创建新 IRI并在 QA 中警告，不猜测映射。

### NamedIndividual

- 有明确业务标识符：按 identity DatatypeProperty + 规范化标识值跨文档、跨重建稳定合并；名称变化不改变身份。
- 无业务标识符：按 Source Document 路径 + Class IRI + 规范化名称定址，只在该来源范围内合并。
- 不按相同名称跨文档合并；重名时宁可保留多个实例，也不做不安全合并。
- 移动来源文件或重命名无 ID 实体会创建新身份；标题和行号变化不会改变身份。

## OWL Profile

允许：

- `owl:Ontology`：仅 `ontology.owl` 中恰好一个。
- TBox：`owl:Class`、`owl:ObjectProperty`、`owl:DatatypeProperty`。
- ABox：`owl:NamedIndividual`、Class 类型断言、已声明 ObjectProperty/DatatypeProperty 断言。
- 元数据：`rdfs:label`、`rdfs:comment`。
- Schema 关系：`rdfs:subClassOf`、`rdfs:subPropertyOf`、`owl:inverseOf`、`owl:equivalentClass`、`owl:equivalentProperty`、`rdfs:domain`、`rdfs:range`、`owl:disjointWith`。
- 数据值：与 Schema Card range 完全一致的 XSD typed literal。

禁止：

- `owl:oneOf`、`owl:Restriction`、匿名类、RDF Collection、blank node。
- `owl:AnnotationProperty` 或自定义注解谓语。
- Functional、cardinality、union/intersection 等复杂 OWL 语义。
- 未在 Schema Card 中声明的 Class、Property、实例断言谓语。

枚举继续使用“枚举父类 + 值类子类 + ObjectProperty”，不把枚举值生成为业务实例。详细写法见 `references/owl-best-practices.md`；只有需要编写或审查 OWL 结构时读取该文件。

---

## Role 1: CQ Generator

1. 读取全部 Source Documents。
2. 写入 `output/artifacts/cqs.md`。
3. 最低 5 个 CQ，覆盖类层级、关系、数据属性和实例查询。

每个 CQ 包含：

```markdown
- **CQ-ID**: CQ-001
- **问题**: 客户 C-001 创建了哪些订单？
- **期望答案类型**: NamedIndividual 列表
- **原文依据**: 来源路径、标题路径、行范围、原文摘录
```

不要把文档中的示例或假设改写为需要真实 ABox 回答的 CQ。

## Role 2: 领域分析师

读取全部 Source Documents 和 `cqs.md`，写入 `srd.md`：

```markdown
# 语义需求文档

## 概念清单
| 概念名 | 类型 | 定义 | 原文证据 | 关联 CQ |

## 关系清单
| 主语类型 | 谓语 | 宾语类型 | 是否多值 | 原文证据 | 关联 CQ |

## 属性清单
| 所属概念 | 属性名 | XSD 类型 | 是否业务标识 | 最大值数 | 原文证据 | 关联 CQ |

## 枚举与层级
| 名称 | 父类/值类 | 原文证据 | 关联 CQ |

## ABox 信号
| 实体/事实类型 | 可接受证据模式 | 必须拒绝的模式 | 关联 CQ |
```

要求：

- 每个概念和属性支撑至少一个 CQ。
- 只有原文明示为稳定唯一编号的字段才能标记“业务标识”。
- `最大值数` 只有在原文明示单值/唯一时设为 1，否则留空。
- 不在 SRD 阶段生成或合并 NamedIndividual。

## Role 3: 本体架构师

1. 读取 `cqs.md`、`srd.md`，并在生成前完整读取 `references/schema-card.schema.json`。
2. 若输出位置已有旧 `schema_card.json`，先建立旧术语身份对照。
3. 写入符合契约的 `output/artifacts/schema_card.json`。

Schema Card 规则：

- 所有 `iri` 位于 `entity_namespace` 下。
- 所有数组字段即使为空也必须输出。
- ObjectProperty 必须有唯一且已声明的 Class domain/range。
- DatatypeProperty 必须有唯一 Class domain 和 XSD range。
- `identity: true` 只用于明确业务标识符。
- `max_count` 是抽取准入约束，不序列化为 OWL cardinality；无明确依据时省略。
- Domain/Range 不确定的 Property 不进入 Schema Card，并在 SRD/QA 记录原因。
- 每个 CQ 在 `cqs.md` 中补充可由哪些 Schema Card 术语回答。

## Role 4: 实例抽取师

1. 完整读取 `references/abox-candidates.schema.json`、`schema_card.json` 和全部 Source Documents。
2. 写入 `output/artifacts/abox_candidates.json`，先完成所有 `entities`，再完成 `assertions`。

### Pass A：Candidate Entity

- 只使用 Schema Card 中的 Class。
- 每次实体提及都给出全局唯一 `candidate_id`；可使用 `<source-slug>.entity.NNN`。
- 仅当原文同时给出标识值，且对应 DatatypeProperty 为 `identity: true` 时填写 `business_identifier`；否则写 `null` 或省略。
- `evidence.source` 必须是精确 workspace-relative POSIX path。
- `line_start`/`line_end` 必须覆盖 `quote`，`quote` 必须逐字来自该范围。

### Pass B：Candidate Assertion

- 先锁定实体表，再抽关系与数据属性，禁止在断言阶段临时创造实体。
- `kind: object` 只能引用两个 Candidate Entity；`kind: data` 必须携带与 Schema Card range 完全一致的 datatype。
- 每条断言必须有独立原文证据；不要用实体证据替代关系证据。
- 不输出置信度。证据不足即不候选，不以“低置信度”绕过准入。

## Role 5: 本体构建师

从 workspace 根目录执行，并把 `<skill-root>` 替换为本 Skill 的绝对路径。每个 Source Document 都重复提供一个 `--source`：

```bash
python3 <skill-root>/scripts/ontology_pipeline.py resolve \
  output/artifacts/schema_card.json \
  output/artifacts/abox_candidates.json \
  --workspace <workspace-root> \
  --source <workspace-relative-source-1.md> \
  --source <workspace-relative-source-2.md> \
  --output output/artifacts/resolved_instances.json \
  --evidence output/artifacts/evidence.jsonl \
  --rejections output/artifacts/rejections.jsonl
```

然后构建 RDF/XML：

```bash
python3 <skill-root>/scripts/ontology_pipeline.py build \
  output/artifacts/schema_card.json \
  output/artifacts/resolved_instances.json \
  --output-dir output
```

`resolve` 自动执行：来源白名单、quote/行号、稳定身份、Class 白名单、domain/range、XSD lexical value、重复事实和 `max_count` 冲突检查。冲突值全部拒绝，不按出现顺序选择赢家。

## Role 6: QA 审查员（3-Gate）

### Gate 1：架构与忠实性

Gate 1 是 `QA_GATE_1` Semantic Work Item。读取其只读 `review_bundle`，并提交符合
`references/qa-gate1-output.schema.json` 的 strict JSON；不得附加置信度、patch 或自由字段。

- Schema Card 是否覆盖全部可回答 CQ，且没有无证据术语？
- 旧 Ontology Project/Term 身份是否按规则保留？
- ABox 是否只包含原文明示的现实实体/事实？
- 同一业务 ID 是否合并，无 ID 同名实体是否保持来源隔离？
- `evidence.jsonl` 是否覆盖每个 admitted entity/assertion？
- `rejections.jsonl` 是否明确记录拒绝原因，是否误拒绝有效事实？

### Gate 2：RDF 语法与 OWL-RL（Python）

```bash
python3 <skill-root>/scripts/validate.py output/ontology.owl
```

Gate 2 只有脚本 RDF 语法和 OWL-RL 均 PASS 时通过。失败立即停止本轮，不创建 Fixer。

### Gate 3：输出约束（Python）

Gate 3 由 Python 对权威 Schema Card、static/dynamic SHACL 和完整 OWL bundle 独立验证：

- `ontology.owl` 恰好一个 `owl:Ontology`，无 blank node、复杂类、自定义注解或未声明谓语。
- NamedIndividual 至少有一个已声明 Class 类型与 label。
- Object/Data assertion 满足 Schema Card domain/range 与 XSD。
- `schema.owl + instances.owl` 的三元组与 `ontology.owl`（除 ontology 声明）一致。
- OWL 中没有证据、置信度或来源路径。

Gate 1 只返回结构化结果：

```json
{
  "version": 1,
  "round": 1,
  "status": "FAIL",
  "findings": [
    {"reason_code": "SCHEMA_TERM_MISSING", "target": "SCHEMA_CARD", "detail": "..."}
  ]
}
```

Python 是 `qa_report.json` 的唯一汇总者。`ABOX_CHUNK` 必须引用 expected terminal chunk；
`PASS` 必须没有 findings，`FAIL` 必须有 findings，且 findings 必须唯一并按契约排序。

## Role 7: 修复专家

1. 只有 Gate 1 findings 全部唯一定位到同一个 `CQ`、`SRD` 或 `SCHEMA_CARD` 时，才创建一个 `FIXER` Semantic Work Item。
2. 提交符合 `references/fixer-output.schema.json` 的完整 replacement；replacement digest 必须变化。
3. 禁止修改或 patch candidates、Critic、Coverage、resolved、evidence、rejections 或三个 OWL 文件。
4. CQ replacement 使 SRD 及其下游失效；SRD replacement 使 Schema Card 及其下游失效；Schema Card replacement 创建新 lock，并以新 budgets 重跑全部 ABox work。
5. 每个 replacement 后完整重建，再回 Role 6 重跑三个 Gate。

QA 最多 20 轮。Gate 2/3 failure、terminal ABox finding、incomplete Coverage、重复 findings、
无合法 target、无变化 replacement 或第 20 轮 FAIL 都立即停止。只有可解析 OWL 才允许
`FORCED_WITH_ERRORS`；不可解析或 state ledger 漂移必须 `FAILED`，绝不能伪装为 PASS。

## 交付报告

```markdown
## 交付报告
**文件**: output/ontology.owl
**状态**: PASS | FORCED_WITH_ERRORS
**统计**: Class=N, ObjectProperty=N, DatatypeProperty=N, NamedIndividual=N, Assertion=N
**证据/拒绝**: admitted evidence=N, rejection=N
**CQ 数量**: N
**QA 循环**: N
**Gate 1/2/3**: PASS | FAIL
**中间产物**: output/artifacts/
```
