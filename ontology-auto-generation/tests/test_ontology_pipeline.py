from __future__ import annotations

import importlib.util
import hashlib
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from rdflib import BNode, Graph, Literal, RDF, RDFS, OWL, URIRef

SKILL_DIR = Path(__file__).resolve().parents[1]
MODULE_PATH = SKILL_DIR / "scripts" / "ontology_pipeline.py"
sys.path.insert(0, str(SKILL_DIR / "scripts"))
SPEC = importlib.util.spec_from_file_location("ontology_pipeline", MODULE_PATH)
pipeline = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(pipeline)

NS = "https://example.org/ontology/sales#"
XSD = "http://www.w3.org/2001/XMLSchema#"


class ReferenceContractTest(unittest.TestCase):
    def test_abox_object_example_references_a_declared_full_hash_individual(self) -> None:
        reference = (
            SKILL_DIR / "references" / "owl-best-practices.md"
        ).read_text(encoding="utf-8")
        declared = set(
            re.findall(r'<owl:NamedIndividual rdf:about="([^"]+)">', reference)
        )
        object_targets = set(
            re.findall(r'<places rdf:resource="([^"]+)"/>', reference)
        )

        self.assertTrue(object_targets)
        self.assertLessEqual(object_targets, declared)
        self.assertTrue(
            all(re.search(r"#I_[0-9a-f]{64}$", iri) for iri in object_targets)
        )


def schema_card() -> dict:
    return {
        "version": 1,
        "ontology_iri": "https://example.org/ontology/sales",
        "entity_namespace": NS,
        "classes": [
            {
                "iri": NS + "Customer",
                "label": "客户",
                "comment": "购买商品的客户",
                "superclasses": [],
                "equivalent_classes": [],
                "disjoint_with": [],
            },
            {
                "iri": NS + "Order",
                "label": "订单",
                "comment": "客户创建的订单",
                "superclasses": [],
                "equivalent_classes": [],
                "disjoint_with": [],
            },
        ],
        "object_properties": [
            {
                "iri": NS + "places",
                "label": "下单",
                "comment": "客户创建订单",
                "domain": NS + "Customer",
                "range": NS + "Order",
                "subproperty_of": [],
                "equivalent_properties": [],
                "inverse_of": [],
            }
        ],
        "datatype_properties": [
            {
                "iri": NS + "customerId",
                "label": "客户编号",
                "comment": "客户的唯一业务编号",
                "domain": NS + "Customer",
                "range": XSD + "string",
                "subproperty_of": [],
                "equivalent_properties": [],
                "max_count": 1,
                "identity": True,
            },
            {
                "iri": NS + "amount",
                "label": "金额",
                "comment": "订单金额",
                "domain": NS + "Order",
                "range": XSD + "decimal",
                "subproperty_of": [],
                "equivalent_properties": [],
                "max_count": 1,
                "identity": False,
            },
        ],
    }


def evidence(source: str, line: int, quote: str) -> dict:
    return {
        "source": source,
        "heading_path": ["记录"],
        "line_start": line,
        "line_end": line,
        "quote": quote,
    }


class OntologyPipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        (self.workspace / "a.md").write_text(
            "# 记录\n客户 Alice（客户编号 C-001）创建订单 O-1。\n订单 O-1 金额为 12.50。\n",
            encoding="utf-8",
        )
        (self.workspace / "b.md").write_text(
            "# 记录\n客户 Alice Zhang 的客户编号是 C-001。\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_resolve_build_and_validate_combined_graph(self) -> None:
        candidates = {
            "version": 1,
            "entities": [
                {
                    "candidate_id": "a.customer",
                    "class_iri": NS + "Customer",
                    "name": "Alice",
                    "business_identifier": {"property_iri": NS + "customerId", "value": "C-001"},
                    "evidence": evidence("a.md", 2, "客户 Alice（客户编号 C-001）"),
                },
                {
                    "candidate_id": "b.customer",
                    "class_iri": NS + "Customer",
                    "name": "Alice Zhang",
                    "business_identifier": {"property_iri": NS + "customerId", "value": "C-001"},
                    "evidence": evidence("b.md", 2, "客户 Alice Zhang 的客户编号是 C-001"),
                },
                {
                    "candidate_id": "a.order",
                    "class_iri": NS + "Order",
                    "name": "O-1",
                    "business_identifier": None,
                    "evidence": evidence("a.md", 2, "订单 O-1"),
                },
            ],
            "assertions": [
                {
                    "candidate_id": "a.places",
                    "kind": "object",
                    "subject_candidate_id": "a.customer",
                    "property_iri": NS + "places",
                    "object_candidate_id": "a.order",
                    "evidence": evidence("a.md", 2, "创建订单 O-1"),
                },
                {
                    "candidate_id": "a.amount",
                    "kind": "data",
                    "subject_candidate_id": "a.order",
                    "property_iri": NS + "amount",
                    "value": "12.50",
                    "datatype": XSD + "decimal",
                    "evidence": evidence("a.md", 3, "订单 O-1 金额为 12.50"),
                },
            ],
        }

        resolved, evidence_rows, rejections = pipeline.resolve_candidates(
            schema_card(), candidates, self.workspace, [Path("a.md"), Path("b.md")]
        )

        self.assertEqual(2, len(resolved["individuals"]))
        self.assertEqual(3, len(resolved["assertions"]))
        self.assertEqual([], rejections)
        customer_iris = {
            row["subject"]
            for row in evidence_rows
            if row["candidate_id"] in ("a.customer", "b.customer") and row["candidate_kind"] == "entity"
        }
        self.assertEqual(1, len(customer_iris))
        self.assertRegex(next(iter(customer_iris)), re.escape(NS) + r"I_[0-9a-f]{64}$")
        predicates = {row["predicate"] for row in evidence_rows}
        self.assertTrue(
            {
                "http://www.w3.org/1999/02/22-rdf-syntax-ns#type",
                "http://www.w3.org/2000/01/rdf-schema#label",
                "urn:ontology-auto-generation:observedAlias",
                NS + "customerId",
                NS + "places",
                NS + "amount",
            }.issubset(predicates)
        )
        self.assertTrue(
            all(re.fullmatch(r"(?:fact|alias)-v1-[0-9a-f]{64}", row["fact_id"]) for row in evidence_rows)
        )
        self.assertEqual(1, len([row for row in evidence_rows if row["status"] == "observed_alias"]))

        output_dir = self.workspace / "output"
        stats = pipeline.build_owl_files(schema_card(), resolved, output_dir)
        self.assertEqual(2, stats["individuals"])
        graph = Graph().parse(output_dir / "ontology.owl", format="xml")
        self.assertEqual(1, len(set(graph.subjects(RDF.type, OWL.Ontology))))
        self.assertEqual(2, len(set(graph.subjects(RDF.type, OWL.NamedIndividual))))
        self.assertIn((URIRef(NS + "places"), RDF.type, OWL.ObjectProperty), graph)

        validate_spec = importlib.util.spec_from_file_location("validate", SKILL_DIR / "scripts" / "validate.py")
        validate = importlib.util.module_from_spec(validate_spec)
        assert validate_spec.loader is not None
        validate_spec.loader.exec_module(validate)
        for gate in (
            validate.gate1_rdf_syntax(output_dir / "ontology.owl"),
            validate.gate2_owl_consistency(output_dir / "ontology.owl"),
            validate.gate3_output_constraints(output_dir / "ontology.owl", "combined"),
        ):
            self.assertTrue(gate.passed, gate.report())

        tampered = json.loads(json.dumps(resolved))
        tampered["individuals"][0]["unexpected"] = True
        with self.assertRaisesRegex(pipeline.PipelineError, "resolved-instances.schema.json"):
            pipeline.build_owl_files(schema_card(), tampered, self.workspace / "tampered-output")

    def test_builder_revalidates_untrusted_resolved_sidecar(self) -> None:
        candidates = {
            "version": 1,
            "entities": [
                {
                    "candidate_id": "customer",
                    "class_iri": NS + "Customer",
                    "name": "Alice",
                    "business_identifier": {"property_iri": NS + "customerId", "value": "C-001"},
                    "evidence": evidence("a.md", 2, "客户 Alice（客户编号 C-001）"),
                },
                {
                    "candidate_id": "order",
                    "class_iri": NS + "Order",
                    "name": "O-1",
                    "business_identifier": None,
                    "evidence": evidence("a.md", 2, "订单 O-1"),
                },
            ],
            "assertions": [
                {
                    "candidate_id": "places",
                    "kind": "object",
                    "subject_candidate_id": "customer",
                    "property_iri": NS + "places",
                    "object_candidate_id": "order",
                    "evidence": evidence("a.md", 2, "创建订单 O-1"),
                }
            ],
        }
        resolved, _, _ = pipeline.resolve_candidates(
            schema_card(), candidates, self.workspace, [Path("a.md")]
        )

        tampered_cases = []
        wrong_schema_digest = json.loads(json.dumps(resolved))
        wrong_schema_digest["schema_card_sha256"] = "0" * 64
        tampered_cases.append(wrong_schema_digest)
        wrong_identity = json.loads(json.dumps(resolved))
        wrong_identity["individuals"][0]["iri"] = NS + "I_" + "0" * 64
        tampered_cases.append(wrong_identity)
        wrong_fact_id = json.loads(json.dumps(resolved))
        wrong_fact_id["assertions"][0]["fact_id"] = "fact-v1-" + "0" * 64
        tampered_cases.append(wrong_fact_id)
        wrong_alias = json.loads(json.dumps(resolved))
        wrong_alias["individuals"][0]["observed_aliases"] = [
            {
                "alias_id": "alias-v1-" + "0" * 64,
                "name": "Alicia",
                "candidate_ids": ["alias-candidate"],
                "evidence_records": [evidence("a.md", 2, "客户 Alice")],
            }
        ]
        tampered_cases.append(wrong_alias)
        duplicate = json.loads(json.dumps(resolved))
        duplicate["assertions"].append(json.loads(json.dumps(duplicate["assertions"][0])))
        duplicate["assertions"][1]["candidate_ids"] = ["duplicate-fact"]
        tampered_cases.append(duplicate)

        for ordinal, tampered in enumerate(tampered_cases):
            with self.subTest(ordinal=ordinal), self.assertRaises(pipeline.PipelineError):
                pipeline.build_owl_files(schema_card(), tampered, self.workspace / f"tampered-{ordinal}")

        conflict = json.loads(json.dumps(resolved))
        order = next(row for row in conflict["individuals"] if row["class_iri"] == NS + "Order")
        for ordinal, value in enumerate(("1.0", "2.0"), start=1):
            key = ("data", order["iri"], NS + "amount", value, XSD + "decimal")
            conflict["assertions"].append(
                {
                    "kind": "data", "subject_iri": order["iri"], "property_iri": NS + "amount",
                    "value": value, "datatype": XSD + "decimal", "candidate_ids": [f"amount-{ordinal}"],
                    "fact_id": "fact-v1-" + hashlib.sha256(
                        pipeline._canonical_json(["fact", *key]).encode("utf-8")
                    ).hexdigest(),
                }
            )
        with self.assertRaisesRegex(pipeline.PipelineError, "max_count"):
            pipeline.build_owl_files(schema_card(), conflict, self.workspace / "tampered-conflict")

    def test_closed_datatype_profile_accepts_only_verbatim_legal_lexical_forms(self) -> None:
        allowed = {
            "string": ["", "hello"],
            "boolean": ["true", "false", "1", "0"],
            "integer": ["0", "+12", "-9"],
            "decimal": ["12", "12.50", ".5", "-0.0"],
            "double": ["1E3", "-2.5e-2", "INF", "-INF", "NaN"],
            "date": ["0000-01-01", "2026-07-23", "2024-02-29Z", "2026-07-23+08:00"],
            "time": ["00:00:00", "23:59:59.5Z", "24:00:00"],
            "dateTime": ["2026-07-23T14:30:00+08:00"],
            "anyURI": ["https://example.com/a?b=1", "relative/path", "has a space", "\n"],
        }
        forbidden = {
            "boolean": ["TRUE", "yes", "2"],
            "integer": ["1.0", " 1"],
            "decimal": ["1e2", "."],
            "double": ["Infinity", "+INF"],
            "date": ["00000-01-01", "2023-02-29", "2026-7-23"],
            "time": ["24:00:01", "12:60:00"],
            "dateTime": ["2026-07-23 14:30:00", "2026-02-29T00:00:00"],
        }
        for local, values in allowed.items():
            for value in values:
                with self.subTest(datatype=local, value=value):
                    self.assertTrue(pipeline._literal_is_valid(value, XSD + local))
        for local, values in forbidden.items():
            for value in values:
                with self.subTest(datatype=local, value=value):
                    self.assertFalse(pipeline._literal_is_valid(value, XSD + local))

        unsupported = schema_card()
        unsupported["datatype_properties"][0]["range"] = XSD + "token"
        with self.assertRaisesRegex(pipeline.PipelineError, "允许 profile"):
            pipeline.validate_schema_card(unsupported)

        lexical_graph = Graph()
        lexical_subject = URIRef(NS + "I_" + "f" * 64)
        for local, value in (("integer", "+12"), ("decimal", ".5"), ("double", "1E3")):
            lexical_graph.add(
                (lexical_subject, URIRef(NS + local), Literal(value, datatype=URIRef(XSD + local), normalize=False))
            )
        lexical_xml = pipeline._restricted_rdf_xml_bytes(lexical_graph, NS).decode("utf-8")
        for value in ("+12", ".5", "1E3"):
            self.assertIn(f">{value}<", lexical_xml)

        invalid_xml = Graph()
        invalid_xml.add((lexical_subject, RDFS.label, Literal("invalid\x01text", normalize=False)))
        with self.assertRaisesRegex(pipeline.PipelineError, "XML 1.0-invalid"):
            pipeline._restricted_rdf_xml_bytes(invalid_xml, NS)

    def test_restricted_rdf_xml_is_byte_stable_untagged_and_union_exact(self) -> None:
        candidates = {
            "version": 1,
            "entities": [{
                "candidate_id": "customer", "class_iri": NS + "Customer", "name": "Alice",
                "business_identifier": {"property_iri": NS + "customerId", "value": "C-001"},
                "evidence": evidence("a.md", 2, "客户 Alice（客户编号 C-001）"),
            }],
            "assertions": [],
        }
        resolved, _, _ = pipeline.resolve_candidates(
            schema_card(), candidates, self.workspace, [Path("a.md")]
        )
        first = self.workspace / "stable-1"
        second = self.workspace / "stable-2"
        pipeline.build_owl_files(schema_card(), resolved, first)
        shuffled = json.loads(json.dumps(resolved))
        shuffled["individuals"].reverse()
        shuffled["assertions"].reverse()
        pipeline.build_owl_files(schema_card(), shuffled, second)

        for filename in ("schema.owl", "instances.owl", "ontology.owl"):
            self.assertEqual((first / filename).read_bytes(), (second / filename).read_bytes())
            self.assertTrue((first / filename).read_bytes().startswith(b'<?xml version="1.0" encoding="UTF-8"?>\n'))
            root_line = (first / filename).read_text().splitlines()[1]
            for prefix in ("rdf", "rdfs", "owl", "xsd", "ont"):
                self.assertIn(f"xmlns:{prefix}=", root_line)
            graph = Graph().parse(first / filename, format="xml")
            self.assertFalse(any(isinstance(node, BNode) for triple in graph for node in triple))
            for predicate in (RDFS.label, RDFS.comment):
                self.assertTrue(all(value.language is None for value in graph.objects(None, predicate)))

        schema_graph = Graph().parse(first / "schema.owl", format="xml")
        instances_graph = Graph().parse(first / "instances.owl", format="xml")
        combined_graph = Graph().parse(first / "ontology.owl", format="xml")
        declaration = (URIRef(schema_card()["ontology_iri"]), RDF.type, OWL.Ontology)
        self.assertEqual(0, len(set(schema_graph.subjects(RDF.type, OWL.Ontology))))
        self.assertEqual(0, len(set(instances_graph.subjects(RDF.type, OWL.Ontology))))
        self.assertEqual(1, len(set(combined_graph.subjects(RDF.type, OWL.Ontology))))
        self.assertEqual(set(schema_graph) | set(instances_graph) | {declaration}, set(combined_graph))

        validate_spec = importlib.util.spec_from_file_location("validate_bundle", SKILL_DIR / "scripts" / "validate.py")
        validator = importlib.util.module_from_spec(validate_spec)
        assert validate_spec.loader is not None
        validate_spec.loader.exec_module(validator)
        shapes = pipeline._dynamic_shapes(schema_card())
        bundle = validator.gate3_owl_bundle(
            first, shapes, card=schema_card(), expected_dynamic_shapes=shapes
        )
        self.assertTrue(bundle.passed, bundle.report())

        tampered_bundle = self.workspace / "tampered-bundle"
        shutil.copytree(first, tampered_bundle)
        tampered_graph = Graph().parse(tampered_bundle / "ontology.owl", format="xml")
        tampered_graph.remove(next(iter(tampered_graph)))
        tampered_graph.serialize(tampered_bundle / "ontology.owl", format="xml")
        bundle = validator.gate3_owl_bundle(
            tampered_bundle, shapes, card=schema_card(), expected_dynamic_shapes=shapes
        )
        self.assertFalse(bundle.passed)

        empty_shapes = validator.gate3_owl_bundle(
            first, "", card=schema_card(), expected_dynamic_shapes=shapes
        )
        self.assertFalse(empty_shapes.passed)

        wrong_declaration = self.workspace / "wrong-declaration"
        shutil.copytree(first, wrong_declaration)
        wrong_graph = Graph().parse(wrong_declaration / "ontology.owl", format="xml")
        wrong_graph.remove((URIRef(schema_card()["ontology_iri"]), RDF.type, OWL.Ontology))
        wrong_graph.add((URIRef("https://example.com/wrong"), RDF.type, OWL.Ontology))
        wrong_graph.serialize(wrong_declaration / "ontology.owl", format="xml")
        bundle = validator.gate3_owl_bundle(
            wrong_declaration, shapes, card=schema_card(), expected_dynamic_shapes=shapes
        )
        self.assertFalse(bundle.passed)

        typed_label = self.workspace / "typed-label"
        shutil.copytree(first, typed_label)
        for filename in ("schema.owl", "ontology.owl"):
            typed_graph = Graph().parse(typed_label / filename, format="xml")
            customer = URIRef(NS + "Customer")
            typed_graph.remove((customer, RDFS.label, None))
            typed_graph.add((customer, RDFS.label, Literal("42", datatype=URIRef(XSD + "integer"))))
            typed_graph.serialize(typed_label / filename, format="xml")
        bundle = validator.gate3_owl_bundle(
            typed_label, shapes, card=schema_card(), expected_dynamic_shapes=shapes
        )
        self.assertFalse(bundle.passed)

    def test_dynamic_shapes_enforce_whitelist_and_semantic_max_count(self) -> None:
        card = schema_card()
        card["object_properties"].extend(
            [
                {
                    "iri": NS + "specialPlaces", "label": "特别下单", "comment": "特别下单关系",
                    "domain": NS + "Customer", "range": NS + "Order",
                    "subproperty_of": [NS + "places"], "equivalent_properties": [], "inverse_of": [],
                },
                {
                    "iri": NS + "placedBy", "label": "由客户下单", "comment": "下单关系的逆关系",
                    "domain": NS + "Order", "range": NS + "Customer",
                    "subproperty_of": [], "equivalent_properties": [], "inverse_of": [NS + "places"],
                },
            ]
        )
        card["object_properties"][0]["max_count"] = 1
        pipeline.validate_schema_card(card)
        customer = URIRef(NS + "I_" + "1" * 64)
        first_order = URIRef(NS + "I_" + "2" * 64)
        second_order = URIRef(NS + "I_" + "3" * 64)
        graph = Graph()
        for node, class_iri in (
            (customer, NS + "Customer"), (first_order, NS + "Order"), (second_order, NS + "Order")
        ):
            graph.add((node, RDF.type, OWL.NamedIndividual))
            graph.add((node, RDF.type, URIRef(class_iri)))
        graph.add((customer, URIRef(NS + "specialPlaces"), first_order))
        graph.add((second_order, URIRef(NS + "placedBy"), customer))

        from pyshacl import validate as validate_shacl

        conforms, _, _ = validate_shacl(
            data_graph=graph, shacl_graph=pipeline._dynamic_shapes(card), inference="none",
            meta_shacl=True, advanced=False, debug=False,
        )
        self.assertFalse(conforms)

        clean = Graph()
        for triple in graph:
            clean.add(triple)
        clean.remove((second_order, URIRef(NS + "placedBy"), customer))
        conforms, _, _ = validate_shacl(
            data_graph=clean, shacl_graph=pipeline._dynamic_shapes(card), inference="none",
            meta_shacl=True, advanced=False, debug=False,
        )
        self.assertTrue(conforms)

        clean.add((customer, URIRef(NS + "unknownProperty"), Literal("forbidden")))
        conforms, _, _ = validate_shacl(
            data_graph=clean, shacl_graph=pipeline._dynamic_shapes(card), inference="none",
            meta_shacl=True, advanced=False, debug=False,
        )
        self.assertFalse(conforms)

        clean.remove((customer, URIRef(NS + "unknownProperty"), Literal("forbidden")))
        clean.add((customer, RDF.type, URIRef(NS + "UnknownClass")))
        conforms, _, _ = validate_shacl(
            data_graph=clean, shacl_graph=pipeline._dynamic_shapes(card), inference="none",
            meta_shacl=True, advanced=False, debug=False,
        )
        self.assertFalse(conforms)

    def test_entities_without_business_id_remain_source_scoped(self) -> None:
        candidates = {
            "version": 1,
            "entities": [
                {
                    "candidate_id": "a.order",
                    "class_iri": NS + "Order",
                    "name": "同名订单",
                    "evidence": evidence("a.md", 2, "订单 O-1"),
                },
                {
                    "candidate_id": "b.order",
                    "class_iri": NS + "Order",
                    "name": "同名订单",
                    "evidence": evidence("b.md", 2, "客户 Alice Zhang"),
                },
            ],
            "assertions": [],
        }
        resolved, _, _ = pipeline.resolve_candidates(
            schema_card(), candidates, self.workspace, [Path("a.md"), Path("b.md")]
        )
        self.assertEqual(2, len(resolved["individuals"]))
        self.assertEqual(2, len({item["iri"] for item in resolved["individuals"]}))

    def test_conflicting_single_value_assertions_are_all_rejected(self) -> None:
        candidates = {
            "version": 1,
            "entities": [
                {
                    "candidate_id": "order",
                    "class_iri": NS + "Order",
                    "name": "O-1",
                    "evidence": evidence("a.md", 2, "订单 O-1"),
                }
            ],
            "assertions": [
                {
                    "candidate_id": "amount.1",
                    "kind": "data",
                    "subject_candidate_id": "order",
                    "property_iri": NS + "amount",
                    "value": "12.50",
                    "datatype": XSD + "decimal",
                    "evidence": evidence("a.md", 3, "12.50"),
                },
                {
                    "candidate_id": "amount.2",
                    "kind": "data",
                    "subject_candidate_id": "order",
                    "property_iri": NS + "amount",
                    "value": "99.00",
                    "datatype": XSD + "decimal",
                    "evidence": evidence("a.md", 3, "订单 O-1 金额"),
                },
            ],
        }
        resolved, _, rejections = pipeline.resolve_candidates(
            schema_card(), candidates, self.workspace, [Path("a.md")]
        )
        self.assertEqual([], resolved["assertions"])
        self.assertEqual({"amount.1", "amount.2"}, {row["candidate_id"] for row in rejections})

    def test_ungrounded_candidate_is_rejected(self) -> None:
        candidates = {
            "version": 1,
            "entities": [
                {
                    "candidate_id": "invented",
                    "class_iri": NS + "Customer",
                    "name": "Bob",
                    "evidence": evidence("a.md", 2, "文档中不存在 Bob"),
                }
            ],
            "assertions": [],
        }
        resolved, evidence_rows, rejections = pipeline.resolve_candidates(
            schema_card(), candidates, self.workspace, [Path("a.md")]
        )
        self.assertEqual([], resolved["individuals"])
        self.assertEqual([], evidence_rows)
        self.assertEqual(["ENTITY_EVIDENCE_INVALID"], rejections[0]["reasons"])

    def test_invalid_candidate_is_rejected_without_contaminating_identity_group(self) -> None:
        candidates = {
            "version": 1,
            "entities": [
                {
                    "candidate_id": "customer",
                    "class_iri": NS + "Customer",
                    "name": "Alice",
                    "confidence": 0.99,
                    "evidence": evidence("a.md", 2, "客户 Alice"),
                },
                {
                    "candidate_id": ["invalid"],
                    "class_iri": NS + "Customer",
                    "name": "Alice",
                    "evidence": evidence("a.md", 2, "客户 Alice"),
                },
                {
                    "candidate_id": "valid-customer",
                    "class_iri": NS + "Customer",
                    "name": "Alice",
                    "business_identifier": {"property_iri": NS + "customerId", "value": "C-001"},
                    "evidence": evidence("a.md", 2, "客户 Alice（客户编号 C-001）"),
                },
            ],
            "assertions": [],
        }
        resolved, _, rejections = pipeline.resolve_candidates(
            schema_card(), candidates, self.workspace, [Path("a.md")]
        )
        self.assertEqual([["valid-customer"]], [item["candidate_ids"] for item in resolved["individuals"]])
        self.assertEqual(
            {"ENTITY_CONTRACT_INVALID", "ENTITY_CANDIDATE_ID_INVALID"},
            {row["reasons"][0] for row in rejections},
        )

    def test_duplicate_candidate_rejections_are_input_order_independent(self) -> None:
        duplicates = [
            {
                "candidate_id": "duplicate", "class_iri": NS + "Customer", "name": "Alice",
                "evidence": evidence("a.md", 2, "客户 Alice"),
            },
            {
                "candidate_id": "duplicate", "class_iri": NS + "Order", "name": "O-1",
                "evidence": evidence("a.md", 3, "订单 O-1"),
            },
        ]
        forward = pipeline.resolve_candidates(
            schema_card(), {"version": 1, "entities": duplicates, "assertions": []},
            self.workspace, [Path("a.md")],
        )
        reverse = pipeline.resolve_candidates(
            schema_card(), {"version": 1, "entities": list(reversed(duplicates)), "assertions": []},
            self.workspace, [Path("a.md")],
        )
        self.assertEqual(forward, reverse)

    def test_schema_terms_cannot_use_reserved_canonical_entity_local_names(self) -> None:
        card = schema_card()
        card["classes"][0]["iri"] = NS + "I_" + "a" * 64
        with self.assertRaisesRegex(pipeline.PipelineError, "Canonical Entity 保留本地名"):
            pipeline.validate_schema_card(card)

    def test_business_identity_equivalence_nfc_aliases_and_full_hash_are_stable(self) -> None:
        card = json.loads(json.dumps(schema_card()))
        equivalent = {
            "iri": NS + "customerKey", "label": "客户键", "comment": "等价客户身份键",
            "domain": NS + "Customer", "range": XSD + "string", "subproperty_of": [],
            "equivalent_properties": [NS + "customerId"], "max_count": 1, "identity": True,
        }
        card["datatype_properties"][0]["equivalent_properties"] = [NS + "customerKey"]
        card["datatype_properties"].append(equivalent)
        documents = {
            "id-a.md": "# 记录\n客户 Alpha 的编号是 Café。\n",
            "id-b.md": "# 记录\n客户 Beta 的编号是 Cafe\u0301。\n",
            "id-c.md": "# 记录\n客户 Gamma 的编号是 CAFÉ。\n",
            "id-d.md": "# 记录\n客户 Delta 的编号是  Café 。\n",
        }
        for path, content in documents.items():
            (self.workspace / path).write_text(content, encoding="utf-8")
        entities = [
            {
                "candidate_id": "b", "class_iri": NS + "Customer", "name": "Beta",
                "business_identifier": {"property_iri": NS + "customerKey", "value": "Cafe\u0301"},
                "evidence": evidence("id-b.md", 2, "客户 Beta 的编号是 Cafe\u0301"),
            },
            {
                "candidate_id": "a", "class_iri": NS + "Customer", "name": "Alpha",
                "business_identifier": {"property_iri": NS + "customerId", "value": "Café"},
                "evidence": evidence("id-a.md", 2, "客户 Alpha 的编号是 Café"),
            },
            {
                "candidate_id": "c", "class_iri": NS + "Customer", "name": "Gamma",
                "business_identifier": {"property_iri": NS + "customerId", "value": "CAFÉ"},
                "evidence": evidence("id-c.md", 2, "客户 Gamma 的编号是 CAFÉ"),
            },
            {
                "candidate_id": "d", "class_iri": NS + "Customer", "name": "Delta",
                "business_identifier": {"property_iri": NS + "customerId", "value": " Café "},
                "evidence": evidence("id-d.md", 2, "客户 Delta 的编号是  Café "),
            },
        ]
        candidates = {"version": 1, "entities": entities, "assertions": []}
        sources = [Path(path) for path in documents]

        resolved, evidence_rows, rejections = pipeline.resolve_candidates(
            card, candidates, self.workspace, sources
        )
        reversed_result = pipeline.resolve_candidates(
            card, {**candidates, "entities": list(reversed(entities))}, self.workspace, list(reversed(sources))
        )

        self.assertEqual((resolved, evidence_rows, rejections), reversed_result)
        identity_conflicts = [
            row for row in rejections if row["candidate_id"] in {
                "a:business-identifier", "b:business-identifier"
            }
        ]
        self.assertEqual(2, len(identity_conflicts))
        self.assertEqual({"MAX_COUNT_CONFLICT"}, {row["reasons"][0] for row in identity_conflicts})
        self.assertEqual(3, len(resolved["individuals"]))
        merged = next(item for item in resolved["individuals"] if item["candidate_ids"] == ["a", "b"])
        identity_key = pipeline._canonical_json(["business", NS + "customerId", "Café"])
        expected_local = "I_" + hashlib.sha256(identity_key.encode("utf-8")).hexdigest()
        self.assertEqual(NS + expected_local, merged["iri"])
        self.assertRegex(expected_local, r"^I_[0-9a-f]{64}$")
        self.assertEqual("Alpha", merged["label"])
        self.assertEqual(["Beta"], [alias["name"] for alias in merged["observed_aliases"]])
        self.assertRegex(merged["observed_aliases"][0]["alias_id"], r"^alias-v1-[0-9a-f]{64}$")
        self.assertEqual(["b"], merged["observed_aliases"][0]["candidate_ids"])
        self.assertEqual(
            {"property_iri": NS + "customerId", "value": "Café"}, merged["business_identifier"]
        )
        identity_assertions = [
            assertion for assertion in resolved["assertions"]
            if assertion["subject_iri"] == merged["iri"]
            and assertion["property_iri"] in {NS + "customerId", NS + "customerKey"}
        ]
        self.assertEqual([], identity_assertions)

        output = self.workspace / "identity-output"
        pipeline.build_owl_files(card, resolved, output)
        ontology = Graph().parse(output / "ontology.owl", format="xml")
        self.assertNotIn("Beta", {str(value) for value in ontology.objects(None, None)})

    def test_source_scoped_identity_preserves_case_spacing_and_original_class(self) -> None:
        card = json.loads(json.dumps(schema_card()))
        card["classes"].append(
            {
                "iri": NS + "Purchase", "label": "采购单", "comment": "等价订单类型",
                "superclasses": [], "equivalent_classes": [NS + "Order"], "disjoint_with": [],
            }
        )
        card["classes"][1]["equivalent_classes"] = [NS + "Purchase"]
        (self.workspace / "scope.md").write_text(
            "# 记录\nCafé Order\nCafe\u0301 Order\nCAFÉ Order\nCafé  Order\n", encoding="utf-8"
        )
        entities = [
            {
                "candidate_id": "nfc.1", "class_iri": NS + "Order", "name": "Café Order",
                "evidence": evidence("scope.md", 2, "Café Order"),
            },
            {
                "candidate_id": "nfc.2", "class_iri": NS + "Order", "name": "Cafe\u0301 Order",
                "evidence": evidence("scope.md", 3, "Cafe\u0301 Order"),
            },
            {
                "candidate_id": "case", "class_iri": NS + "Order", "name": "CAFÉ Order",
                "evidence": evidence("scope.md", 4, "CAFÉ Order"),
            },
            {
                "candidate_id": "space", "class_iri": NS + "Order", "name": "Café  Order",
                "evidence": evidence("scope.md", 5, "Café  Order"),
            },
            {
                "candidate_id": "equivalent-class", "class_iri": NS + "Purchase", "name": "Café Order",
                "evidence": evidence("scope.md", 2, "Café Order"),
            },
        ]
        resolved, _, _ = pipeline.resolve_candidates(
            card, {"version": 1, "entities": entities, "assertions": []},
            self.workspace, [Path("scope.md")],
        )
        self.assertEqual(4, len(resolved["individuals"]))
        self.assertIn(["nfc.1", "nfc.2"], [item["candidate_ids"] for item in resolved["individuals"]])
        nfc_entity = next(item for item in resolved["individuals"] if item["candidate_ids"] == ["nfc.1", "nfc.2"])
        self.assertEqual("Café Order", nfc_entity["label"])
        self.assertEqual(["Cafe\u0301 Order"], [alias["name"] for alias in nfc_entity["observed_aliases"]])
        self.assertNotEqual(
            next(item["iri"] for item in resolved["individuals"] if "nfc.1" in item["candidate_ids"]),
            next(item["iri"] for item in resolved["individuals"] if "equivalent-class" in item["candidate_ids"]),
        )

    def test_business_identity_selects_specific_class_and_rejects_conflict_group(self) -> None:
        card = json.loads(json.dumps(schema_card()))
        card["classes"].extend(
            [
                {
                    "iri": NS + "Party", "label": "主体", "comment": "业务主体",
                    "superclasses": [], "equivalent_classes": [], "disjoint_with": [],
                },
                {
                    "iri": NS + "Buyer", "label": "买方", "comment": "购买方",
                    "superclasses": [NS + "Party"], "equivalent_classes": [NS + "Purchaser"],
                    "disjoint_with": [],
                },
                {
                    "iri": NS + "Purchaser", "label": "采购方", "comment": "等价买方",
                    "superclasses": [NS + "Party"], "equivalent_classes": [NS + "Buyer"],
                    "disjoint_with": [],
                },
                {
                    "iri": NS + "Seller", "label": "卖方", "comment": "销售方",
                    "superclasses": [NS + "Party"], "equivalent_classes": [], "disjoint_with": [],
                },
            ]
        )
        card["datatype_properties"].append(
            {
                "iri": NS + "partyId", "label": "主体编号", "comment": "主体唯一编号",
                "domain": NS + "Party", "range": XSD + "string", "subproperty_of": [],
                "equivalent_properties": [], "max_count": 1, "identity": True,
            }
        )
        (self.workspace / "classes.md").write_text(
            "# 记录\n主体 Root 编号 P-1。\n买方 Buyer 编号 P-1。\n"
            "买方 One 编号 P-2。\n采购方 Two 编号 P-2。\n"
            "买方 Conflict Buyer 编号 P-3。\n卖方 Conflict Seller 编号 P-3。\n",
            encoding="utf-8",
        )
        rows = [
            ("root", "Party", "Root", "P-1", 2), ("buyer", "Buyer", "Buyer", "P-1", 3),
            ("equiv.1", "Buyer", "One", "P-2", 4), ("equiv.2", "Purchaser", "Two", "P-2", 5),
            ("conflict.1", "Buyer", "Conflict Buyer", "P-3", 6),
            ("conflict.2", "Seller", "Conflict Seller", "P-3", 7),
        ]
        entities = [
            {
                "candidate_id": candidate_id, "class_iri": NS + class_name, "name": name,
                "business_identifier": {"property_iri": NS + "partyId", "value": identifier},
                "evidence": evidence("classes.md", line, identifier),
            }
            for candidate_id, class_name, name, identifier, line in rows
        ]
        assertions = [
            {
                "candidate_id": "depends-on-conflict", "kind": "object",
                "subject_candidate_id": "conflict.1", "property_iri": NS + "places",
                "object_candidate_id": "buyer", "evidence": evidence("classes.md", 6, "P-3"),
            }
        ]

        resolved, _, rejections = pipeline.resolve_candidates(
            card, {"version": 1, "entities": entities, "assertions": assertions},
            self.workspace, [Path("classes.md")],
        )

        self.assertEqual(2, len(resolved["individuals"]))
        self.assertEqual(
            NS + "Buyer", next(item["class_iri"] for item in resolved["individuals"] if "buyer" in item["candidate_ids"])
        )
        self.assertEqual(
            NS + "Buyer", next(item["class_iri"] for item in resolved["individuals"] if "equiv.1" in item["candidate_ids"])
        )
        conflict_rows = [row for row in rejections if row["candidate_id"].startswith("conflict.")]
        self.assertEqual(2, len(conflict_rows))
        self.assertEqual({"IDENTITY_CLASS_CONFLICT"}, {row["reasons"][0] for row in conflict_rows})
        self.assertEqual(1, len({row["context"]["conflict_id"] for row in conflict_rows}))
        self.assertRegex(conflict_rows[0]["context"]["conflict_id"], r"^conflict-v1-[0-9a-f]{64}$")
        dependent = next(row for row in rejections if row["candidate_id"] == "depends-on-conflict")
        self.assertEqual(["IDENTITY_CLASS_CONFLICT"], dependent["context"]["subject_reasons"])
        self.assertEqual(
            conflict_rows[0]["context"]["conflict_id"],
            dependent["context"]["subject_context"]["conflict_id"],
        )

    def test_equivalent_class_assertions_pass_static_shacl(self) -> None:
        card = json.loads(json.dumps(schema_card()))
        card["classes"][0]["equivalent_classes"] = [NS + "ACustomer"]
        card["classes"][1]["equivalent_classes"] = [NS + "Purchase"]
        card["classes"].extend(
            [
                {
                    "iri": NS + "ACustomer", "label": "等价客户", "comment": "客户的等价类型",
                    "superclasses": [], "equivalent_classes": [NS + "Customer"], "disjoint_with": [],
                },
                {
                    "iri": NS + "Purchase", "label": "等价订单", "comment": "订单的等价类型",
                    "superclasses": [], "equivalent_classes": [NS + "Order"], "disjoint_with": [],
                },
            ]
        )
        candidates = {
            "version": 1,
            "entities": [
                {
                    "candidate_id": "customer", "class_iri": NS + "ACustomer", "name": "Alice",
                    "business_identifier": {"property_iri": NS + "customerId", "value": "C-001"},
                    "evidence": evidence("a.md", 2, "客户 Alice（客户编号 C-001）"),
                },
                {
                    "candidate_id": "order", "class_iri": NS + "Purchase", "name": "O-1",
                    "evidence": evidence("a.md", 2, "订单 O-1"),
                },
            ],
            "assertions": [
                {
                    "candidate_id": "places", "kind": "object", "subject_candidate_id": "customer",
                    "property_iri": NS + "places", "object_candidate_id": "order",
                    "evidence": evidence("a.md", 2, "创建订单 O-1"),
                }
            ],
        }
        resolved, _, rejections = pipeline.resolve_candidates(
            card, candidates, self.workspace, [Path("a.md")]
        )
        self.assertEqual([], rejections)
        output = self.workspace / "equivalent-output"
        pipeline.build_owl_files(card, resolved, output)

        validate_spec = importlib.util.spec_from_file_location("ontology_validate", SKILL_DIR / "scripts" / "validate.py")
        validator = importlib.util.module_from_spec(validate_spec)
        assert validate_spec.loader is not None
        validate_spec.loader.exec_module(validator)
        result = validator.gate3_output_constraints(str(output / "ontology.owl"), "combined")
        self.assertTrue(result.passed, result.errors)

    def test_semantic_closure_conflicts_duplicates_and_ledger_ids_are_stable(self) -> None:
        card = json.loads(json.dumps(schema_card()))
        card["object_properties"][0]["subproperty_of"] = [NS + "customerOrder", NS + "priorityOrder"]
        card["object_properties"][0]["inverse_of"] = [NS + "placedBy"]
        card["object_properties"].extend(
            [
                {
                    "iri": NS + "customerOrder", "label": "客户订单", "comment": "客户关联的订单",
                    "domain": NS + "Customer", "range": NS + "Order", "subproperty_of": [],
                    "equivalent_properties": [], "inverse_of": [], "max_count": 1,
                },
                {
                    "iri": NS + "priorityOrder", "label": "优先订单", "comment": "客户的唯一优先订单",
                    "domain": NS + "Customer", "range": NS + "Order", "subproperty_of": [],
                    "equivalent_properties": [], "inverse_of": [], "max_count": 1,
                },
                {
                    "iri": NS + "placedBy", "label": "下单客户", "comment": "订单的下单客户",
                    "domain": NS + "Order", "range": NS + "Customer", "subproperty_of": [],
                    "equivalent_properties": [], "inverse_of": [NS + "places"],
                },
                {
                    "iri": NS + "knows", "label": "认识", "comment": "客户认识另一客户",
                    "domain": NS + "Customer", "range": NS + "Customer", "subproperty_of": [],
                    "equivalent_properties": [], "inverse_of": [],
                },
            ]
        )
        (self.workspace / "admission.md").write_text(
            "# 记录\n客户 Alice 创建订单 O-1。\n订单 O-2 由客户 Alice 创建。\n客户 Alice 认识自己。\n",
            encoding="utf-8",
        )
        entities = [
            {"candidate_id": "customer", "class_iri": NS + "Customer", "name": "Alice",
             "evidence": evidence("admission.md", 2, "客户 Alice")},
            {"candidate_id": "order-1", "class_iri": NS + "Order", "name": "O-1",
             "evidence": evidence("admission.md", 2, "订单 O-1")},
            {"candidate_id": "order-2", "class_iri": NS + "Order", "name": "O-2",
             "evidence": evidence("admission.md", 3, "订单 O-2")},
        ]
        assertions = [
            {"candidate_id": "places-o1", "kind": "object", "subject_candidate_id": "customer",
             "property_iri": NS + "places", "object_candidate_id": "order-1",
             "evidence": evidence("admission.md", 2, "创建订单 O-1")},
            {"candidate_id": "placed-by-o2", "kind": "object", "subject_candidate_id": "order-2",
             "property_iri": NS + "placedBy", "object_candidate_id": "customer",
             "evidence": evidence("admission.md", 3, "由客户 Alice 创建")},
            {"candidate_id": "self-1", "kind": "object", "subject_candidate_id": "customer",
             "property_iri": NS + "knows", "object_candidate_id": "customer",
             "evidence": evidence("admission.md", 4, "认识自己")},
            {"candidate_id": "self-2", "kind": "object", "subject_candidate_id": "customer",
             "property_iri": NS + "knows", "object_candidate_id": "customer",
             "evidence": evidence("admission.md", 4, "客户 Alice 认识自己")},
        ]
        candidates = {"version": 1, "entities": entities, "assertions": assertions}
        result = pipeline.resolve_candidates(card, candidates, self.workspace, [Path("admission.md")])
        reversed_result = pipeline.resolve_candidates(
            card,
            {"version": 1, "entities": list(reversed(entities)), "assertions": list(reversed(assertions))},
            self.workspace,
            [Path("admission.md")],
        )
        self.assertEqual(result, reversed_result)
        resolved, evidence_rows, rejections = result
        self.assertEqual(1, len(resolved["assertions"]))
        self.assertEqual(["self-1", "self-2"], resolved["assertions"][0]["candidate_ids"])
        conflicts = [row for row in rejections if row["candidate_id"] in {"places-o1", "placed-by-o2"}]
        self.assertEqual({"MAX_COUNT_CONFLICT"}, {row["reasons"][0] for row in conflicts})
        self.assertTrue(all(len(row["context"]["conflict_ids"]) == 2 for row in conflicts))
        self.assertEqual(1, len({tuple(row["context"]["conflict_ids"]) for row in conflicts}))
        self.assertRegex(conflicts[0]["context"]["conflict_id"], r"^conflict-v1-[0-9a-f]{64}$")
        assertion_evidence = [row for row in evidence_rows if row["candidate_id"].startswith("self-")]
        self.assertEqual(2, len(assertion_evidence))
        self.assertTrue(all(re.fullmatch(r"fact-v1-[0-9a-f]{64}", row["fact_id"]) for row in assertion_evidence))


if __name__ == "__main__":
    unittest.main()
