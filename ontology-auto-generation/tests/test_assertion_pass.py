from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
CLI = SKILL_DIR / "scripts" / "ontology_pipeline.py"
XSD = "http://www.w3.org/2001/XMLSchema#"


class AssertionPassCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.output = self.root / "output"
        self.workspace.mkdir()
        (self.workspace / "source.md").write_text(
            "# Orders\n"
            "Customer Alice placed order O-1 with amount 12.50.\n"
            "Customer Alice has customer ID C-1.\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CLI), *args], cwd=self.root, text=True, capture_output=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )

    def write_json(self, name: str, value: dict) -> Path:
        path = self.root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def submit(self, work: dict, result: Path) -> subprocess.CompletedProcess[str]:
        return self.run_cli(
            "run", "submit", "--output", str(self.output), "--work-item-id", work["work_item_id"],
            "--input-digest", work["input_digest"], "--result", str(result),
        )

    def advance_to_entity(self) -> tuple[dict, dict]:
        started = self.run_cli(
            "run", "start", "--workspace", str(self.workspace), "--output", str(self.output),
            "--source", "source.md",
        )
        self.assertEqual(0, started.returncode, started.stderr)
        envelope = json.loads(started.stdout)
        cq = self.root / "cq.md"
        cq.write_text("# CQs\n- Which customer placed which order?\n", encoding="utf-8")
        completed = self.submit(envelope["pending_work_details"][0], cq)
        self.assertEqual(0, completed.returncode, completed.stderr)
        envelope = json.loads(completed.stdout)
        srd = self.root / "srd.md"
        srd.write_text("# SRD\nCustomers place orders with amounts.\n", encoding="utf-8")
        completed = self.submit(envelope["pending_work_details"][0], srd)
        self.assertEqual(0, completed.returncode, completed.stderr)
        envelope = json.loads(completed.stdout)
        project = json.loads((self.output / "project.json").read_text())
        namespace = project["entity_namespace"]
        card = {
            "version": 1,
            "ontology_iri": project["ontology_iri"],
            "entity_namespace": namespace,
            "classes": [
                {
                    "iri": namespace + "Customer", "label": "Customer", "comment": "A customer.",
                    "superclasses": [], "equivalent_classes": [], "disjoint_with": [],
                },
                {
                    "iri": namespace + "Order", "label": "Order", "comment": "An order.",
                    "superclasses": [], "equivalent_classes": [], "disjoint_with": [],
                },
            ],
            "object_properties": [
                {
                    "iri": namespace + "places", "label": "places", "comment": "Places an order.",
                    "domain": namespace + "Customer", "range": namespace + "Order", "subproperty_of": [],
                    "equivalent_properties": [], "inverse_of": [],
                },
                {
                    "iri": namespace + "requests", "label": "requests", "comment": "Requests an order.",
                    "domain": namespace + "Customer", "range": namespace + "Order", "subproperty_of": [],
                    "equivalent_properties": [], "inverse_of": [],
                },
            ],
            "datatype_properties": [
                {
                    "iri": namespace + "amount", "label": "amount", "comment": "Order amount.",
                    "domain": namespace + "Order", "range": XSD + "decimal", "subproperty_of": [],
                    "equivalent_properties": [], "identity": False,
                },
                {
                    "iri": namespace + "orderDate", "label": "order date", "comment": "Order date.",
                    "domain": namespace + "Order", "range": XSD + "date", "subproperty_of": [],
                    "equivalent_properties": [], "identity": False,
                },
                {
                    "iri": namespace + "customerId", "label": "customer ID", "comment": "Customer ID.",
                    "domain": namespace + "Customer", "range": XSD + "string", "subproperty_of": [],
                    "equivalent_properties": [], "max_count": 1, "identity": True,
                },
            ],
        }
        completed = self.submit(envelope["pending_work_details"][0], self.write_json("schema.json", card))
        self.assertEqual(0, completed.returncode, completed.stderr)
        return json.loads(completed.stdout), card

    def entity_result(self, work: dict, card: dict) -> dict:
        prefix = work["input"]["candidate_id_prefix"]
        primary = work["input"]["chunk"]["primary"]
        lines = primary["text"].splitlines()
        relative_line, quote = next(
            (ordinal, line) for ordinal, line in enumerate(lines) if "Alice" in line and "O-1" in line
        )
        evidence = {
            "source": "source.md", "heading_path": ["Orders"],
            "line_start": primary["line_start"] + relative_line,
            "line_end": primary["line_start"] + relative_line,
            "quote": quote,
        }
        return {
            "version": 1, "chunk_id": work["input"]["chunk"]["chunk_id"], "status": "complete",
            "entities": [
                {
                    "candidate_id": prefix + ".entity.001", "class_iri": card["classes"][0]["iri"],
                    "name": "Alice", "business_identifier": None, "evidence": evidence,
                },
                {
                    "candidate_id": prefix + ".entity.002", "class_iri": card["classes"][1]["iri"],
                    "name": "O-1", "business_identifier": None, "evidence": evidence,
                },
            ],
            "ambiguities": [], "failure": None,
        }

    def advance_to_assertion(self) -> tuple[dict, dict, dict]:
        envelope, card = self.advance_to_entity()
        entity_work = envelope["pending_work_details"][0]
        entity_result = self.entity_result(entity_work, card)
        completed = self.submit(entity_work, self.write_json("entities.json", entity_result))
        self.assertEqual(0, completed.returncode, completed.stderr)
        response = json.loads(completed.stdout)
        assertion_work = response["pending_work_details"][0]
        self.assertEqual("ASSERTION", assertion_work["stage"])
        return assertion_work, card, entity_result

    @staticmethod
    def evidence(quote: str, line: int = 2) -> dict:
        return {
            "source": "source.md", "heading_path": ["Orders"], "line_start": line, "line_end": line,
            "quote": quote,
        }

    def test_object_and_data_assertions_advance_to_candidate_critic(self) -> None:
        work, card, entity_result = self.advance_to_assertion()
        prefix = work["input"]["candidate_id_prefix"]
        quote = "Customer Alice placed order O-1 with amount 12.50."
        result_payload = {
            "version": 1, "chunk_id": work["input"]["chunk"]["chunk_id"], "status": "complete",
            "assertions": [
                {
                    "candidate_id": prefix + ".assertion.001", "kind": "object",
                    "subject_candidate_id": prefix + ".entity.001",
                    "property_iri": card["object_properties"][0]["iri"],
                    "object_candidate_id": prefix + ".entity.002", "evidence": self.evidence(quote),
                },
                {
                    "candidate_id": prefix + ".assertion.002", "kind": "data",
                    "subject_candidate_id": prefix + ".entity.002",
                    "property_iri": card["datatype_properties"][0]["iri"], "value": "12.50",
                    "datatype": XSD + "decimal", "evidence": self.evidence(quote),
                },
            ],
            "exclusions": [], "failure": None,
        }

        result = self.submit(work, self.write_json("assertions.json", result_payload))

        self.assertEqual(0, result.returncode, result.stderr)
        response = json.loads(result.stdout)
        self.assertEqual("ABOX_WORK", response["current_stage"])
        critic = response["pending_work_details"][0]
        self.assertEqual("CANDIDATE_CRITIC", critic["stage"])
        self.assertEqual(entity_result, critic["input"]["entity_result"])
        self.assertEqual(result_payload, critic["input"]["assertion_result"])
        status = self.run_cli("run", "status", "--output", str(self.output), "--json")
        rows = [row for row in json.loads(status.stdout)["work_item_results"] if row["stage"] == "ASSERTION"]
        self.assertEqual(result_payload, rows[0]["result"])

    def test_all_closed_exclusion_reasons_are_accepted(self) -> None:
        (self.workspace / "source.md").write_text(
            "# Orders\n"
            "Customer Alice did not place order O-1.\n"
            "For example, Customer Alice may place order O-1.\n"
            "Every customer must place an order.\n"
            "Customer Alice placed order O-99.\n"
            "Customer Alice placed order O-1 or O-2.\n"
            "Customer Alice filed order O-1.\n"
            "Order O-1 was placed on July 23, 2026.\n",
            encoding="utf-8",
        )
        work, card, _ = self.advance_to_assertion()
        prefix = work["input"]["candidate_id_prefix"]
        places = card["object_properties"][0]["iri"]
        reasons = [
            ("NEGATED_STATEMENT", [prefix + ".entity.001", prefix + ".entity.002"], places),
            ("EXAMPLE_OR_HYPOTHETICAL", [prefix + ".entity.001", prefix + ".entity.002"], places),
            ("GENERIC_OR_NORMATIVE_STATEMENT", [], places),
            ("UNLOCKED_ENTITY_REFERENCE", [prefix + ".entity.001"], places),
            ("ENTITY_REFERENCE_AMBIGUOUS", [prefix + ".entity.001"], places),
            ("PROPERTY_AMBIGUOUS", [prefix + ".entity.001", prefix + ".entity.002"], None),
            ("LITERAL_NOT_SCHEMA_TYPED", [prefix + ".entity.002"], card["datatype_properties"][1]["iri"]),
        ]
        quotes = work["input"]["chunk"]["primary"]["text"].splitlines()[1:]
        exclusions = [
            {
                "exclusion_id": f"{prefix}.exclusion.{ordinal:03d}", "reason": reason,
                "candidate_ids": candidate_ids, "property_iri": property_iri,
                "detail": "The explicit statement is not admissible for this primary reason.",
                "evidence": self.evidence(quotes[ordinal - 1], ordinal + 1),
            }
            for ordinal, (reason, candidate_ids, property_iri) in enumerate(reasons, start=1)
        ]
        payload = {
            "version": 1, "chunk_id": work["input"]["chunk"]["chunk_id"], "status": "complete",
            "assertions": [], "exclusions": exclusions, "failure": None,
        }

        result = self.submit(work, self.write_json("exclusions.json", payload))

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("CANDIDATE_CRITIC", json.loads(result.stdout)["pending_work_details"][0]["stage"])

    def test_empty_and_retryable_failure_results_remain_explicit(self) -> None:
        work, _, _ = self.advance_to_assertion()
        failure = {
            "version": 1, "chunk_id": work["input"]["chunk"]["chunk_id"],
            "status": "retryable_failure", "assertions": [], "exclusions": [],
            "failure": {"code": "CHUNK_PROVENANCE_INCONSISTENT", "detail": "Primary provenance conflicts."},
        }
        result = self.submit(work, self.write_json("failure.json", failure))
        self.assertEqual(0, result.returncode, result.stderr)
        response = json.loads(result.stdout)
        self.assertEqual(work["work_item_id"], response["pending_work_items"][0])
        self.assertEqual("retryable_failure", response["work_result"]["status"])

        empty = {
            "version": 1, "chunk_id": work["input"]["chunk"]["chunk_id"], "status": "complete",
            "assertions": [], "exclusions": [], "failure": None,
        }
        result = self.submit(work, self.write_json("empty.json", empty))
        self.assertEqual(0, result.returncode, result.stderr)
        critic_input = json.loads(result.stdout)["pending_work_details"][0]["input"]
        self.assertEqual([], critic_input["assertion_result"]["assertions"])

    def test_strict_schema_endpoints_typing_literals_and_identity_are_enforced(self) -> None:
        work, card, _ = self.advance_to_assertion()
        prefix = work["input"]["candidate_id_prefix"]
        quote = "Customer Alice placed order O-1 with amount 12.50."
        base = {
            "version": 1, "chunk_id": work["input"]["chunk"]["chunk_id"], "status": "complete",
            "assertions": [
                {
                    "candidate_id": prefix + ".assertion.001", "kind": "object",
                    "subject_candidate_id": prefix + ".entity.001",
                    "property_iri": card["object_properties"][0]["iri"],
                    "object_candidate_id": prefix + ".entity.002", "evidence": self.evidence(quote),
                }
            ],
            "exclusions": [], "failure": None,
        }
        invalid_payloads = []
        extra = json.loads(json.dumps(base))
        extra["assertions"][0]["confidence"] = 0.9
        invalid_payloads.append(extra)
        gapped_id = json.loads(json.dumps(base))
        gapped_id["assertions"][0]["candidate_id"] = prefix + ".assertion.002"
        invalid_payloads.append(gapped_id)
        entity_only_evidence = json.loads(json.dumps(base))
        entity_only_evidence["assertions"][0]["evidence"]["quote"] = "Customer Alice"
        invalid_payloads.append(entity_only_evidence)
        missing_endpoint = json.loads(json.dumps(base))
        missing_endpoint["assertions"][0]["object_candidate_id"] = prefix + ".entity.999"
        invalid_payloads.append(missing_endpoint)
        wrong_domain = json.loads(json.dumps(base))
        wrong_domain["assertions"][0]["subject_candidate_id"] = prefix + ".entity.002"
        invalid_payloads.append(wrong_domain)
        wrong_datatype = json.loads(json.dumps(base))
        wrong_datatype["assertions"] = [
            {
                "candidate_id": prefix + ".assertion.001", "kind": "data",
                "subject_candidate_id": prefix + ".entity.002",
                "property_iri": card["datatype_properties"][0]["iri"], "value": "12.50",
                "datatype": XSD + "double", "evidence": self.evidence(quote),
            }
        ]
        invalid_payloads.append(wrong_datatype)
        identity_rescue = json.loads(json.dumps(base))
        identity_rescue["assertions"] = [
            {
                "candidate_id": prefix + ".assertion.001", "kind": "data",
                "subject_candidate_id": prefix + ".entity.001",
                "property_iri": card["datatype_properties"][2]["iri"], "value": "C-1",
                "datatype": XSD + "string",
                "evidence": self.evidence("Customer Alice has customer ID C-1.", 3),
            }
        ]
        invalid_payloads.append(identity_rescue)

        snapshot = self.root / "assertion-pending"
        shutil.copytree(self.output, snapshot)
        for ordinal, payload in enumerate(invalid_payloads, start=1):
            with self.subTest(ordinal=ordinal):
                shutil.rmtree(self.output)
                shutil.copytree(snapshot, self.output)
                result = self.submit(work, self.write_json(f"invalid-{ordinal}.json", payload))
                self.assertEqual(4, result.returncode)
                self.assertEqual("invalid_submission", json.loads(result.stdout)["error_code"])

    def test_context_before_cannot_supply_exclusion_evidence(self) -> None:
        (self.workspace / "source.md").write_text(
            "# First\n" + "x" * 4100 + "\n\nCustomer Alice.\n# Second\nCustomer Bob.\n", encoding="utf-8"
        )
        envelope, _ = self.advance_to_entity()
        entity_work = next(
            item for item in envelope["pending_work_details"] if item["input"]["chunk"]["context_before"] is not None
        )
        target_chunk_id = entity_work["input"]["chunk"]["chunk_id"]
        completed = None
        for work in envelope["pending_work_details"]:
            empty_entities = {
                "version": 1,
                "chunk_id": work["input"]["chunk"]["chunk_id"],
                "status": "complete",
                "entities": [],
                "ambiguities": [],
                "failure": None,
            }
            completed = self.submit(
                work,
                self.write_json(
                    f"context-entities-{work['logical_sequence']}.json",
                    empty_entities,
                ),
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
        assert completed is not None
        assertion_work = next(
            item for item in json.loads(completed.stdout)["pending_work_details"]
            if item["stage"] == "ASSERTION"
            and item["input"]["chunk"]["chunk_id"] == target_chunk_id
        )
        prefix = assertion_work["input"]["candidate_id_prefix"]
        payload = {
            "version": 1, "chunk_id": assertion_work["input"]["chunk"]["chunk_id"], "status": "complete",
            "assertions": [],
            "exclusions": [
                {
                    "exclusion_id": prefix + ".exclusion.001", "reason": "NEGATED_STATEMENT",
                    "candidate_ids": [], "property_iri": None, "detail": "Context-only statement.",
                    "evidence": {
                        "source": "source.md", "heading_path": ["First"], "line_start": 4, "line_end": 4,
                        "quote": "Customer Alice.",
                    },
                }
            ],
            "failure": None,
        }

        result = self.submit(assertion_work, self.write_json("context-exclusion.json", payload))

        self.assertEqual(4, result.returncode)
        self.assertEqual("invalid_submission", json.loads(result.stdout)["error_code"])


if __name__ == "__main__":
    unittest.main()
