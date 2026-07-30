from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
CLI = SKILL_DIR / "scripts" / "ontology_pipeline.py"
XSD = "http://www.w3.org/2001/XMLSchema#"
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from ontology_pipeline import PipelineError, validate_schema_card


class GlobalWorkItemCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.output = self.root / "output"
        self.workspace.mkdir()
        (self.workspace / "source.md").write_text("# Orders\nOrder O-1 belongs to Alice.\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            cwd=self.root,
            text=True,
            capture_output=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )

    def start(self) -> dict:
        result = self.run_cli(
            "run", "start", "--workspace", str(self.workspace), "--output", str(self.output), "--source", "source.md"
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout)

    def status(self) -> dict:
        result = self.run_cli("run", "status", "--output", str(self.output), "--json")
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout)

    def submit(self, result_path: Path, *, work: dict | None = None) -> subprocess.CompletedProcess[str]:
        work = work or self.status()["pending_work_details"][0]
        return self.run_cli(
            "run", "submit", "--output", str(self.output), "--work-item-id", work["work_item_id"],
            "--input-digest", work["input_digest"], "--result", str(result_path),
        )

    def write_text(self, name: str, value: str) -> Path:
        path = self.root / name
        path.write_text(value, encoding="utf-8")
        return path

    def schema_card(self, **changes: object) -> dict:
        project = json.loads((self.output / "project.json").read_text())
        namespace = project["entity_namespace"]
        card = {
            "version": 1,
            "ontology_iri": project["ontology_iri"],
            "entity_namespace": namespace,
            "classes": [
                {
                    "iri": namespace + "Order", "label": "Order", "comment": "A business order.",
                    "superclasses": [], "equivalent_classes": [], "disjoint_with": [],
                }
            ],
            "object_properties": [],
            "datatype_properties": [
                {
                    "iri": namespace + "orderId", "label": "order ID", "comment": "Stable order identifier.",
                    "domain": namespace + "Order", "range": XSD + "string", "subproperty_of": [],
                    "equivalent_properties": [], "max_count": 1, "identity": True,
                }
            ],
        }
        card.update(changes)
        return card

    def advance_to_schema(self) -> tuple[dict, dict, dict]:
        started = self.start()
        cq_work = self.status()["pending_work_details"][0]
        cq = self.write_text("cq.md", "\ufeff# CQs\r\n- Which order belongs to Alice?\r\n")
        cq_result = self.submit(cq, work=cq_work)
        self.assertEqual(0, cq_result.returncode, cq_result.stderr)
        srd_work = json.loads(cq_result.stdout)["pending_work_details"][0]
        srd = self.write_text("srd.md", "# SRD\nOrder has an order ID.\n")
        srd_result = self.submit(srd, work=srd_work)
        self.assertEqual(0, srd_result.returncode, srd_result.stderr)
        schema_work = json.loads(srd_result.stdout)["pending_work_details"][0]
        return started, cq_work, schema_work

    def test_global_chain_locks_schema_and_creates_entity_work(self) -> None:
        started, cq_work, schema_work = self.advance_to_schema()
        schema_path = self.write_text("schema.json", json.dumps(self.schema_card()))

        result = self.submit(schema_path, work=schema_work)

        self.assertEqual(0, result.returncode, result.stderr)
        envelope = json.loads(result.stdout)
        self.assertEqual("SCHEMA_LOCKED", envelope["current_stage"])
        self.assertTrue(all(item["stage"] == "ENTITY" for item in envelope["pending_work_details"]))
        run_root = self.output / ".staging" / started["run_id"]
        lock = json.loads((run_root / "artifacts" / "schema_lock.json").read_text())
        expected = json.loads((run_root / "artifacts" / "expected_abox_chunks.json").read_text())
        expected_schema = json.dumps(self.schema_card(), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        self.assertEqual(hashlib.sha256(expected_schema.encode("utf-8")).hexdigest(), lock["schema_card_sha256"])
        self.assertEqual(1, len(expected["chunk_ids"]))
        self.assertTrue((run_root / "artifacts" / "dynamic_shapes.ttl").read_text().endswith("\n"))

        attempt = run_root / "work_items" / cq_work["work_item_id"] / "attempts" / "0001"
        self.assertEqual("# CQs\n- Which order belongs to Alice?\n", (attempt / "normalized_output.md").read_text())
        manifest = json.loads((attempt / "attempt.json").read_text())
        self.assertEqual(cq_work["input_digest"], manifest["input_digest"])
        self.assertEqual(64, len(manifest["raw_output_sha256"]))
        self.assertEqual(64, len(manifest["normalized_output_sha256"]))

    def test_submit_rejects_unknown_mismatch_duplicate_and_late_without_new_attempt(self) -> None:
        self.start()
        current = self.status()["pending_work_details"][0]
        cq = self.write_text("cq.md", "# CQs\n")

        unknown = dict(current, work_item_id="work-v1-cq-" + "0" * 64)
        result = self.submit(cq, work=unknown)
        self.assertEqual(4, result.returncode)
        self.assertEqual("unknown_work_item", json.loads(result.stdout)["error_code"])

        traversal = dict(current, work_item_id="../../project.json")
        result = self.submit(cq, work=traversal)
        self.assertEqual(4, result.returncode)
        self.assertEqual("unknown_work_item", json.loads(result.stdout)["error_code"])

        mismatch = dict(current, input_digest="f" * 64)
        result = self.submit(cq, work=mismatch)
        self.assertEqual(4, result.returncode)
        self.assertEqual("input_mismatch", json.loads(result.stdout)["error_code"])

        accepted = self.submit(cq, work=current)
        self.assertEqual(0, accepted.returncode, accepted.stderr)
        result = self.submit(cq, work=current)
        self.assertEqual(4, result.returncode)
        self.assertEqual("duplicate_submission", json.loads(result.stdout)["error_code"])
        attempts = self.output / ".staging" / json.loads(accepted.stdout)["run_id"] / "work_items" / current["work_item_id"] / "attempts"
        self.assertEqual(["0001"], sorted(path.name for path in attempts.iterdir()))

    def test_resume_rejects_tampered_attempt_artifact(self) -> None:
        started = self.start()
        current = self.status()["pending_work_details"][0]
        accepted = self.submit(self.write_text("cq.md", "# CQs\n"), work=current)
        self.assertEqual(0, accepted.returncode, accepted.stderr)
        raw = (
            self.output / ".staging" / started["run_id"] / "work_items" / current["work_item_id"]
            / "attempts" / "0001" / "raw_output.md"
        )
        raw.write_text("tampered\n", encoding="utf-8")

        result = self.run_cli("run", "resume", "--output", str(self.output))

        self.assertEqual(5, result.returncode)
        self.assertEqual("ledger_corrupt", json.loads(result.stdout)["error_code"])

    def test_schema_lock_rejects_forbidden_datatype_and_semantic_contradiction(self) -> None:
        _, _, schema_work = self.advance_to_schema()
        card = self.schema_card()
        card["datatype_properties"][0]["range"] = XSD + "duration"
        invalid = self.write_text("invalid-schema.json", json.dumps(card))
        result = self.submit(invalid, work=schema_work)
        self.assertEqual(4, result.returncode)
        self.assertEqual("SCHEMA_CARD_INVALID", json.loads(result.stdout)["error_code"])

    def test_semantic_closure_rejects_subproperty_inverse_and_identity_conflicts(self) -> None:
        self.start()
        base = self.schema_card()
        namespace = base["entity_namespace"]
        base["classes"].append(
            {
                "iri": namespace + "Customer", "label": "Customer", "comment": "A customer.",
                "superclasses": [], "equivalent_classes": [], "disjoint_with": [],
            }
        )

        subproperty_cycle = json.loads(json.dumps(base))
        subproperty_cycle["datatype_properties"].extend(
            [
                {
                    "iri": namespace + "codeA", "label": "code A", "comment": "Code A.",
                    "domain": namespace + "Order", "range": XSD + "string", "subproperty_of": [namespace + "codeB"],
                    "equivalent_properties": [], "identity": False,
                },
                {
                    "iri": namespace + "codeB", "label": "code B", "comment": "Code B.",
                    "domain": namespace + "Order", "range": XSD + "string", "subproperty_of": [namespace + "codeA"],
                    "equivalent_properties": [], "identity": False,
                },
            ]
        )

        inverse_mismatch = json.loads(json.dumps(base))
        inverse_mismatch["object_properties"] = [
            {
                "iri": namespace + "owns", "label": "owns", "comment": "Owns an order.",
                "domain": namespace + "Customer", "range": namespace + "Order", "subproperty_of": [],
                "equivalent_properties": [], "inverse_of": [namespace + "ownedBy"],
            },
            {
                "iri": namespace + "ownedBy", "label": "owned by", "comment": "Owned by a customer.",
                "domain": namespace + "Customer", "range": namespace + "Order", "subproperty_of": [],
                "equivalent_properties": [], "inverse_of": [namespace + "owns"],
            },
        ]

        identity_mismatch = json.loads(json.dumps(base))
        identity_mismatch["datatype_properties"].append(
            {
                "iri": namespace + "externalId", "label": "external ID", "comment": "Equivalent external ID.",
                "domain": namespace + "Order", "range": XSD + "string", "subproperty_of": [],
                "equivalent_properties": [namespace + "orderId"], "max_count": 1, "identity": False,
            }
        )
        identity_mismatch["datatype_properties"][0]["equivalent_properties"] = [namespace + "externalId"]

        for card in (subproperty_cycle, inverse_mismatch, identity_mismatch):
            with self.subTest(card=card), self.assertRaises(PipelineError):
                validate_schema_card(card)

        folded = json.loads(json.dumps(base))
        folded["classes"][0]["equivalent_classes"] = [namespace + "Customer"]
        folded["classes"][0]["superclasses"] = [namespace + "Customer"]
        folded["classes"][1]["equivalent_classes"] = [namespace + "Order"]
        folded["classes"][1]["superclasses"] = [namespace + "Order"]
        validate_schema_card(folded)

        # A new run verifies folded class equivalence/disjointness independently.
        self.run_cli("run", "abort", "--output", str(self.output))
        _, _, schema_work = self.advance_to_schema()
        card = self.schema_card()
        namespace = card["entity_namespace"]
        card["classes"].append(
            {
                "iri": namespace + "Purchase", "label": "Purchase", "comment": "Equivalent purchase.",
                "superclasses": [], "equivalent_classes": [namespace + "Order"], "disjoint_with": [namespace + "Order"],
            }
        )
        card["classes"][0]["equivalent_classes"] = [namespace + "Purchase"]
        contradiction = self.write_text("contradiction.json", json.dumps(card))
        result = self.submit(contradiction, work=schema_work)
        self.assertEqual(4, result.returncode)
        self.assertEqual("SCHEMA_CARD_INVALID", json.loads(result.stdout)["error_code"])


if __name__ == "__main__":
    unittest.main()
