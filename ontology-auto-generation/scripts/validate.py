#!/usr/bin/env python3
"""OWL TBox+ABox 校验 — 复用 RDFLib、OWL-RL 与 pySHACL，零 Java 依赖。

对应 brains-group 的 tools/syntax_checks.py，并以标准 SHACL Shapes 表达输出约束。

用法:
    python3 scripts/validate.py <owl_file>
    python3 scripts/validate.py <owl_file> --gate 1    # 仅 RDF 语法
    python3 scripts/validate.py <owl_file> --gate 2    # 仅 OWL 结构一致性
    python3 scripts/validate.py <owl_file> --gate 3    # 仅输出约束

依赖: pip install -r requirements.txt
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from xsd_profile import literal_is_valid


class GateResult:
    def __init__(self, gate: int, name: str):
        self.gate = gate
        self.name = name
        self.passed = True
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def fail(self, msg: str):
        self.passed = False
        self.errors.append(msg)

    def warn(self, msg: str):
        self.warnings.append(msg)

    def report(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        lines = [f"Gate {self.gate} ({self.name}): {status}"]
        for e in self.errors:
            lines.append(f"  ❌ {e}")
        for w in self.warnings:
            lines.append(f"  ⚠️  {w}")
        return "\n".join(lines)

    def to_json(self) -> dict:
        return {
            "gate": self.gate,
            "name": self.name,
            "status": "success" if self.passed else "error",
            "errors": self.errors,
            "warnings": self.warnings,
        }


# ═══════════════════════════════════════════════
# 共享的 rdflib Graph 加载
# ═══════════════════════════════════════════════

def _load_graph(filepath: str):
    """加载本体图，返回 (Graph, text_lines)。失败返回 (None, None, 错误信息)。"""
    from rdflib import Graph

    path = Path(filepath)
    if not path.exists():
        return None, None, f"文件不存在: {filepath}"

    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    suffix = path.suffix.lower()
    fmt_map = {".ttl": "turtle", ".owl": "xml", ".rdf": "xml", ".nt": "nt", ".n3": "n3"}
    fmt = fmt_map.get(suffix, None)

    g = Graph()
    try:
        if fmt:
            g.parse(str(path), format=fmt)
        else:
            g.parse(str(path))
    except Exception as e:
        return None, lines, str(e)

    return g, lines, None


def _parse_error_report(error_msg: str, lines: list[str]) -> list[str]:
    """从 rdflib 异常中提取行号并生成上下文报告（含 suspect_line 回溯）。"""
    report = [f"RDF 解析失败: {error_msg}"]
    m_line = re.search(r"line\s+(\d+)", error_msg)
    rep_line = int(m_line.group(1)) if m_line else None

    # suspect_line 回溯：找出前一条可能缺语句终止符的行
    suspect = None
    if rep_line and lines:
        for i in range(rep_line - 1, 0, -1):
            s = lines[i - 1].strip()
            if not s or s.startswith("#") or s.startswith("@"):
                continue
            if not s.endswith(('.', ';', ',', ']', '}', ')')):
                suspect = i
            break  # 只回溯一层
    if suspect:
        report.append(f"  suspect_line: {suspect}, 该行可能缺少 '.' 或 ';'")

    if rep_line:
        ctx_start = max(0, rep_line - 4)
        ctx_end = min(len(lines), rep_line + 3)
        report.append(f"  context (第{ctx_start+1}-{ctx_end}行):")
        for i in range(ctx_start, ctx_end):
            marker = ">>>" if i + 1 == rep_line else "   "
            report.append(f"  {marker} {i+1}: {lines[i][:120]}")
    return report


# ═══════════════════════════════════════════════
# Gate 1: RDF 语法 — rdflib 解析 (≈ verify_rdf_syntax)
# ═══════════════════════════════════════════════

def gate1_rdf_syntax(filepath: str) -> GateResult:
    result = GateResult(1, "RDF 语法 (rdflib)")

    import rdflib
    from rdflib import Namespace, URIRef

    g, lines, err = _load_graph(filepath)
    if g is None:
        for msg in _parse_error_report(err or "未知错误", lines or []):
            result.fail(msg)
        return result

    triples = len(g)
    if triples == 0:
        result.warn("本体为空，未包含任何三元组")
    else:
        result.warn(f"共 {triples} 个三元组，解析成功")

    # 输出格式不需要 owl:Ontology 声明；Gate 3 会拒绝该三元组。

    # URI 含中文检测
    RDFS = Namespace("http://www.w3.org/2000/01/rdf-schema#")
    for s, p, o in g:
        for node in (s, p, o):
            if not isinstance(node, URIRef):
                continue
            uri = str(node)
            if any('\u4e00' <= c <= '\u9fff' for c in uri):
                result.fail(f"URI 含中文: {uri[:80]}...")
                break

    # Language metadata is outside the v1 contract; Gate 3 rejects tagged labels/comments.
    for s, p, o in g.triples((None, RDFS.label, None)):
        if isinstance(o, rdflib.Literal) and o.language is not None:
            val = str(o)[:40]
            result.warn(f"rdfs:label 带有语言标签: '{val}' (主语: {_short(s)})")

    return result


# ═══════════════════════════════════════════════
# Gate 2: OWL-RL 推理检查 — owlrl
# ═══════════════════════════════════════════════

def gate2_owl_consistency(filepath: str) -> GateResult:
    try:
        from owlrl import DeductiveClosure, OWLRL_Semantics
    except ImportError:
        result = GateResult(2, "OWL-RL 推理 (owlrl)")
        requirements = Path(__file__).resolve().parent.parent / "requirements.txt"
        result.fail(f"缺少 OWL-RL，请先执行: pip install -r {requirements}")
        return result
    result = GateResult(2, "OWL-RL 推理 (owlrl)")

    import copy
    from rdflib import RDF, RDFS, OWL

    g, _, err = _load_graph(filepath)
    if g is None:
        result.fail("无法解析文件，跳过 OWL-RL 推理")
        return result

    try:
        rg = copy.deepcopy(g)
        DeductiveClosure(OWLRL_Semantics).expand(rg)
        inferred_subclasses = set()
        for s, p, o in rg.triples((None, RDFS.subClassOf, None)):
            if (s, p, o) not in g:
                sub = _short(s)
                sup = _short(o)
                if sub in ("Nothing", "Thing") or sup in ("Nothing", "Thing"):
                    continue
                if sub == sup:
                    continue
                inferred_subclasses.add((sub, sup))
        if inferred_subclasses:
            result.warn(f"OWL-RL 推断出 {len(inferred_subclasses)} 条隐含 subClassOf")

        unsatisfiable = {
            _short(cls)
            for cls in g.subjects(RDF.type, OWL.Class)
            if (cls, RDFS.subClassOf, OWL.Nothing) in rg and cls != OWL.Nothing
        }
        if unsatisfiable:
            result.fail(f"OWL-RL 推断出不可满足类: {sorted(unsatisfiable)}")
    except Exception as exc:
        result.fail(f"OWL-RL 推理失败: {exc}")

    return result


# ═══════════════════════════════════════════════
# Gate 3: 输出约束
# ═══════════════════════════════════════════════

def gate3_output_constraints(filepath: str, kind: str = "combined") -> GateResult:
    """使用 pySHACL 检查目标输出格式约束。"""
    result = GateResult(3, "输出约束 (pySHACL)")

    try:
        from pyshacl import validate
    except ImportError:
        requirements = Path(__file__).resolve().parent.parent / "requirements.txt"
        result.fail(f"缺少 pySHACL，请先执行: pip install -r {requirements}")
        return result

    from rdflib import BNode, Namespace, RDF, RDFS, OWL, XSD, Literal

    g, _, err = _load_graph(filepath)
    if g is None:
        result.fail("无法解析文件，跳过输出约束检查")
        return result

    ontology_nodes = set(g.subjects(RDF.type, OWL.Ontology))
    expected = 1 if kind == "combined" else 0
    if len(ontology_nodes) != expected:
        result.fail(f"{kind} 输出必须包含 {expected} 个 owl:Ontology 声明，实际为 {len(ontology_nodes)}")

    if any(isinstance(node, BNode) for triple in g for node in triple):
        result.fail(f"{kind} 输出禁止 blank node")

    for _, predicate, value in g:
        if (
            isinstance(value, Literal)
            and value.datatype is not None
            and getattr(value, "ill_typed", False) is True
            and not literal_is_valid(str(value), str(value.datatype))
        ):
            result.fail(f"typed literal 词法值不合法: predicate={_short(predicate)}, value={value}")
        if predicate in (RDFS.label, RDFS.comment) and isinstance(value, Literal):
            if value.language is not None or value.datatype not in (None, XSD.string):
                result.fail(f"label/comment 必须是无语言标签字符串: predicate={_short(predicate)}, value={value}")

    shapes_path = Path(__file__).resolve().parent.parent / "references" / "owl-output-shapes.ttl"
    if not shapes_path.exists():
        result.fail(f"SHACL Shapes 文件不存在: {shapes_path}")
        return result

    try:
        conforms, report_graph, report_text = validate(
            data_graph=g,
            shacl_graph=str(shapes_path),
            inference="none",
            abort_on_first=False,
            allow_infos=True,
            allow_warnings=True,
            meta_shacl=True,
            advanced=False,
            debug=False,
        )
    except Exception as exc:
        result.fail(f"pySHACL 执行失败: {exc}")
        return result

    SH = Namespace("http://www.w3.org/ns/shacl#")
    if not hasattr(report_graph, "subjects"):
        result.fail(f"pySHACL 返回 ValidationFailure: {report_text}")
        return result

    seen: set[tuple[str, str, str]] = set()
    for item in report_graph.subjects(RDF.type, SH.ValidationResult):
        focus = report_graph.value(item, SH.focusNode)
        path = report_graph.value(item, SH.resultPath)
        severity = report_graph.value(item, SH.resultSeverity)
        messages = list(report_graph.objects(item, SH.resultMessage))
        message = next((str(msg) for msg in messages if msg.language == "zh"), None)
        if message is None:
            message = str(messages[0]) if messages else "SHACL 约束未通过"
        context = f"focus={_short(focus)}"
        if path is not None:
            context += f", path={_short(path)}"
        key = (str(severity), message, context)
        if key in seen:
            continue
        seen.add(key)
        detail = f"{message} ({context})"
        if severity in (SH.Warning, SH.Info):
            result.warn(detail)
        else:
            result.fail(detail)

    if not conforms and result.passed:
        result.fail(f"SHACL 校验未通过: {report_text}")

    return result


def gate3_owl_bundle(
    output_dir: str | Path,
    dynamic_shapes: str | Path,
    *,
    card: dict,
    expected_dynamic_shapes: str,
) -> GateResult:
    """Independently verify all three OWL files and the saved Schema Card shapes."""
    from rdflib import BNode, Graph, RDF, RDFS, OWL, URIRef

    result = GateResult(3, "三图输出约束 (static + dynamic SHACL)")
    root = Path(output_dir)
    loaded: dict[str, Graph] = {}
    for filename in ("schema.owl", "instances.owl", "ontology.owl"):
        graph, _, error = _load_graph(str(root / filename))
        if graph is None:
            result.fail(f"{filename} 无法解析: {error}")
            continue
        loaded[filename] = graph
        if any(isinstance(node, BNode) for triple in graph for node in triple):
            result.fail(f"{filename} 禁止 blank node")

    if len(loaded) != 3:
        return result

    schema_graph = loaded["schema.owl"]
    instances_graph = loaded["instances.owl"]
    combined_graph = loaded["ontology.owl"]
    schema_declarations = set(schema_graph.subjects(RDF.type, OWL.Ontology))
    instance_declarations = set(instances_graph.subjects(RDF.type, OWL.Ontology))
    combined_declarations = set(combined_graph.subjects(RDF.type, OWL.Ontology))
    if schema_declarations:
        result.fail(f"schema.owl 必须有 0 个 ontology declaration，实际为 {len(schema_declarations)}")
    if instance_declarations:
        result.fail(f"instances.owl 必须有 0 个 ontology declaration，实际为 {len(instance_declarations)}")
    if len(combined_declarations) != 1:
        result.fail(f"ontology.owl 必须有 1 个 ontology declaration，实际为 {len(combined_declarations)}")
    elif combined_declarations != {URIRef(card["ontology_iri"])}:
        result.fail("ontology.owl declaration 与权威 Schema Card ontology_iri 不一致")
    elif set(combined_graph) != set(schema_graph) | set(instances_graph) | {
        (next(iter(combined_declarations)), RDF.type, OWL.Ontology)
    }:
        result.fail("schema.owl ∪ instances.owl 加唯一 declaration 不等于 ontology.owl")

    expected_declarations = {
        OWL.Class: {URIRef(term["iri"]) for term in card["classes"]},
        OWL.ObjectProperty: {URIRef(term["iri"]) for term in card["object_properties"]},
        OWL.DatatypeProperty: {URIRef(term["iri"]) for term in card["datatype_properties"]},
    }
    for kind, expected_terms in expected_declarations.items():
        actual_terms = set(schema_graph.subjects(RDF.type, kind))
        if actual_terms != expected_terms:
            result.fail(f"schema.owl 的 {_short(kind)} declarations 与权威 Schema Card 不一致")
    for prop in card["object_properties"] + card["datatype_properties"]:
        node = URIRef(prop["iri"])
        if (node, RDFS.domain, URIRef(prop["domain"])) not in schema_graph:
            result.fail(f"schema.owl 缺少权威 domain: {prop['iri']}")
        if (node, RDFS.range, URIRef(prop["range"])) not in schema_graph:
            result.fail(f"schema.owl 缺少权威 range: {prop['iri']}")

    for path, kind in ((root / "schema.owl", "fragment"), (root / "ontology.owl", "combined")):
        check = gate3_output_constraints(str(path), kind)
        result.errors.extend(check.errors)
        result.warnings.extend(check.warnings)
        result.passed = result.passed and check.passed

    try:
        from pyshacl import validate as validate_shacl

        shapes_graph: Graph
        if isinstance(dynamic_shapes, Path) or (
            isinstance(dynamic_shapes, str) and "\n" not in dynamic_shapes and Path(dynamic_shapes).exists()
        ):
            actual_dynamic_shapes = Path(dynamic_shapes).read_text(encoding="utf-8")
        else:
            actual_dynamic_shapes = str(dynamic_shapes)
        if actual_dynamic_shapes != expected_dynamic_shapes:
            result.fail("保存的 dynamic Schema Card shapes 与权威 Schema Card 不一致")
        shapes_graph = Graph().parse(data=actual_dynamic_shapes, format="turtle")
        conforms, _, report_text = validate_shacl(
            data_graph=combined_graph,
            shacl_graph=shapes_graph,
            inference="none",
            abort_on_first=False,
            meta_shacl=True,
            advanced=False,
            debug=False,
        )
        if not conforms:
            result.fail(f"dynamic Schema Card SHACL 未通过: {report_text}")
    except Exception as exc:
        result.fail(f"dynamic Schema Card SHACL 执行失败: {exc}")
    return result


# ═══════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════

def _short(uri) -> str:
    s = str(uri)
    return s.rsplit("#", 1)[-1] if "#" in s else s.rsplit("/", 1)[-1]


# ═══════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="OWL 三级校验 (RDFLib + OWL-RL + pySHACL)")
    parser.add_argument("owl_file", help="OWL 文件路径 (.owl / .ttl)")
    parser.add_argument("--gate", type=int, choices=[1, 2, 3])
    parser.add_argument(
        "--kind",
        choices=["combined", "fragment"],
        default="combined",
        help="combined 要求恰好一个 owl:Ontology；fragment 要求没有声明",
    )
    parser.add_argument("--json", action="store_true", help="JSON 输出 (brains-group 兼容)")
    args = parser.parse_args()

    gates = {
        1: ("RDF 语法 (rdflib)", gate1_rdf_syntax),
        2: ("OWL-RL 推理 (owlrl)", gate2_owl_consistency),
        3: ("输出约束 (pySHACL)", lambda filepath: gate3_output_constraints(filepath, args.kind)),
    }

    if args.gate:
        _, func = gates[args.gate]
        result = func(args.owl_file)
        if args.json:
            import json
            print(json.dumps(result.to_json(), ensure_ascii=False, indent=2))
        else:
            print(result.report())
        sys.exit(0 if result.passed else 1)

    all_passed = True
    all_results = []
    for gate_num in [1, 2, 3]:
        _, func = gates[gate_num]
        result = func(args.owl_file)
        all_results.append(result)
        if not result.passed:
            all_passed = False
        if not args.json:
            print(result.report())
            print()

    if args.json:
        import json
        print(json.dumps({
            "status": "success" if all_passed else "error",
            "gates": [r.to_json() for r in all_results],
        }, ensure_ascii=False, indent=2))
    else:
        print(f"总体: {'PASS' if all_passed else 'FAIL'}")
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
