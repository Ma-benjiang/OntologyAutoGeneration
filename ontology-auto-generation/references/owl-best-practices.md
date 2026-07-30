# OWL TBox+ABox 编写约束

本参考只描述目标输出支持的轻量 OWL Profile。机器约束见 `owl-output-shapes.ttl`；结构化源数据见 `schema-card.schema.json` 和 `abox-candidates.schema.json`。

## Schema Card 优先

- `schema_card.json` 是 TBox 和 ABox 白名单的唯一可信来源。
- `resolved_instances.json` 是确定性准入结果。
- `schema.owl`、`instances.owl`、`ontology.owl` 只由 `scripts/ontology_pipeline.py build` 生成，禁止手工修改。
- `max_count` 和 `identity` 是抽取约束，不持久化为 OWL cardinality/FunctionalProperty。

## 命名

- Class：PascalCase，如 `IndividualCustomer`、`OrderItem`。
- Property：camelCase，如 `places`、`annualSpend`。
- NamedIndividual：由解析脚本生成稳定英文 IRI，禁止人工拼接。
- 所有本地名匹配 `^[A-Za-z][A-Za-z0-9_]*$`，且同一本地名只能对应一个 IRI。
- 中文业务名称只进入 `rdfs:label`/`rdfs:comment`，不进入 IRI。

## TBox 示例

```xml
<owl:Class rdf:about="https://example.org/ontology/sales#Customer">
  <rdfs:label>客户</rdfs:label>
  <rdfs:comment>购买商品的客户</rdfs:comment>
</owl:Class>

<owl:ObjectProperty rdf:about="https://example.org/ontology/sales#places">
  <rdfs:label>下单</rdfs:label>
  <rdfs:comment>客户创建订单</rdfs:comment>
  <rdfs:domain rdf:resource="https://example.org/ontology/sales#Customer"/>
  <rdfs:range rdf:resource="https://example.org/ontology/sales#Order"/>
</owl:ObjectProperty>

<owl:DatatypeProperty rdf:about="https://example.org/ontology/sales#amount">
  <rdfs:label>金额</rdfs:label>
  <rdfs:comment>订单金额</rdfs:comment>
  <rdfs:domain rdf:resource="https://example.org/ontology/sales#Order"/>
  <rdfs:range rdf:resource="http://www.w3.org/2001/XMLSchema#decimal"/>
</owl:DatatypeProperty>
```

## ABox 示例

```xml
<owl:NamedIndividual rdf:about="https://example.org/ontology/sales#I_0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef">
  <rdf:type rdf:resource="https://example.org/ontology/sales#Customer"/>
  <rdfs:label>Alice</rdfs:label>
  <places rdf:resource="https://example.org/ontology/sales#I_abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"/>
</owl:NamedIndividual>

<owl:NamedIndividual rdf:about="https://example.org/ontology/sales#I_abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789">
  <rdf:type rdf:resource="https://example.org/ontology/sales#Order"/>
  <rdfs:label>O-1</rdfs:label>
  <amount rdf:datatype="http://www.w3.org/2001/XMLSchema#decimal">12.50</amount>
</owl:NamedIndividual>
```

受限序列化器固定 namespace 前缀、完整 IRI/term 排序、XML declaration、两空格缩进与 LF。判断语义时仍比较完整 IRI，不依赖前缀名。

## 枚举

枚举使用“枚举父类 + 值类子类”，业务实体通过 ObjectProperty 指向枚举父类。例如 `PaidStatus rdfs:subClassOf OrderStatus`。不要使用 `owl:oneOf`，也不要把枚举值生成为 `owl:NamedIndividual`。

## 实例准入

- Class 和断言谓语必须存在于 Schema Card。
- ObjectProperty 的 subject/object Class 必须满足 domain/range；允许显式子类。
- DatatypeProperty 的 subject Class 必须满足 domain，typed literal datatype 必须与 range 完全一致，词法值必须有效。
- `max_count` 冲突时拒绝该 subject/property 下的全部冲突值，不按顺序选择。
- 每个 admitted entity/assertion 必须有 workspace 内 Source Document、标题路径、行范围和逐字 quote。
- 示例、假设、模板、否定陈述、建议以及证据不足的推断不进入 ABox。

## 身份与证据

- 有业务 ID：实例 IRI 只依赖 identity Property 和规范化 ID，不依赖显示名称或来源文件。
- 无业务 ID：实例 IRI 依赖 workspace-relative source path、Class 和规范化名称。
- 证据、来源、置信度和拒绝原因只写旁路 JSONL，不进入 OWL。

## 输出边界

支持：

- `owl:Ontology`（仅合并图一个）、`owl:Class`、`owl:ObjectProperty`、`owl:DatatypeProperty`、`owl:NamedIndividual`。
- `rdfs:label`、`rdfs:comment`。
- `rdfs:subClassOf`、`rdfs:subPropertyOf`、`owl:inverseOf`、`owl:equivalentClass`、`owl:equivalentProperty`、`rdfs:domain`、`rdfs:range`、`owl:disjointWith`。
- NamedIndividual 的 Class、ObjectProperty 和 DatatypeProperty 断言。

禁止：

- blank node、RDF Collection、`owl:oneOf`、`owl:Restriction`、匿名或复杂类表达式。
- `owl:AnnotationProperty`、自定义注解、未声明业务谓语。
- 同一 IRI 同时声明为多种 Ontology Term，或本地名冲突。

## 常见错误

1. 把中文名称写进 IRI。
2. 将实例的对象关系写成字符串 literal。
3. 把字符串/数字 literal 的 XSD datatype 写错或省略。
4. ObjectProperty 的 domain/range 倒置。
5. 仅因跨文档同名就合并无业务 ID 的实体。
6. 直接编辑 OWL，导致它与 Schema Card、证据账本不一致。
7. 把 quote、source、confidence 写成 OWL 自定义注解。
