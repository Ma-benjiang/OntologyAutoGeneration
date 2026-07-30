# GitHub 上的 LLM TBox/ABox 自动生成项目调研

调研日期：2026-07-17

## 结论摘要

没有发现一个成熟开源项目能够原样满足以下全部要求：

> 指定多个 Markdown 文件 → 面向大文档的两种分块视图 → 从文档动态归纳 TBox → 按该 TBox 抽取 ABox → 保守实体消歧与事实准入 → TBox+ABox 合并为单个 RDF/XML OWL → RDF/OWL/SHACL 校验 → 证据旁路保存。

扩展搜索后，最完整的工程参考变为 [growgraph/ontocast](https://github.com/growgraph/ontocast)：它已实现文档结构/语义分块、ontology/facts 并行 map-reduce、critic、跨块实体对齐、RDF 1.2 provenance 和 Turtle 输出。它是本轮唯一足以改变候选排序的新发现，但其 per-chunk TBox/ABox 共演化、缓存、配置和三元组存储架构都比当前 Skill 重，不能原样复用。

[SenolIsci/mykg](https://github.com/SenolIsci/mykg) 仍是**最接近当前轻量严格两遍流程**的蓝本：先归纳全局 schema，再按 schema 抽取实例，具有稳定 ID、跨文件合并、TBox+ABox 同图输出和较完整测试。但源码实际输出是 **RDFS TBox + RDF ABox 的 Turtle**，不是 README 所称的严格 OWL：类使用 `rdfs:Class`，对象/数据属性都使用 `rdf:Property`，实例也不声明 `owl:NamedIndividual`。

推荐组合不是拼装多个大型框架，而是：

1. 以 myKG 的严格两遍管线、schema 白名单、稳定 ID 和失败分块处理为主骨架。
2. 移植 OntoCast 的 ontology/facts map-reduce、critic、跨块实体对齐和非法 literal 隔离思路，但不引入其平台运行时。
3. 以 OntoRAG 的 DTO、Schema Card 和分块提案归并方式为中间数据设计参考。
4. 以 Docling Graph 的确定性溯源、身份字段和合并账本为溯源/实体归并参考。
5. 以 Structured Decomposition 的“实体识别 → 断言抽取 → 推理验证”拆分 ABox 阶段。
6. 直接复用 Text2KGBench 的 ontology conformance、relation/entity hallucination 等评测定义及许可允许的数据集。
7. 保留现有 RDFLib、OWL-RL、pySHACL；自行实现薄的 RDF/XML OWL 序列化和项目特有准入规则。

## 筛选方法

使用 GitHub 仓库搜索与代码搜索。第一轮围绕以下查询扩展同义词：

```text
"TBox" "ABox" LLM ontology generation text extraction
ontology population instance extraction LLM OWL RDF pipeline
"schema induction" "fact extraction" OWL RDF knowledge graph
LLM ontology generation OWL
LLM ontology population
text to OWL LLM
```

筛选时要求至少命中下列一项：动态 schema/TBox 归纳、按已有本体抽取实例、OWL/RDF 输出、面向文本的两阶段知识图谱构建。随后逐仓库核对 README、核心源码、测试、许可证、Release 和最近更新；不能由源码确认的 README 宣称不计为已实现。

成熟度是截至调研日的快照，star 仅作辅助信号；判断更看重版本发布、测试、许可和源码完整度。

## 扩展搜索覆盖与新增发现

本轮先逐条执行精确短语的 GitHub repository search，再执行 GitHub code search；精确短语无结果时，按“去引号 → 去掉过严限定词 → 拆成能力词 → 用源码符号反查”的顺序放宽。精确仓库搜索多数返回空集，说明 `TBox`、`ABox`、`ontology population` 并不是项目 README 普遍使用的词；真正有效的命中主要来自放宽后的 repository search 和 OWL/RDF 源码符号反查。

### 搜索组与代表命中

| 搜索组 | 已执行查询 | 放宽/反查策略 | 代表命中与处理 |
|---|---|---|---|
| 动态 TBox | `"ontology learning" LLM`、`"ontology induction" LLM`、`"ontology engineering" LLM`、`"taxonomy discovery" LLM`、`"schema generation" "knowledge graph" LLM`、`"competency question" ontology LLM` | 去引号；改搜 `ontology learning LLM`、`schema generation knowledge graph LLM`；从 `owl:Class`、`ObjectProperty` 和 CQ 源码反查 | 新增 OntoCast、OLAF、OntoLearner、shapespresso、OntoConnectLM、OntoExtract；LLMs4OL 为重复命中 |
| ABox 抽取 | `"ontology population" LLM`、`"instance extraction" ontology`、`"individual extraction" OWL`、`"assertion extraction" ontology`、`"schema-guided extraction" RDF`、`"ontology-guided information extraction" LLM` | 去引号；改搜 fixed ontology、typed triples；用 `owl:NamedIndividual`、`OWLNamedIndividual`、Owlready2 反查 | 新增 KnowledgeGraphBuilder、Viewsari、Battery extractor；Structured Decomposition、Ontology Population Paper、CIDOC CRM 为重复命中 |
| 端到端 | `"document to knowledge graph" ontology`、`"text to knowledge graph" "schema induction"`、`"knowledge graph construction" "dynamic schema" LLM`、`"structured extraction" OWL RDF`、`"semantic graph extraction" documents` | 去掉过严组合，改搜 document/KG/ontology；从 README 所称 ontology 回查实际模型和 serializer | 新增 OntoCast、Docs2KG、Vertical AI；OpenSPG 等 property-graph 平台因无 OWL TBox/ABox 主链路排除 |
| 代码反查 | `"owl:NamedIndividual" OpenAI`、`OWLNamedIndividual LLM`、`rdflib ObjectProperty LLM`、`owlready2 "ontology population"`、`pyshacl LLM ontology`、`LinkML "ontology extraction"` | 搜索 literal/class 名称而非项目描述，并逐个打开命中源码 | 发现 Vertical AI、OntoConnectLM、ontology-extractor；owlapy/OWLAPI 等基础库因不负责文档抽取排除 |
| 治理子能力 | ontology alignment、entity resolution、coreference resolution、provenance extraction 与 LLM/KG/ontology 组合 | repository search 后继续搜源码；再从候选的 tests、CHANGELOG、论文反查实现 | 新增 OntoCast EntityAligner、Viewsari、KnowledgeGraphBuilder、DeepOnto、Open Ontologies；后两者归为基础设施/子能力 |

所有查询都继续做了代码搜索。自然语言组合的 code search 噪声很高，常命中论文列表、`llms.txt` 或普通文档；因此只有同时在 README、核心源码、测试、输出样例、许可证或论文中得到交叉证实的仓库，才进入详细分析。

### 与第一轮相比真正新增了什么

- **改变排序：OntoCast。** [workflow 文档](https://github.com/growgraph/ontocast/blob/main/docs/user_guide/workflow.md)、[pipeline 创建代码](https://github.com/growgraph/ontocast/blob/main/ontocast/stategraph/create.py)、[EntityAligner](https://github.com/growgraph/ontocast/blob/main/ontocast/tool/agg/entity_aligner.py) 和 [CHANGELOG](https://github.com/growgraph/ontocast/blob/main/CHANGELOG.md) 共同确认 ontology/facts 双循环、并行 map-reduce、critic、跨块实体对齐及 RDF provenance，不只是 README 宣称。
- **补强 ABox 工程参考：KnowledgeGraphBuilder。** 源码确认固定 OWL 引导抽取、CQ discovery、coreference/ensemble、SHACL 生成、pySHACL 与 RDF 导出；但它不负责从同一批文档先动态生成 TBox。
- **补强 OWL 序列化参考：OLAF 与 OntoConnectLM。** 两者都能生成 OWL class/property/individual；OLAF 将 linguistic realisation 当 individual 的做法不能视为忠实业务 ABox，OntoConnectLM 又缺少大文档分块和全局实体治理。
- **补强 provenance/mention 建模参考：Viewsari。** 它有固定 OWL 2 DL、本体引导 NER/EL、coreferent mention、PROV-O 和大规模 RDF KG，但领域和本体固定，且证据写图方式与当前旁路 `evidence.jsonl` 决策不同。
- **README 宣称被源码降级：Docs2KG。** [Ontology 模型](https://github.com/AI4WA/Docs2KG/blob/develop/Docs2KG/utils/models.py) 只是 `entity_types`/`relation_types` 列表，[entity_type_llm.py](https://github.com/AI4WA/Docs2KG/blob/develop/Docs2KG/kg_construction/semantic_kg/ontology/entity_type_llm.py) 只更新 JSON 类型集合；源码未见正式 OWL/RDF serializer。它不应因 README 的“ontology + unified KG”进入核心候选。

### 去重与排除记录

- `LLMs4OL`、Ontology Population Paper、CIDOC CRM extractor、Structured Decomposition、OntoGPT 等是第一轮重复命中，不重复计数。
- DeepOnto、OWLAPI、Owlapy、Open Ontologies 是 ontology reasoning/alignment/validation 基础设施，不是文档生成器；其中 Open Ontologies 可作为未来 QA backend 评估，但第一版已有 RDFLib/OWL-RL/pySHACL。
- shapespresso 从既有 Wikidata/YAGO 实例生成 ShEx schema，顺序和目标不同；OntoLearner 主要是 ontology-learning benchmark/library，不是业务文档生产管线。
- `arango-solutions/arango-ontoextract` 当前实现的是文档到 TBox 的审核/版本化；仓库自己的 ABox/CQ 文档仍是实施计划，不能计为已完成端到端能力。
- [`D2KLab/llm4ke`](https://github.com/D2KLab/llm4ke) 主要是已有 OWL → competency questions 的 LangChain/Ollama 工具；[`fabio-rovai/open-ontologies`](https://github.com/fabio-rovai/open-ontologies) 是 Rust OWL/RDF/SHACL/SPARQL/MCP 基础设施，二者都不做业务 Markdown → 动态 TBox → ABox。
- `DeviFAU/ontology-extractor` 从 ontology diagram 图片抽 TBox+ABox，OWL serializer 和评测较完整，但输入不是业务文本；仅作为 serializer/评测测试样例。
- 玩具仓库、课程作业、仅 prompt/chat UI、无源码论文列表、fork、以及只输出 Neo4j/property graph 而无 RDF/OWL 语义的项目进入筛选记录，不展开。

本节是可复核的**代表性搜索**，不是穷尽式系统综述。GitHub 索引、仓库命名和新项目持续变化，不能据此声称覆盖全部实现；但连续扩词后新增项目已落入“动态 TBox”“固定 TBox ABox”“实体治理”“OWL/SHACL 基础设施”等已有能力类别，没有再出现新的完整架构类型。

## 总览对比

符号：`是`=源码确认；`部分`=只覆盖一部分或语义较弱；`否`=没有；`固定`=依赖预先给定 TBox。

| 项目 | 文本输入 | 动态 TBox | 按 TBox 抽取 ABox | 两阶段 | OWL/RDF 输出 | 分块 | 消歧/合并 | 验证/溯源 | 结论 |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| [OntoCast](https://github.com/growgraph/ontocast) | PDF/Markdown/JSON/文本 | 是，按单元 map/reduce 共演化 | 是，facts loop | 部分：ontology/facts 并行而非先锁定全局 TBox | RDF/Turtle；RDF 1.2 provenance | 结构/语义 chunk | EntityAligner：embedding+符号对齐 | critic、结构检查、RDF store | **重点移植** |
| [myKG](https://github.com/SenolIsci/mykg) | Markdown 等 | 是 | 是 | 是，TBox→ABox | 部分：RDFS/RDF Turtle | 固定 token 滑窗 | 类型+名称稳定 ID、别名归一 | schema 校验；文件级来源 | **移植** |
| [OntoRAG](https://github.com/ontorag/ontorag) | PDF/Markdown 等 | 是 | 是 | 是，TBox→ABox | OWL TBox + RDF ABox Turtle | Markdown 标题/PageIndex/LlamaIndex | 仅名称去重；实例 IRI 含 chunk | DTO/mention 溯源；校验较弱 | **移植** |
| [KnowledgeGraphBuilder](https://github.com/DataScienceLabFHSWF/KnowledgeGraphBuilder) | PDF/DOCX/PPTX/XML/MD/TXT | 否，固定 OWL | 是 | 否，ontology-guided ABox | RDF/Turtle/JSON-LD 等 | 语义分块+Qdrant | coreference、ensemble、consensus | SHACL/pySHACL、OWL-RL、质量分数 | **移植 ABox** |
| [OLAF](https://github.com/wikit-ai/olaf) | 文本 | 是 | 部分：linguistic realisations 被转为 individuals | 模块化学习流程 | OWL/RDFLib，Turtle/XML | 预处理模块 | 未见业务实体全局消歧 | axiom extraction、评测 | **移植 TBox/序列化** |
| [OntoConnectLM](https://github.com/IRT-SystemX/OntoconnectLM) | 文本/可选 CQ | 是 | 是，typed triples→individuals | 函数式多步 | OWL/RDF/XML/Turtle | 小型输入列表 | 未见大文档全局消歧 | ontology evaluator | **参考** |
| [Viewsari](https://github.com/ISE-FIZKarlsruhe/viewsari) | 数字人文文本 | 否，固定 OWL 2 DL | 是 | 固定本体 population | OWL/RDF Turtle | 段落/文本块 | ObliquER entity linking + coreference | PROV-O、CQ/SPARQL、推理 | **参考 provenance** |
| [Docs2KG](https://github.com/AI4WA/Docs2KG) | PDF/DOCX/HTML/EPUB | 部分：JSON 类型列表 | 是，property graph NER | 部分 | Neo4j/JSON；未见正式 OWL/RDF | 句号切分 | hash/UUID，未见稳定语义 ID | 人工协作指标 | **降级参考** |
| [brains-group pipeline](https://github.com/brains-group/towards_automated_ontology_generation) | PDF | 是 | 是，和 TBox 同步生成 | 页面增量，不是严格两遍 | OWL Turtle | PDF 按页 | 未见可靠全局实体消歧 | RDFLib + Owlready/HermiT | **参考** |
| [Docling Graph](https://github.com/docling-project/docling-graph) | Markdown/PDF/Office 等 | 是，生成 Pydantic schema | 是，按模板抽取 | 是，schema→数据 | 否：NetworkX/JSON/CSV/Cypher | 结构化分块 | 身份字段+确定性合并 | 很强的节点/块/页溯源 | **移植** |
| [AutoSchemaKG](https://github.com/HKUST-KnowComp/AutoSchemaKG) | JSON 文本；MD 需转换 | 是，概念化 | 先抽事实再归纳 schema | 是，但顺序相反 | 否：CSV/GraphML/Neo4j | 字符滑窗 | 大规模概念归并 | KG/事实评测，非 OWL QA | **参考** |
| [OntoGPT](https://github.com/monarch-initiative/ontogpt) | 文本 | 否，需 LinkML 模板 | 固定 | 否 | 是：OWL/Turtle | 句子/字符 | ontology grounding | span、模板校验 | **移植 ABox 子能力** |
| [Structured Decomposition](https://github.com/albsadowski/structured-decomposition-swj) | 文本/CSV | 否 | 固定 | 是，实体→断言 | ABox Turtle | 否 | 案例内实体映射 | Pellet/SWRL 一致性 | **移植 ABox 子能力** |
| [Ontology Population Paper](https://github.com/iocroblab/Ontology_population_paper) | 文本 | 否 | 固定 | 多步 ABox | 合并后 OWL | 否 | embedding+可选人工消歧 | domain/range/disjoint 检查 | **参考** |
| [MASEO](https://github.com/oeg-upm/maseo) | CQ JSON | 是 | 否 | 多代理 TBox QA | RDF/XML OWL | 不适用 | 不适用 | RDFLib+HermiT+OOPS | **参考 TBox QA** |
| [OntoSphere](https://github.com/boricles/ontosphere) | PDF | 是 | 未见独立 ABox population | 否 | TTL/JSON-LD/RDF/XML | 文档处理 | schema 版本/兼容检查 | SHACL+来源 | **参考产品形态** |
| [Text2KGBench](https://github.com/cenguix/Text2KGBench) | 单句 | 否 | 固定本体的事实抽取评测 | 否 | 数据含 OWL/TTL；预测为 triples | 否 | 否 | conformance/幻觉/P-R-F1 | **直接复用评测** |
| [OntologyAutoGeneration](https://github.com/liuhuanyong/OntologyAutoGeneration) | MD/PDF/HTML/TXT 等 | 是 | 否 | 否 | 否：Ontology JSON | 标题/段落/固定 | Union-Find 概念合并 | 结构一致性检查 | **参考 TBox 算法** |
| [OntoGenix](https://github.com/tecnomod-um/OntoGenix) | CSV | 是 | 通过 RML 物化 | TBox→mapping→KG | OWL/RDF+RML | CSV 行/列 | 未见文本实体消歧 | RDFLib 解析 | **排除主流程** |
| [CIDOC CRM LLM Extractor](https://github.com/lias-laboratory/cidoccrm-llm-extractor) | CSV | 否 | 固定 CIDOC CRM | 否 | JSON-LD 文本 | 固定行数 | 否 | 主要依赖 prompt | **参考 ontology subset prompting** |

### 许可证与成熟度快照

测试数按仓库中的 `test_*.py` 文件粗略统计，不等于测试用例数；仅用于比较工程化程度。

| 项目 | 许可证 | Release/版本 | 测试文件 | 调研时 stars | 成熟度判断 |
|---|---|---|---:|---:|---|
| OntoCast | Apache-2.0 | v0.4.3；PyPI；2026-07 活跃 | 约 63 | 148 | 本轮新增最完整工程参考，但依赖配置/缓存/图存储，非轻量 Skill |
| myKG | MIT | v0.3.25 | 约 63 | 49 | 最接近当前轻量严格两遍流程，但 OWL 输出语义不足 |
| OntoRAG | Apache-2.0 | 无 Release；0.1.0 | 0 | 13 | 早期原型 |
| KnowledgeGraphBuilder | MIT | 2026-06 活跃；无正式 Release | 约 120 | 4 | ABox/SHACL 工程较完整，但固定 OWL、平台依赖重 |
| OLAF | Apache-2.0 | setup 0.0.1；无 Release | 约 10 | 12 | 学术框架；TBox/OWL 序列化有价值，ABox 语义有限 |
| OntoConnectLM | MPL-2.0 | v1.1.1 | 约 6 个 notebook | 2 | 可复用 OWL/ABox 小组件，非大文档管线 |
| Viewsari | 未识别 | dev 分支；无 Release | 未统计 | 3 | 固定领域大型 provenance KG，适合模式参考 |
| Docs2KG | Apache-2.0 | v0.3.5；PyPI | 测试很少 | 370 | README 完整但源码只是 JSON/property graph ontology，降级为参考 |
| KGs_for_Vertical_AI | MIT | 无 Release | 0 | 16 | 文本→TTL 实验，缺少稳定 ABox/实体治理 |
| brains-group pipeline | 未声明 | 无 Release | 0 | 4 | 研究原型，不宜复制代码 |
| Docling Graph | MIT | v1.8.0 | 约 108 | 174 | 工程成熟，但目标模型不是 OWL |
| AutoSchemaKG | MIT | 无 Release；0.0.5.post1 | 核心目录约 9 | 784 | 研究/大规模 KG 工程较成熟，非 OWL 管线 |
| OntoGPT | BSD-3-Clause | v1.1.1 | 约 27 | 934 | 成熟的 schema-guided extraction 工具 |
| Structured Decomposition | MIT | 无 Release | 0 | 3 | 新研究实现，范围窄 |
| Ontology Population Paper | 未声明 | 无 Release | 0 | 0 | 学术样例 |
| MASEO | Apache-2.0 | GitHub v1.2；包内 0.1.0 | 0 | 4 | 新研究原型，版本信息不一致 |
| OntoSphere | Apache-2.0 | README v0.4.0；无 GitHub Release | 约 4 | 38 | 新产品原型 |
| Text2KGBench | Apache-2.0（代码） | ISWC 2023 submission | 0 | 88 | 稳定 benchmark，不是生成器 |
| OntologyAutoGeneration | 未声明 | 无 Release | 约 7 | 33 | 新 TBox 原型 |
| OntoGenix | GPL-3.0 | 1.1 | 0 | 39 | 有 GUI/样例，但维护停在 2024 且场景不同 |
| CIDOC CRM LLM Extractor | MIT | 无 Release | 0 | 9 | 固定领域研究代码 |

## 重点项目核验

### 0. growgraph/ontocast：本轮最重要新增工程参考

一手证据：

- [README](https://github.com/growgraph/ontocast/blob/main/README.md) 和 [workflow guide](https://github.com/growgraph/ontocast/blob/main/docs/user_guide/workflow.md) 说明文档先转 Markdown，再进行结构/语义分块；每个单元运行 ontology/facts loop，最终聚合和序列化 RDF。
- [stategraph](https://github.com/growgraph/ontocast/tree/main/ontocast/stategraph) 与 [agent modules](https://github.com/growgraph/ontocast/tree/main/ontocast/agent) 提供 ontology/facts 的 render、critic、normalize/consolidate、retry/map-reduce 组件。
- [EntityAligner](https://github.com/growgraph/ontocast/blob/main/ontocast/tool/agg/entity_aligner.py) 使用类型/角色/别名兼容性、符号匹配和 embedding 对齐跨块实体；[aggregate](https://github.com/growgraph/ontocast/blob/main/ontocast/tool/agg/aggregate.py) 负责连通分量合并。
- [facts models](https://github.com/growgraph/ontocast/blob/main/ontocast/onto/facts.py) 和 [serialize](https://github.com/growgraph/ontocast/blob/main/ontocast/agent/serialize.py) 确认 facts 进入 RDF/Turtle；[CHANGELOG](https://github.com/growgraph/ontocast/blob/main/CHANGELOG.md) 记录 RDF 1.2 quoted-triple/reification provenance、literal quarantine 和跨单元对齐。
- [Apache-2.0](https://github.com/growgraph/ontocast/blob/main/LICENSE)、[v0.4.3](https://github.com/growgraph/ontocast/releases/tag/v0.4.3)、约 63 个测试文件、调研时 148 stars；代码在 2026-07 仍有提交。

关键差距：

- 默认 `RENDER_MODE=ontology_and_facts`，ontology 与 facts 按单元并行共演化；这不是当前要求的“先全局锁定 TBox，再只按 TBox 抽 ABox”。
- 输出重点是 Turtle/graph store，不是本 Skill 要交付的单个 RDF/XML OWL；默认连接 Fuseki/pyoxigraph，并有 tenancy、Qdrant、LangGraph、环境配置等重依赖。
- LLM response 和 chunking 默认启用缓存，与“每次完整重建、不做缓存”冲突；provenance 也默认进入 RDF 相关结构，与“证据旁路、不进 OWL”冲突。
- facts 默认允许 domain ontology 中的 reference individuals，并使用 `cd:` 实例命名空间；当前方案不考虑上传基准本体。

结论：将 OntoCast **提升为工程实现第一参考**，移植其 map-reduce、critic、EntityAligner、literal quarantine 和失败单元处理；不直接依赖其运行时。myKG 仍负责当前“小 Skill + 严格 TBox→ABox”的最短实现路径。

### 0.1 DataScienceLabFHSWF/KnowledgeGraphBuilder：固定 OWL 的 ABox 参考

[README](https://github.com/DataScienceLabFHSWF/KnowledgeGraphBuilder/blob/main/README.md) 与源码确认：输入 OWL + 文档 + 可选 competency questions；语义分块后按 ontology 生成提示，运行 rule-based/LLM ensemble、coreference、consensus 和质量过滤，生成 SHACL shapes，执行 pySHACL/可选 OWL-RL，并导出 RDF/Turtle、JSON-LD、Cypher 等。它还有迭代 discovery loop 和 checkpoint。

它的优点是 ABox 生产、实体/关系合并、验证和质量打分很完整；缺点是 TBox 必须预先存在，运行依赖 Neo4j、Qdrant、Fuseki、Ollama 和配置文件，置信度策略也不同于当前“确定性准入 + 冲突拒绝”。结论：**移植 ABox 骨架和验证测试，不移植平台**。

### 0.2 wikit-ai/olaf 与 IRT-SystemX/OntoconnectLM：OWL/TBox 小组件

[OLAF README](https://github.com/wikit-ai/olaf/blob/main/README.md)、[OWL serializer](https://github.com/wikit-ai/olaf/tree/main/olaf) 和 demo 确认它是模块化 ontology-learning framework，能用 RDFLib 生成 `owl:Class`、`owl:ObjectProperty`、`owl:DatatypeProperty` 并序列化 Turtle/XML。demo 的 `concept_lrs_to_owl_individuals` 会把 linguistic realisation 当 NamedIndividual，并对关系两端 realization 做笛卡尔积；这不是忠实业务 ABox，因此只移植 TBox/序列化测试思路。Apache-2.0、setup 0.0.1、约 10 个测试文件。

[OntoConnectLM README](https://github.com/IRT-SystemX/OntoconnectLM/blob/main/README.md) 与 notebook 代码确认文本/可选 CQ → classes，文本 → typed triples，再由 classes/properties/triples 生成包含 `owl:NamedIndividual` 和 `owl:ObjectProperty` 的 OWL/RDF。它适合参考 ABox serializer 和 CQ/ontology evaluator，但示例是小型列表函数，不含大 Markdown 分块、全局消歧、旁路证据或严格两遍编排。MPL-2.0、v1.1.1、约 2 stars。

### 0.3 Viewsari：provenance-aware fixed-ontology population

[README](https://github.com/ISE-FIZKarlsruhe/viewsari/blob/dev/README.md) 和 [KG population 目录](https://github.com/ISE-FIZKarlsruhe/viewsari/tree/dev/src/kg_population) 确认固定 OWL 2 DL、ObliquER LLM NER/entity-linking、显式/隐式/coreferent/generic mention 类型、PROV-O activity reification、CQ/SPARQL evaluation 和大规模 Turtle KG。它展示了如何把 mention、referent、prompt、model/run 和每条 assertion 的解释活动建模为 provenance。

它不能作为当前主流程：领域 ontology 固定，源仓库含 GraphDB、Git LFS 和大量预计算数据，且 provenance 写入图而不是 `evidence.jsonl` 旁路。结论：**只参考 mention/entity/provenance 数据模型**。

### 0.4 AI4WA/Docs2KG：README 与源码不一致，明确降级

[README](https://github.com/AI4WA/Docs2KG/blob/develop/README.md) 和 [v0.3.5 release](https://github.com/AI4WA/Docs2KG/releases/tag/v0.3.5) 宣称 bottom-up/top-down unified KG、ontology 和人工协作；但 [Ontology model](https://github.com/AI4WA/Docs2KG/blob/develop/Docs2KG/utils/models.py) 只有 `entity_types`/`relation_types` 两个 JSON 列表，[entity type generator](https://github.com/AI4WA/Docs2KG/blob/develop/Docs2KG/kg_construction/semantic_kg/ontology/entity_type_llm.py) 只把 LLM 返回的类型集合并回 JSON，[NER](https://github.com/AI4WA/Docs2KG/blob/develop/Docs2KG/kg_construction/semantic_kg/ner/ner_prompt_based.py) 按句号切分并输出 property-graph JSON。

源码未见正式 OWL/RDF serializer、OWL class/property/domain/range、`owl:NamedIndividual` 或稳定跨文档 IRI。因此它只能参考文档结构化、人机协作和类型白名单，不能计为动态 TBox+ABox 候选；这是本轮最典型的 README→源码降级案例。

### 1. SenolIsci/mykg：最接近目标，适合移植而非直接依赖

一手证据：

- [README](https://github.com/SenolIsci/mykg/blob/main/README.md) 明确描述 Pass 1 归纳全局 schema、Pass 2 按 schema 抽取实例。
- [pass1.py](https://github.com/SenolIsci/mykg/blob/main/src/mykg/pass1.py) 对分块进行 schema proposal，并按 token 预算组成批次。
- [pass2.py](https://github.com/SenolIsci/mykg/blob/main/src/mykg/pass2.py) 将 schema 注入抽取提示，并在代码中拒绝未知 node type、edge type 和悬空端点。
- [chunker.py](https://github.com/SenolIsci/mykg/blob/main/src/mykg/chunker.py) 实际使用 tiktoken 固定窗口和重叠，没有 Markdown 标题感知，也没有 TBox/ABox 双视图。
- [ids.py](https://github.com/SenolIsci/mykg/blob/main/src/mykg/ids.py) 使用 `type + normalized name` 生成稳定 ID；[assembler.py](https://github.com/SenolIsci/mykg/blob/main/src/mykg/assembler.py) 跨文件合并同 ID 节点、去重边和聚合来源。
- [exporter.py](https://github.com/SenolIsci/mykg/blob/main/src/mykg/exporter.py) 在一个 Turtle 图中先写 TBox、再写 ABox。
- [schema_validator.py](https://github.com/SenolIsci/mykg/blob/main/src/mykg/schema_validator.py) 用 RDFLib 检查语法及 domain/range/parent 引用。
- [tests](https://github.com/SenolIsci/mykg/tree/main/tests) 当前有约 63 个 `test_*.py` 文件；[v0.3.25 Release](https://github.com/SenolIsci/mykg/releases/tag/v0.3.25)；[MIT License](https://github.com/SenolIsci/mykg/blob/main/LICENSE)。调研时约 49 stars。

README 与源码的关键差异：

| README/表面印象 | 源码实际行为 | 对当前 Skill 的影响 |
|---|---|---|
| “RDFS/OWL ontology” | 类为 `rdfs:Class`；所有属性为 `rdf:Property` | 不能满足严格区分 `owl:ObjectProperty` / `owl:DatatypeProperty` |
| “OWL toolchains” | Turtle 中没有 `owl:NamedIndividual`，也不输出 RDF/XML | 需要重写确定性序列化器 |
| schema-guided | 确实检查 type/property 名称和边端点 | 这部分值得移植 |
| confidence-scored | 冲突属性按 confidence 选取；两个 1.0 字符串甚至会拼接 | 与当前“冲突事实全部拒绝”规则冲突 |
| stable IDs | `type + name` 全局合并 | 对无业务编号、同名不同人可能过度合并 |
| provenance | 节点/边保留 `source_files`，另有 chunk index | 没有每条事实的精确 quote/行号旁路证据 |
| bring your own ontology | README 也注明普通 base schema 约束由 LLM 控制，只有 freeze 模式严格 | 当前第一版已排除基准本体，可不移植 |

适合移植：两遍流程、schema proposal 归并、Pass 2 代码级白名单校验、失败 chunk 日志、稳定 ID 的框架、边去重、测试分层。

不应照搬：目录+配置驱动入口、token 滑窗、confidence 冲突仲裁、跨文件同名自动合并、RDFS exporter、缓存/append/session/MCP 等平台能力。

### 2. OntoRAG：中间层设计非常贴近，但实现仍偏早期

一手证据：

- [README](https://github.com/ontorag/ontorag/blob/main/README.md) 的主链路是 Baselines → DTOs → Ontology → Instances → RDF。
- [dto.py](https://github.com/ontorag/ontorag/blob/main/ontorag/dto.py) 提供内容寻址的 DocumentDTO/ChunkDTO 和页码、章节、offset、snippet。
- [extractor_ingest.py](https://github.com/ontorag/ontorag/blob/main/ontorag/extractor_ingest.py) 对 Markdown 本地按标题拆分，也支持其他 ingestion engine。
- [ontology_extractor_openrouter.py](https://github.com/ontorag/ontorag/blob/main/ontorag/ontology_extractor_openrouter.py) 逐 chunk 提出类/属性/事件候选；[schema_card.py](https://github.com/ontorag/ontorag/blob/main/ontorag/schema_card.py) 再确定性归并。
- [instance_extractor_openrouter.py](https://github.com/ontorag/ontorag/blob/main/ontorag/instance_extractor_openrouter.py) 要求只能使用 Schema Card 中的类和属性。
- [proposal_to_ttl.py](https://github.com/ontorag/ontorag/blob/main/ontorag/proposal_to_ttl.py) 正确区分 OWL Class/ObjectProperty/DatatypeProperty；[instances_to_ttl.py](https://github.com/ontorag/ontorag/blob/main/ontorag/instances_to_ttl.py) 生成 RDF ABox 和 mention provenance。
- [Apache-2.0 License](https://github.com/ontorag/ontorag/blob/main/LICENSE)。版本为 `0.1.0`，无正式 Release，仓库未见测试目录；调研时约 13 stars。

实际缺口：实例 IRI 由 `class + label + chunk_id` 哈希生成，同一实体跨 chunk 会天然分裂；数据属性统一 `Literal(str(value))`，没有按 Schema Card range 转 XSD；实例序列化器没有代码级核验 class/property/domain/range；mention 以 blank node 和自定义谓语写进图，与当前“证据不进 OWL”相反。

结论：移植 DTO、Schema Card、proposal merge 的数据形状；不要直接复用实例 IRI、literal 转换和 provenance RDF。

### 3. brains-group/towards_automated_ontology_generation：与现有 Skill 原型最像

一手证据：

- [README](https://github.com/brains-group/towards_automated_ontology_generation/blob/main/README.md) 给出 CQ → SRD → TIP → Turtle → QA/Fixer 的多代理流程。
- [multi_agent.py](https://github.com/brains-group/towards_automated_ontology_generation/blob/main/agents/multi_agent.py) 实际按 PDF page 逐页运行，并在同一 Turtle 文件增量编辑。
- [示例输出](https://github.com/brains-group/towards_automated_ontology_generation/blob/main/ontology/multi_qa_2.ttl) 同时包含 `owl:Class`/属性和 Policy、Person、State、Event 等实例，说明并非只生成 TBox。
- [syntax_checks.py](https://github.com/brains-group/towards_automated_ontology_generation/blob/main/tools/syntax_checks.py) 使用 RDFLib 语法检查、Owlready2/HermiT 逻辑检查。

差距：生命保险领域提示大量硬编码；先生成 CQ 且按页处理，不适合超大 Markdown 全局归并；LLM 直接编辑 Turtle，缺少唯一机器可读 schema source；没有自动化测试、无许可证、无 Release，调研时约 4 stars。

结论：作为现有流程来源和 QA/Fixer 设计参考，不应直接复制代码。

### 4. Docling Graph：schema induction、确定性合并和溯源做得最完整

一手证据：

- [README](https://github.com/docling-project/docling-graph/blob/main/README.md) 支持从文档经 LLM 生成 Pydantic template，再按 template 抽取图；也能从 OWL/RDFS/LinkML 生成 template。
- [templategen/induce](https://github.com/docling-project/docling-graph/tree/main/docling_graph/templategen/induce) 将文档 schema proposal 结构化后确定性渲染，不让 LLM 直接写代码。
- [document_chunker.py](https://github.com/docling-project/docling-graph/blob/main/docling_graph/core/extractors/document_chunker.py) 和 [chunking 文档](https://github.com/docling-project/docling-graph/blob/main/docs/fundamentals/extraction-process/chunking-strategies.md) 提供结构化分块。
- [provenance 文档](https://github.com/docling-project/docling-graph/blob/main/docs/fundamentals/graph-management/provenance.md) 与 [provenance 源码](https://github.com/docling-project/docling-graph/tree/main/docling_graph/core/provenance) 实现不额外调用 LLM 的 chunk/page/span 账本，并明确 unresolved 而不猜测位置。
- [graph_converter.py](https://github.com/docling-project/docling-graph/blob/main/docling_graph/core/converters/graph_converter.py) 依据 `graph_id_fields` 生成稳定身份并去重。
- [v1.8.0 Release](https://github.com/docling-project/docling-graph/releases/tag/v1.8.0)、[MIT License](https://github.com/docling-project/docling-graph/blob/main/LICENSE)、约 108 个测试文件；调研时约 174 stars。

差距：schema 是 Pydantic extraction model，不是完整 OWL TBox；输出是 NetworkX/JSON/CSV/Cypher，没有 OWL/RDF exporter；依赖重且第一版 Skill 已决定自行写 Markdown chunker。

结论：不引入整个框架；移植“身份字段优先”“确定性 provenance ledger”“unresolved 不猜测”“proposal→deterministic renderer”等设计。

### 5. AutoSchemaKG：动态 schema 很强，但模型不是 OWL TBox/ABox

一手证据：

- [README](https://github.com/HKUST-KnowComp/AutoSchemaKG/blob/main/README.md) 明确顺序是先抽实体/事件 triples，再通过 conceptualization 归纳 schema。
- [triple_extraction.py](https://github.com/HKUST-KnowComp/AutoSchemaKG/blob/main/atlas_rag/kg_construction/triple_extraction.py) 使用字符窗口抽取 entity relations、event entities、event relations，随后输出 CSV/GraphML。
- [concept_generation.py](https://github.com/HKUST-KnowComp/AutoSchemaKG/blob/main/atlas_rag/kg_construction/concept_generation.py) 对已抽取节点/关系生成多层抽象概念。
- [MIT License](https://github.com/HKUST-KnowComp/AutoSchemaKG/blob/main/LICENSE)，包版本 `atlas-rag 0.0.5.post1`，核心 `tests/` 约 9 个测试文件，无 GitHub Release；调研时约 784 stars。

差距：顺序是 ABox-like triples → schema，而当前方案要求 TBox v1 → 受约束 ABox；概念层是 property graph abstraction，不表达 OWL class/property/domain/range；无 OWL/RDF 输出。

结论：只参考大规模批处理、概念化和多语言经验，不作为 Skill 核心。

## ABox/ontology population 专项项目

### OntoGPT

[OntoGPT README](https://github.com/monarch-initiative/ontogpt/blob/main/README.md) 将 SPIRES 定义为基于模板和 ontology grounding 的结构化文本抽取；[spires_engine.py](https://github.com/monarch-initiative/ontogpt/blob/main/src/ontogpt/engines/spires_engine.py) 支持句子或字符分块并合并结果；[owl_exporter.py](https://github.com/monarch-initiative/ontogpt/blob/main/src/ontogpt/io/owl_exporter.py) 通过 LinkML-OWL 输出 OWL。它不是动态 TBox 归纳器，而是“先有 LinkML extraction schema，再抽实例”。项目成熟度高：[v1.1.1](https://github.com/monarch-initiative/ontogpt/releases/tag/v1.1.1)、[BSD-3-Clause](https://github.com/monarch-initiative/ontogpt/blob/main/LICENSE)、约 27 个测试文件、调研时约 934 stars。

可移植：schema-constrained extraction、ontology grounding、span 捕获和 OWL exporter 的测试案例。直接依赖会引入 LinkML/OAK 体系，且不能解决动态 TBox，第一版不建议。

### Structured Decomposition Framework

[README](https://github.com/albsadowski/structured-decomposition-swj/blob/main/README.md) 和 [evaluator.py](https://github.com/albsadowski/structured-decomposition-swj/blob/main/src/structured_decomposition_swj/evaluator.py) 明确把 ABox population 拆为实体识别、断言抽取、Pellet/SWRL 推理；[abox_builder.py](https://github.com/albsadowski/structured-decomposition-swj/blob/main/src/structured_decomposition_swj/abox_builder.py) 可持久化 Turtle。MIT，2026-07 仍活跃，约 3 stars，未见测试文件。

可移植：不要让一次 LLM 调用同时决定实体、属性和关系；先锁定实体表，再抽断言并做 domain/range/一致性校验。它使用人工编写 TBox，不负责动态 TBox。

### iocroblab/Ontology_population_paper

[README](https://github.com/iocroblab/Ontology_population_paper/blob/main/README.md) 与 [ontopopulator.py](https://github.com/iocroblab/Ontology_population_paper/blob/main/src/ontopopulator.py) 实现固定 OWL → 实体 → 属性 → 关系 → Owlready2 实例化；[ontology_manager.py](https://github.com/iocroblab/Ontology_population_paper/blob/main/src/ontology_manager.py) 检查 domain/range 与 disjoint，再保存合并后的 OWL。

差距：一次读取完整文本、无大文档分块；歧义时需要人工选择；没有自动测试；README 仍是许可证占位文本，GitHub 也未识别许可证；无 Release/几乎无社区使用。只适合作为固定 TBox population 的学术样例。

### 其他固定本体 population

- [Ontology-Guided-LLM-for-Battery-Information-Extraction](https://github.com/arslimane/Ontology-Guided-LLM-for-Battery-Information-Extraction)：固定电池 ontology，PDF datasheet → ontology-guided RDF-like triples → LLM validation/dedup；没有动态 TBox、正式 OWL serializer 或跨文档实体治理。适合参考 schema-guided prompt 和重复事实过滤。
- [CIDOC CRM LLM Extractor](https://github.com/lias-laboratory/cidoccrm-llm-extractor)：MIT；固定 CIDOC CRM，比较 full ontology 与 ontology subset prompting；输入实际是 CSV，输出只是把各 chunk 的 LLM JSON-LD 文本拼接，未见 RDF 解析校验或测试。适合参考“只把相关本体子集放进 prompt”，不适合复用主流程。
- [fghazouani/LLM-Ontology-Population](https://github.com/fghazouani/LLM-Ontology-Population)：固定 TetraOnto，提供 prompt 与 LoRA/QLoRA 抽取实验及大量 TTL 样例；无许可证、无正式测试，属于实验代码，排除直接复用。
- [ontology-req-pipeline](https://github.com/alessandrostefanone-polimi/ontology-req-pipeline)：MIT；[README](https://github.com/alessandrostefanone-polimi/ontology-req-pipeline/blob/main/README.md) 显示“需求分解 → QUDT 单位归一 → IOF/QUDT grounding → Pellet QA”。可参考术语 grounding、单位归一和每条需求独立 KG，但 TBox 固定且项目很新。

## TBox 生成、QA 与评测专项项目

### KGs_for_Vertical_AI：文本→TTL 的实验性两步样例

[README](https://github.com/tiagocrz/KGs_for_Vertical_AI/blob/main/README.md)、[txt_ontology_learning.py](https://github.com/tiagocrz/KGs_for_Vertical_AI/blob/main/src/txt_ontology_learning.py) 和 [build_kg.py](https://github.com/tiagocrz/KGs_for_Vertical_AI/blob/main/src/build_kg.py) 共同确认：先让 LLM 从文本生成 classes，再逐句提示生成 individuals/relations，并用 RDFLib 校验 Turtle；另有 chunk/no-chunk KG 对比实验。提示词明确要求 `owl:NamedIndividual`、`owl:ObjectProperty` 和 `owl:DatatypeProperty`，是本轮 code search 发现中最接近“先 TBox 再 ABox”的小样例。

但它把每句结果直接拼接为 TTL，未形成唯一 Schema Card，没有跨文档实体消歧、事实冲突账本、稳定 IRI 或正式测试；`build_kg.py` 的主图构建又依赖 LangChain property graph。MIT、16 stars、无 Release。结论：**只参考提示拆分和 RDFLib 校验，不作为工程依赖**。

### MASEO

[MASEO README](https://github.com/oeg-upm/maseo/blob/main/README.md) 与 [workflow.py](https://github.com/oeg-upm/maseo/blob/main/src/maseo/workflow.py) 实现 CQ → RDF/XML OWL → RDFLib syntax repair → HermiT consistency → OOPS pitfalls；还能把 agent rationale/source 写入本体。Apache-2.0，Release `v1.2`，但版本文件仍写 `0.1.0`，未见测试文件，调研时约 4 stars。

它不读业务文档、不抽 ABox，但 QA 分层和“LLM 修复后重新做确定性检查”值得参考。当前 Skill 已决定证据不进 OWL，因此不采用其 rationale 注解方式。

### OntoSphere

[README](https://github.com/boricles/ontosphere/blob/main/README.md)、[LLM service](https://github.com/boricles/ontosphere/blob/main/backend/app/services/llm_service.py)、[export service](https://github.com/boricles/ontosphere/blob/main/backend/app/services/export_service.py) 显示它从 PDF 抽类/属性/关系，支持 TTL/JSON-LD/RDF/XML 导入导出、版本 diff、兼容性检查和 SHACL。Apache-2.0，约 4 个测试文件，调研时约 38 stars，2026 年新项目。

源码未显示“先 TBox 再按 TBox 抽 ABox”的独立 population pass，图节点主要是 ontology classes/properties。它更适合参考产品界面、版本差异和 schema 兼容性，不是当前 Skill 的核心实现。

### liuhuanyong/OntologyAutoGeneration

[README](https://github.com/liuhuanyong/OntologyAutoGeneration/blob/main/README.md) 和 [pipeline.py](https://github.com/liuhuanyong/OntologyAutoGeneration/blob/main/ontology_gen/pipeline.py) 覆盖文档/DDL/query → 概念合并 → taxonomy → 属性/关系 → 公理/SWRL；[text_concept_extractor.py](https://github.com/liuhuanyong/OntologyAutoGeneration/blob/main/ontology_gen/stage2_concept/text_concept_extractor.py) 有 span 反幻觉规则；测试覆盖合并、环检测、domain/range 等。

但最终明确只输出 Ontology JSON，不输出 OWL/Turtle，也不生成 ABox；依赖配置目录，且没有许可证。可参考 Union-Find 概念合并、taxonomy 环检测、属性/关系分类，不直接移植代码。

### Text2KGBench

[README](https://github.com/cenguix/Text2KGBench/blob/main/README.md) 说明任务是“给定 ontology + sentence，抽取遵循 ontology 且忠实于文本的事实”；[evaluation README](https://github.com/cenguix/Text2KGBench/blob/main/src/evaluation/README.md) 与 [run_eval.py](https://github.com/cenguix/Text2KGBench/blob/main/src/evaluation/run_eval.py) 实现 precision/recall/F1、ontology conformance、relation hallucination、subject/object hallucination。项目包含 29 个 ontology、18,334 条句子；[Apache-2.0](https://github.com/cenguix/Text2KGBench/blob/main/LICENSE)，[ISWC 2023 release](https://github.com/cenguix/Text2KGBench/releases/tag/iswc_2023_submission)，调研时约 88 stars。

它不是生产生成器，也没有动态 TBox，但其评测定义和数据集可以直接用于当前 Skill 的 ABox 回归测试。注意其 `run_eval.py` 在计算主 P/R/F1 前会按 gold relations 过滤系统 triples，直接照搬可能高估生产效果；应复用指标定义和数据，不应不加修改地复制计算逻辑。仓库数据还包含各来源数据集自己的许可，复用时需逐项遵循。

### LLMs4OL

- [HamedBabaei/LLMs4OL](https://github.com/HamedBabaei/LLMs4OL)：MIT，约 177 stars；针对术语类型、taxonomy discovery、非 taxonomy 关系抽取的研究实现。
- [sciknoworg/LLMs4OL-Challenge](https://github.com/sciknoworg/LLMs4OL-Challenge)：MIT，2026-06 仍活跃；挑战数据与评测。

二者适合做 TBox 学习的离线 benchmark，不是“文档 → OWL TBox+ABox”工程流水线，排除为运行时依赖。

## 与当前 Skill 需求的逐项差距

| 当前已确定需求 | 最接近的一手实现 | 仍需自行实现 |
|---|---|---|
| 用户明确指定多个 Markdown 文件 | myKG 支持 Markdown，但入口为目录；OntoRAG 有 Markdown 标题拆分 | 文件列表校验和确定性排序 |
| 单个 Python 分块器、TBox/ABox 双视图 | 没有项目同时提供当前所需的 TBox/ABox 两种独立分块视图；OntoCast 有结构/语义 chunk，但不是此两种固定视图 | 标题优先、字符上限、两种块大小、表格/代码块保护 |
| 无基准本体也动态生成 TBox | OntoCast、myKG、OntoRAG、brains、Docling Graph | 收敛到增强版 `schema_card.json` 的统一 JSON Schema |
| 动态 TBox 后再抽 ABox | myKG、OntoRAG 最接近；OntoCast 是 per-unit 共演化 | ABox 确定性准入、示例/假设过滤、冲突全拒绝 |
| schema_card 是唯一可信来源 | OntoRAG/Docling Graph 有近似实现 | 支持当前轻量 OWL profile 的完整字段和 JSON Schema |
| 稳定英文 IRI + 中文 label/comment | myKG 稳定 ID；Docling Graph 身份字段 | `https://example.org/ontology/<ai-domain>#` 首次生成并在本次产物内固定 |
| 有业务 ID 全局合并；无 ID 保守合并 | Docling Graph 的 `graph_id_fields` 最接近 | 跨文档同名不合并、候选冲突日志 |
| 证据不进 OWL，旁路 `evidence.jsonl` | Docling Graph 有完整 ledger，但写入 graph attr；myKG 只有 source_files | 每条正式三元组的 fact_id、quote、文件、标题路径、行号 |
| 同一 RDF/XML OWL 含 TBox+ABox | brains 示例最接近；myKG 同图但 RDFS/Turtle | RDFLib 确定性构图、`owl:NamedIndividual`、XSD typed literals、RDF/XML 序列化 |
| 轻量 OWL profile + ABox 三类断言 | OntoRAG TBox exporter + 多个 ABox 项目 | 与现有 SHACL profile 对齐的 union 校验 |
| RDF/XML 必须可解析；其余错误可强制交付 | brains/MASEO 有修复循环 | `PASS`/`FORCED_WITH_ERRORS`、3 次修复、qa_report/delivery_status |
| 每次完整重建、不缓存 | 多数项目偏缓存/session/append | 清理并重建本次 artifacts，避免旧事实残留 |

## 推荐复用分层

### 直接复用

- RDFLib、OWL-RL、pySHACL：继续作为解析、推理和约束校验基础。
- Text2KGBench 的 Apache-2.0 评测代码思路与许可兼容的数据子集，用于 ABox precision/recall/conformance/hallucination 回归。
- W3C RDF/OWL/XSD/SHACL 标准词汇，不自创序列化语义。

### 移植（复制思路后按当前数据结构重写）

- OntoCast：ontology/facts map-reduce、render/critic 循环、EntityAligner、literal quarantine、失败单元处理；不引入其缓存、Fuseki、Qdrant、LangGraph 和 tenancy。
- myKG：Pass 1/Pass 2 编排、schema 白名单校验、failed chunks、稳定 ID/边去重、测试组织。
- KnowledgeGraphBuilder：固定 TBox 下的实体/关系抽取分层、coreference/consensus、SHACL 生成和 ABox QA 测试；不移植其置信度仲裁与服务栈。
- OntoRAG：DTO、Schema Card、proposal aggregation。
- Docling Graph：identity fields、deterministic provenance ledger、unresolved 策略、合并 lineage。
- OLAF、OntoConnectLM：OWL class/property/NamedIndividual serializer 的测试场景。
- Structured Decomposition：实体识别与断言抽取分离、reasoner feedback。
- OntoGPT：schema-constrained extraction 和 span grounding 的测试场景。

### 仅参考

- brains-group：CQ/SRD/QA/Fixer 角色与 TBox+ABox 同文件样例。
- MASEO：syntax → consistency → pitfall 的分层 QA。
- AutoSchemaKG：批处理、schema conceptualization、多语言。
- OntoSphere：版本 diff、兼容性检查、可视化产品形态。
- liuhuanyong/OntologyAutoGeneration：概念合并、taxonomy 消环、属性/关系分类。
- Viewsari：mention/referent/coreference 与 PROV-O 模型；当前实现仍采用旁路证据，不复制其图内 provenance。
- Docs2KG：文档结构化和人工审核界面；不采用其弱 ontology JSON 作为 Schema Card。

### 排除为第一版依赖

- OntoGenix：CSV/RML 场景且 GPL-3.0，和 Markdown Skill 的边界不同。
- CIDOC CRM extractor、TetraOnto population、Ontology_population_paper：固定领域/固定 TBox、测试或许可不足。
- LLMs4OL/LLMs4OL Challenge：评测/研究代码，不是完整流水线。
- 直接依赖 OntoCast、KnowledgeGraphBuilder、myKG、Docling Graph 或 OntoGPT：会引入缓存、配置、图存储、session、输入转换、LLM provider、LinkML/NetworkX 等当前 Skill 不需要的平台能力。

## 对第一版 Skill 的最终建议

第一版保持“小 Skill + 确定性脚本”，不要包装任何完整框架：

```text
明确的 Markdown 文件列表
  → chunk_markdown.py：TBox/ABox 双视图 JSONL
  → AI：逐块语义发现
  → AI + 确定性归并：semantic_requirements.json / competency_questions.json
  → AI + JSON Schema：schema_card.json（唯一可信来源）
  → build_owl.py：schema.owl
  → AI：先实体、后断言的 ABox candidates
  → resolve_entities.py：业务 ID 优先、保守消歧、冲突拒绝
  → build_owl.py：instances.owl + ontology.owl（RDF/XML）
  → validate.py：RDFLib + OWL-RL + pySHACL
  → evidence.jsonl / rejections.jsonl / qa_report.json / delivery_status.json
```

这条路线复用了 OntoCast、myKG、Docling Graph 和 OntoRAG 已验证的关键思想，同时避免把第一版 Skill 变成任何一个大型框架的二次封装。真正需要自研的部分集中在本项目的差异化要求：双视图 Markdown 分块、增强版 Schema Card、保守实体消歧、确定性事实准入、轻量 OWL profile 的 RDF/XML 输出和强制交付状态。
