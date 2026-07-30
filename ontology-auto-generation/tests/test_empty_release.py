from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from rdflib import Graph, RDF, OWL, URIRef

try:
    from release_goldens import assert_release_golden, seed_golden_identity
except ImportError:
    from .release_goldens import assert_release_golden, seed_golden_identity


SKILL_DIR = Path(__file__).resolve().parents[1]
CLI = SKILL_DIR / "scripts" / "ontology_pipeline.py"


class EmptyReleaseCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.output = self.root / "output"
        self.workspace.mkdir()
        (self.workspace / "empty.md").write_text("", encoding="utf-8")
        seed_golden_identity(self.output)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CLI), *args], cwd=self.root, text=True, capture_output=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )

    def submit(self, work: dict, result: Path) -> dict:
        completed = self.run_cli(
            "run", "submit", "--output", str(self.output), "--work-item-id", work["work_item_id"],
            "--input-digest", work["input_digest"], "--result", str(result),
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        envelope = json.loads(completed.stdout)
        pending = envelope.get("pending_work_details", [])
        if len(pending) == 1 and pending[0]["stage"] == "QA_GATE_1":
            qa_path = self.root / f"qa-{pending[0]['work_item_id']}.json"
            qa_path.write_text(
                json.dumps({
                    "version": 1, "round": pending[0]["input"]["round"],
                    "status": "PASS", "findings": [],
                }),
                encoding="utf-8",
            )
            return self.submit(pending[0], qa_path)
        return envelope

    def build_empty_pass(self) -> dict:
        started = self.run_cli(
            "run", "start", "--workspace", str(self.workspace), "--output", str(self.output), "--source", "empty.md"
        )
        self.assertEqual(0, started.returncode, started.stderr)
        envelope = json.loads(started.stdout)

        cq = self.root / "cqs.md"
        cq.write_text("# CQs\n\nNo instance questions are supported by the empty source.\n", encoding="utf-8")
        envelope = self.submit(envelope["pending_work_details"][0], cq)
        srd = self.root / "srd.md"
        srd.write_text("# SRD\n\nNo source-backed terms are available.\n", encoding="utf-8")
        envelope = self.submit(envelope["pending_work_details"][0], srd)

        project = json.loads((self.output / "project.json").read_text())
        card = {
            "version": 1,
            "ontology_iri": project["ontology_iri"],
            "entity_namespace": project["entity_namespace"],
            "classes": [],
            "object_properties": [],
            "datatype_properties": [],
        }
        schema = self.root / "schema.json"
        schema.write_text(json.dumps(card), encoding="utf-8")
        return self.submit(envelope["pending_work_details"][0], schema)

    def test_empty_full_rebuild_publishes_and_deduplicates_pass_snapshot(self) -> None:
        first = self.build_empty_pass()

        self.assertEqual("PASS", first["delivery_status"])
        self.assertEqual("COMPLETE", first["run_state"])
        self.assertEqual([], first["pending_work_items"])
        snapshot_id = first["snapshot_id"]
        release = self.output / "releases" / snapshot_id
        manifest = json.loads((release / "release_manifest.json").read_text())
        self.assertEqual(snapshot_id, hashlib.sha256(
            json.dumps(manifest["artifacts"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest())
        self.assertEqual(sorted(row["path"] for row in manifest["artifacts"]), [row["path"] for row in manifest["artifacts"]])
        artifact_paths = {row["path"] for row in manifest["artifacts"]}
        self.assertTrue(
            {
                "manifests/abox/empty.md.json", "manifests/tbox/empty.md.json",
                "artifacts/cqs.md", "artifacts/srd.md", "artifacts/schema_card.json",
                "artifacts/dynamic_shapes.ttl", "artifacts/coverage.json",
                "artifacts/resolved_instances.json", "artifacts/evidence.jsonl",
                "artifacts/rejections.jsonl", "artifacts/qa_report.json",
                "schema.owl", "instances.owl", "ontology.owl", "delivery_status.json",
            }.issubset(artifact_paths)
        )
        self.assertTrue(any(path.startswith("work_items/") for path in artifact_paths))
        for artifact in manifest["artifacts"]:
            self.assertEqual(
                artifact["sha256"], hashlib.sha256((release / artifact["path"]).read_bytes()).hexdigest()
            )

        coverage = json.loads((release / "artifacts" / "coverage.json").read_text())
        delivery = json.loads((release / "delivery_status.json").read_text())
        qa = json.loads((release / "artifacts" / "qa_report.json").read_text())
        self.assertEqual("COMPLETE", coverage["status"])
        self.assertEqual([], coverage["expected_chunk_ids"])
        self.assertEqual([], (release / "artifacts" / "failed_chunks.jsonl").read_text().splitlines())
        self.assertEqual([], delivery["reason_codes"])
        self.assertEqual("PASS", qa["status"])
        self.assertEqual(hashlib.sha256((release / "ontology.owl").read_bytes()).hexdigest(), delivery["ontology_sha256"])

        schema_graph = Graph().parse(release / "schema.owl", format="xml")
        instance_graph = Graph().parse(release / "instances.owl", format="xml")
        combined_graph = Graph().parse(release / "ontology.owl", format="xml")
        declaration = (URIRef(delivery["ontology_iri"]), RDF.type, OWL.Ontology)
        self.assertEqual(0, len(instance_graph))
        self.assertEqual(set(schema_graph) | set(instance_graph) | {declaration}, set(combined_graph))
        self.assertEqual([], list(combined_graph.subjects(RDF.type, OWL.NamedIndividual)))

        attempt = json.loads((self.output / "latest_attempt.json").read_text())
        latest = json.loads((self.output / "latest_delivery.json").read_text())
        self.assertEqual(snapshot_id, attempt["snapshot_id"])
        self.assertEqual(snapshot_id, latest["snapshot_id"])

        second = self.build_empty_pass()
        self.assertEqual(snapshot_id, second["snapshot_id"])
        assert_release_golden(
            self,
            "pass",
            output=self.output,
            workspace=self.workspace,
            terminal_envelope=second,
            run_cli=self.run_cli,
        )


if __name__ == "__main__":
    unittest.main()
