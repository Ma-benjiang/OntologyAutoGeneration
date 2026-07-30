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


class EntityPassCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.output = self.root / "output"
        self.workspace.mkdir()
        (self.workspace / "source.md").write_text(
            "# Customers\nCustomer Alice has customer ID C-001.\n", encoding="utf-8"
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
            "run", "start", "--workspace", str(self.workspace), "--output", str(self.output), "--source", "source.md"
        )
        self.assertEqual(0, started.returncode, started.stderr)
        envelope = json.loads(started.stdout)
        cq = self.root / "cq.md"
        cq.write_text("# CQs\n- Which customer has ID C-001?\n", encoding="utf-8")
        completed = self.submit(envelope["pending_work_details"][0], cq)
        self.assertEqual(0, completed.returncode, completed.stderr)
        envelope = json.loads(completed.stdout)
        srd = self.root / "srd.md"
        srd.write_text("# SRD\nCustomer has a stable customer ID.\n", encoding="utf-8")
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
                }
            ],
            "object_properties": [],
            "datatype_properties": [
                {
                    "iri": namespace + "customerId", "label": "customer ID", "comment": "Stable customer ID.",
                    "domain": namespace + "Customer", "range": XSD + "string", "subproperty_of": [],
                    "equivalent_properties": [], "max_count": 1, "identity": True,
                }
            ],
        }
        completed = self.submit(envelope["pending_work_details"][0], self.write_json("schema.json", card))
        self.assertEqual(0, completed.returncode, completed.stderr)
        envelope = json.loads(completed.stdout)
        self.assertEqual("ENTITY", envelope["pending_work_details"][0]["stage"])
        return envelope, card

    def entity_result(self, work: dict, card: dict) -> dict:
        prefix = work["input"]["candidate_id_prefix"]
        chunk = work["input"]["chunk"]
        return {
            "version": 1,
            "chunk_id": chunk["chunk_id"],
            "status": "complete",
            "entities": [
                {
                    "candidate_id": prefix + ".entity.001",
                    "class_iri": card["classes"][0]["iri"],
                    "name": "Alice",
                    "business_identifier": {
                        "property_iri": card["datatype_properties"][0]["iri"],
                        "value": "C-001",
                    },
                    "evidence": {
                        "source": "source.md", "heading_path": ["Customers"], "line_start": 2, "line_end": 2,
                        "quote": "Customer Alice has customer ID C-001.",
                    },
                }
            ],
            "ambiguities": [],
            "failure": None,
        }

    def test_complete_entity_result_advances_only_chunk_to_assertion(self) -> None:
        envelope, card = self.advance_to_entity()
        work = envelope["pending_work_details"][0]

        result = self.submit(work, self.write_json("entity.json", self.entity_result(work, card)))

        self.assertEqual(0, result.returncode, result.stderr)
        response = json.loads(result.stdout)
        self.assertEqual("ABOX_WORK", response["current_stage"])
        self.assertEqual("ASSERTION", response["pending_work_details"][0]["stage"])
        self.assertEqual(self.entity_result(work, card), response["work_result"])
        assertion_input = response["pending_work_details"][0]["input"]
        self.assertEqual(self.entity_result(work, card), assertion_input["entity_result"])
        self.assertNotIn("individual_iri", json.dumps(assertion_input))
        status = self.run_cli("run", "status", "--output", str(self.output), "--json")
        entity_rows = [row for row in json.loads(status.stdout)["work_item_results"] if row["stage"] == "ENTITY"]
        self.assertEqual(self.entity_result(work, card), entity_rows[0]["result"])

    def test_all_initial_entity_chunks_finish_before_assertion_pass_begins(self) -> None:
        (self.workspace / "source.md").write_text(
            "# First\n" + "x" * 4100 + "\n# Second\nCustomer Bob.\n",
            encoding="utf-8",
        )
        envelope, _ = self.advance_to_entity()
        entity_work = envelope["pending_work_details"]
        self.assertGreaterEqual(len(entity_work), 2)
        self.assertEqual({"ENTITY"}, {work["stage"] for work in entity_work})

        first = entity_work[0]
        first_result = {
            "version": 1,
            "chunk_id": first["input"]["chunk"]["chunk_id"],
            "status": "complete",
            "entities": [],
            "ambiguities": [],
            "failure": None,
        }
        submitted = self.submit(first, self.write_json("first-entity.json", first_result))

        self.assertEqual(0, submitted.returncode, submitted.stderr)
        after_first = json.loads(submitted.stdout)
        self.assertEqual(
            {"ENTITY"},
            {work["stage"] for work in after_first["pending_work_details"]},
        )

        envelope = after_first
        while envelope["pending_work_details"][0]["stage"] == "ENTITY":
            work = envelope["pending_work_details"][0]
            result = {
                "version": 1,
                "chunk_id": work["input"]["chunk"]["chunk_id"],
                "status": "complete",
                "entities": [],
                "ambiguities": [],
                "failure": None,
            }
            submitted = self.submit(
                work,
                self.write_json(
                    f"entity-{work['logical_sequence']}.json",
                    result,
                ),
            )
            self.assertEqual(0, submitted.returncode, submitted.stderr)
            envelope = json.loads(submitted.stdout)

        assertions = envelope["pending_work_details"]
        self.assertEqual(len(entity_work), len(assertions))
        self.assertEqual({"ASSERTION"}, {work["stage"] for work in assertions})
        self.assertEqual(
            sorted(work["logical_sequence"] for work in entity_work),
            [work["logical_sequence"] for work in assertions],
        )
        for work in assertions:
            self.assertEqual(
                work["input"]["chunk"]["chunk_id"],
                work["input"]["entity_result"]["chunk_id"],
            )
            self.assertNotIn("entity_results", work["input"])

    def test_strict_schema_and_primary_provenance_rejections_are_audited(self) -> None:
        envelope, card = self.advance_to_entity()
        work = envelope["pending_work_details"][0]
        snapshot = self.root / "entity-pending"
        shutil.copytree(self.output, snapshot)
        invalid_payloads = []
        extra = self.entity_result(work, card)
        extra["entities"][0]["confidence"] = 0.9
        invalid_payloads.append(extra)
        context = self.entity_result(work, card)
        context["entities"][0]["evidence"]["line_start"] = 1
        context["entities"][0]["evidence"]["line_end"] = 1
        invalid_payloads.append(context)
        gap = self.entity_result(work, card)
        gap["entities"][0]["candidate_id"] = work["input"]["candidate_id_prefix"] + ".entity.002"
        invalid_payloads.append(gap)

        for ordinal, invalid in enumerate(invalid_payloads, start=1):
            with self.subTest(ordinal=ordinal):
                shutil.rmtree(self.output)
                shutil.copytree(snapshot, self.output)
                result = self.submit(work, self.write_json(f"invalid-entity-{ordinal}.json", invalid))
                self.assertEqual(4, result.returncode)
                self.assertEqual("invalid_submission", json.loads(result.stdout)["error_code"])

    def test_class_ambiguity_and_retryable_results_remain_explicit(self) -> None:
        envelope, card = self.advance_to_entity()
        work = envelope["pending_work_details"][0]
        prefix = work["input"]["candidate_id_prefix"]
        chunk_id = work["input"]["chunk"]["chunk_id"]
        failure = {
            "version": 1, "chunk_id": chunk_id, "status": "retryable_failure", "entities": [], "ambiguities": [],
            "failure": {"code": "CHUNK_PROVENANCE_INCONSISTENT", "detail": "Reported provenance conflict."},
        }
        result = self.submit(work, self.write_json("failure.json", failure))
        self.assertEqual(0, result.returncode, result.stderr)
        response = json.loads(result.stdout)
        self.assertEqual(work["work_item_id"], response["pending_work_items"][0])
        self.assertEqual("retryable_failure", response["work_result"]["status"])
        status = self.run_cli("run", "status", "--output", str(self.output), "--json")
        self.assertEqual(1, json.loads(status.stdout)["pending_work_details"][0]["attempt_count"])

        ambiguity = {
            "version": 1, "chunk_id": chunk_id, "status": "complete", "entities": [],
            "ambiguities": [
                {
                    "ambiguity_id": prefix + ".ambiguity.001", "candidate_id": None, "field": "class_iri",
                    "mention": "Alice", "alternatives": [card["classes"][0]["iri"]],
                    "reason": "The mention is not classifiable without guessing.",
                    "evidence": {
                        "source": "source.md", "heading_path": ["Customers"], "line_start": 2, "line_end": 2,
                        "quote": "Customer Alice has customer ID C-001.",
                    },
                }
            ],
            "failure": None,
        }
        result = self.submit(work, self.write_json("ambiguity.json", ambiguity))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("ASSERTION", json.loads(result.stdout)["pending_work_details"][0]["stage"])

    def test_valid_empty_result_advances_without_candidates(self) -> None:
        envelope, _ = self.advance_to_entity()
        work = envelope["pending_work_details"][0]
        empty = {
            "version": 1, "chunk_id": work["input"]["chunk"]["chunk_id"], "status": "complete",
            "entities": [], "ambiguities": [], "failure": None,
        }

        result = self.submit(work, self.write_json("empty.json", empty))

        self.assertEqual(0, result.returncode, result.stderr)
        response = json.loads(result.stdout)
        self.assertEqual("ASSERTION", response["pending_work_details"][0]["stage"])
        self.assertEqual([], response["pending_work_details"][0]["input"]["entity_result"]["entities"])

    def test_identifier_ambiguity_keeps_entity_with_null_identifier(self) -> None:
        (self.workspace / "source.md").write_text(
            "# Customers\nCustomer Alice is associated with IDs C-001 and C-007.\n", encoding="utf-8"
        )
        envelope, card = self.advance_to_entity()
        work = envelope["pending_work_details"][0]
        prefix = work["input"]["candidate_id_prefix"]
        quote = "Customer Alice is associated with IDs C-001 and C-007."
        evidence = {
            "source": "source.md", "heading_path": ["Customers"], "line_start": 2, "line_end": 2, "quote": quote,
        }
        result_payload = {
            "version": 1, "chunk_id": work["input"]["chunk"]["chunk_id"], "status": "complete",
            "entities": [
                {
                    "candidate_id": prefix + ".entity.001", "class_iri": card["classes"][0]["iri"],
                    "name": "Alice", "business_identifier": None, "evidence": evidence,
                }
            ],
            "ambiguities": [
                {
                    "ambiguity_id": prefix + ".ambiguity.001", "candidate_id": prefix + ".entity.001",
                    "field": "business_identifier", "mention": "IDs C-001 and C-007",
                    "alternatives": ["C-001", "C-007"], "reason": "No unique identifier is selected.",
                    "evidence": evidence,
                }
            ],
            "failure": None,
        }

        result = self.submit(work, self.write_json("identity-ambiguity.json", result_payload))

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIsNone(json.loads(result.stdout)["work_result"]["entities"][0]["business_identifier"])

    def test_context_before_cannot_supply_entity_evidence(self) -> None:
        (self.workspace / "source.md").write_text(
            "# First\n" + "x" * 4100 + "\n\nCustomer Alice.\n# Second\nCustomer Bob.\n", encoding="utf-8"
        )
        envelope, card = self.advance_to_entity()
        work = next(item for item in envelope["pending_work_details"] if item["input"]["chunk"]["context_before"] is not None)
        prefix = work["input"]["candidate_id_prefix"]
        context_candidate = {
            "version": 1, "chunk_id": work["input"]["chunk"]["chunk_id"], "status": "complete",
            "entities": [
                {
                    "candidate_id": prefix + ".entity.001", "class_iri": card["classes"][0]["iri"],
                    "name": "Alice", "business_identifier": None,
                    "evidence": {
                        "source": "source.md", "heading_path": ["First"], "line_start": 4, "line_end": 4,
                        "quote": "Customer Alice.",
                    },
                }
            ],
            "ambiguities": [], "failure": None,
        }

        result = self.submit(work, self.write_json("context-only.json", context_candidate))

        self.assertEqual(4, result.returncode)
        self.assertEqual("invalid_submission", json.loads(result.stdout)["error_code"])


if __name__ == "__main__":
    unittest.main()
