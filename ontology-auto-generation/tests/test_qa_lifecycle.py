from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

try:
    from release_goldens import assert_release_golden, seed_golden_identity
except ImportError:
    from .release_goldens import assert_release_golden, seed_golden_identity


SKILL_DIR = Path(__file__).resolve().parents[1]
CLI = SKILL_DIR / "scripts" / "ontology_pipeline.py"
sys.path.insert(0, str(SKILL_DIR / "scripts"))
SPEC = importlib.util.spec_from_file_location("ontology_pipeline_qa", CLI)
pipeline = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(pipeline)


class QaLifecycleCliTest(unittest.TestCase):
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

    def write_json(self, name: str, value: dict) -> Path:
        path = self.root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def submit(self, work: dict, value: dict | str, name: str) -> dict:
        result = self.root / name
        if isinstance(value, str):
            result.write_text(value, encoding="utf-8")
        else:
            result.write_text(json.dumps(value), encoding="utf-8")
        completed = self.run_cli(
            "run", "submit", "--output", str(self.output), "--work-item-id", work["work_item_id"],
            "--input-digest", work["input_digest"], "--result", str(result),
        )
        self.assertEqual(0, completed.returncode, completed.stderr or completed.stdout)
        return json.loads(completed.stdout)

    def drive_to_qa(self, card: dict | None = None) -> tuple[dict, dict]:
        started = self.run_cli(
            "run", "start", "--workspace", str(self.workspace), "--output", str(self.output),
            "--source", "empty.md",
        )
        self.assertEqual(0, started.returncode, started.stderr)
        envelope = json.loads(started.stdout)
        envelope = self.submit(envelope["pending_work_details"][0], "# CQs\n\nNo facts.\n", "cqs.md")
        envelope = self.submit(envelope["pending_work_details"][0], "# SRD\n\nNo terms.\n", "srd.md")
        project = json.loads((self.output / "project.json").read_text())
        if card is None:
            card = {
                "version": 1,
                "ontology_iri": project["ontology_iri"],
                "entity_namespace": project["entity_namespace"],
                "classes": [],
                "object_properties": [],
                "datatype_properties": [],
            }
        envelope = self.submit(envelope["pending_work_details"][0], card, "schema.json")
        self.assertEqual("ACTIVE", envelope["run_state"])
        self.assertEqual("QA_GATE_1", envelope["pending_work_details"][0]["stage"])
        return envelope, card

    @staticmethod
    def gate_result(round_number: int, findings: list[dict] | None = None) -> dict:
        findings = findings or []
        return {
            "version": 1,
            "round": round_number,
            "status": "FAIL" if findings else "PASS",
            "findings": findings,
        }

    def test_gate1_pass_publishes_python_aggregated_pass(self) -> None:
        envelope, _ = self.drive_to_qa()
        qa_work = envelope["pending_work_details"][0]
        self.assertEqual({"gate_2": "PASS", "gate_3": "PASS"}, qa_work["input"]["deterministic_gates"])
        completed = self.submit(qa_work, self.gate_result(1), "qa-pass.json")

        self.assertEqual("COMPLETE", completed["run_state"])
        self.assertEqual("PASS", completed["delivery_status"])
        release = self.output / "releases" / completed["snapshot_id"]
        qa = json.loads((release / "artifacts" / "qa_report.json").read_text())
        self.assertEqual({"gate_1": "PASS", "gate_2": "PASS", "gate_3": "PASS"}, qa["gates"])
        self.assertEqual(1, qa["round"])
        self.assertEqual([], qa["gate_1_findings"])

    def test_deterministic_gate_failure_stops_before_gate1_or_fixer(self) -> None:
        started = self.run_cli(
            "run", "start", "--workspace", str(self.workspace), "--output", str(self.output),
            "--source", "empty.md",
        )
        envelope = json.loads(started.stdout)
        envelope = self.submit(envelope["pending_work_details"][0], "# CQs\n", "deterministic-cqs.md")
        envelope = self.submit(envelope["pending_work_details"][0], "# SRD\n", "deterministic-srd.md")
        project = json.loads((self.output / "project.json").read_text())
        card = {
            "version": 1,
            "ontology_iri": project["ontology_iri"],
            "entity_namespace": project["entity_namespace"],
            "classes": [],
            "object_properties": [],
            "datatype_properties": [],
        }
        schema_result = self.write_json("deterministic-schema.json", card)
        schema_work = envelope["pending_work_details"][0]
        deterministic_failure = {
            "version": 1,
            "ontology_parseable": True,
            "gate_2": "FAIL",
            "gate_3": "PASS",
            "errors": ["OWL_RL: inconsistent ontology"],
        }
        with mock.patch.object(
            pipeline, "_evaluate_deterministic_gates", return_value=deterministic_failure
        ):
            completed = pipeline._run_submit(
                SimpleNamespace(
                    output_dir=self.output,
                    work_item_id=schema_work["work_item_id"],
                    input_digest=schema_work["input_digest"],
                    result=schema_result,
                )
            )

        self.assertEqual("COMPLETE", completed["run_state"])
        self.assertEqual("FORCED_WITH_ERRORS", completed["delivery_status"])
        self.assertEqual([], completed["pending_work_items"])
        release = self.output / "releases" / completed["snapshot_id"]
        qa = json.loads((release / "artifacts" / "qa_report.json").read_text())
        self.assertEqual(
            {"gate_1": "NOT_RUN", "gate_2": "FAIL", "gate_3": "PASS"}, qa["gates"]
        )
        stages = {
            json.loads(path.read_text())["stage"]
            for path in release.glob("work_items/*/state.json")
        }
        self.assertNotIn("QA_GATE_1", stages)
        self.assertNotIn("FIXER", stages)

    def test_qa_and_fixer_results_are_strict_closed_contracts(self) -> None:
        envelope, card = self.drive_to_qa()
        qa_work = envelope["pending_work_details"][0]
        invalid_qa = self.write_json(
            "invalid-qa.json",
            {**self.gate_result(1), "confidence": 1.0},
        )
        rejected = self.run_cli(
            "run", "submit", "--output", str(self.output), "--work-item-id", qa_work["work_item_id"],
            "--input-digest", qa_work["input_digest"], "--result", str(invalid_qa),
        )
        self.assertEqual(4, rejected.returncode)
        self.assertEqual("invalid_submission", json.loads(rejected.stdout)["error_code"])

        finding = {
            "reason_code": "SCHEMA_STRICTNESS", "target": "SCHEMA_CARD",
            "detail": "A strict replacement is required.",
        }
        envelope = self.submit(qa_work, self.gate_result(1, [finding]), "strict-qa.json")
        fixer = envelope["pending_work_details"][0]
        invalid_fixer = self.write_json(
            "invalid-fixer.json",
            {
                "version": 1, "round": 1, "target": "SCHEMA_CARD",
                "replacement": card, "candidate_patch": {},
            },
        )
        rejected = self.run_cli(
            "run", "submit", "--output", str(self.output), "--work-item-id", fixer["work_item_id"],
            "--input-digest", fixer["input_digest"], "--result", str(invalid_fixer),
        )
        self.assertEqual(4, rejected.returncode)
        self.assertEqual("invalid_submission", json.loads(rejected.stdout)["error_code"])

    def test_unlocated_finding_stops_without_fixer(self) -> None:
        envelope, _ = self.drive_to_qa()
        finding = {
            "reason_code": "SEMANTIC_MISMATCH", "target": "UNLOCATED",
            "detail": "The finding has no legal upstream repair target.",
        }
        completed = self.submit(
            envelope["pending_work_details"][0], self.gate_result(1, [finding]), "qa-abox-fail.json"
        )
        self.assertEqual("COMPLETE", completed["run_state"])
        self.assertEqual("FORCED_WITH_ERRORS", completed["delivery_status"])
        self.assertEqual([], completed["pending_work_items"])

    def test_unchanged_fixer_stops_early(self) -> None:
        envelope, card = self.drive_to_qa()
        finding = {
            "reason_code": "SCHEMA_TERM_UNSUPPORTED", "target": "SCHEMA_CARD",
            "detail": "The Schema Card must be replaced.",
        }
        envelope = self.submit(
            envelope["pending_work_details"][0], self.gate_result(1, [finding]), "qa-schema-fail.json"
        )
        fixer = envelope["pending_work_details"][0]
        self.assertEqual("FIXER", fixer["stage"])
        completed = self.submit(
            fixer,
            {"version": 1, "round": 1, "target": "SCHEMA_CARD", "replacement": card},
            "fixer-unchanged.json",
        )
        self.assertEqual("FORCED_WITH_ERRORS", completed["delivery_status"])
        release = self.output / "releases" / completed["snapshot_id"]
        qa = json.loads((release / "artifacts" / "qa_report.json").read_text())
        self.assertIn("FIXER_NO_CHANGE", qa["reason_codes"])

    def test_schema_card_repair_rebuilds_and_passes_second_round(self) -> None:
        envelope, card = self.drive_to_qa()
        finding = {
            "reason_code": "SCHEMA_TERM_MISSING", "target": "SCHEMA_CARD",
            "detail": "Add the source-backed class.",
        }
        envelope = self.submit(
            envelope["pending_work_details"][0], self.gate_result(1, [finding]), "qa-repair.json"
        )
        repaired = json.loads(json.dumps(card))
        repaired["classes"].append(
            {
                "iri": repaired["entity_namespace"] + "Record", "label": "Record", "comment": "A record.",
                "superclasses": [], "equivalent_classes": [], "disjoint_with": [],
            }
        )
        envelope = self.submit(
            envelope["pending_work_details"][0],
            {"version": 1, "round": 1, "target": "SCHEMA_CARD", "replacement": repaired},
            "fixer-schema.json",
        )
        qa_work = envelope["pending_work_details"][0]
        self.assertEqual("QA_GATE_1", qa_work["stage"])
        self.assertEqual(2, qa_work["input"]["round"])
        completed = self.submit(qa_work, self.gate_result(2), "qa-round-2-pass.json")
        self.assertEqual("PASS", completed["delivery_status"])
        release = self.output / "releases" / completed["snapshot_id"]
        qa = json.loads((release / "artifacts" / "qa_report.json").read_text())
        self.assertEqual(2, qa["round"])
        self.assertEqual(repaired, json.loads((release / "artifacts" / "schema_card.json").read_text()))

    def test_cq_repair_cascades_to_a_new_srd_work_item(self) -> None:
        envelope, _ = self.drive_to_qa()
        finding = {
            "reason_code": "CQ_INCOMPLETE", "target": "CQ",
            "detail": "Replace the complete competency-question document.",
        }
        envelope = self.submit(
            envelope["pending_work_details"][0], self.gate_result(1, [finding]), "qa-cq-fail.json"
        )
        envelope = self.submit(
            envelope["pending_work_details"][0],
            {"version": 1, "round": 1, "target": "CQ", "replacement": "# CQs\n\nRepaired CQ.\n"},
            "fixer-cq.json",
        )
        self.assertEqual("SRD", envelope["pending_work_details"][0]["stage"])
        self.assertEqual(2, json.loads(
            (self.output / ".staging" / envelope["run_id"] / "artifacts" / "qa_state.json").read_text()
        )["round"])

    def test_srd_repair_cascades_to_a_new_schema_card_work_item(self) -> None:
        envelope, _ = self.drive_to_qa()
        finding = {
            "reason_code": "SRD_INCOMPLETE", "target": "SRD",
            "detail": "Replace the complete semantic requirements document.",
        }
        envelope = self.submit(
            envelope["pending_work_details"][0], self.gate_result(1, [finding]), "qa-srd-fail.json"
        )
        envelope = self.submit(
            envelope["pending_work_details"][0],
            {"version": 1, "round": 1, "target": "SRD", "replacement": "# SRD\n\nRepaired SRD.\n"},
            "fixer-srd.json",
        )
        self.assertEqual("SCHEMA_CARD", envelope["pending_work_details"][0]["stage"])

    def test_repeated_findings_stop_before_another_fixer(self) -> None:
        envelope, card = self.drive_to_qa()
        finding = {
            "reason_code": "SCHEMA_REVIEW_REPEAT", "target": "SCHEMA_CARD",
            "detail": "The same structural issue remains.",
        }
        envelope = self.submit(
            envelope["pending_work_details"][0], self.gate_result(1, [finding]), "qa-repeat-1.json"
        )
        repaired = json.loads(json.dumps(card))
        repaired["classes"].append(
            {
                "iri": repaired["entity_namespace"] + "Record", "label": "Record", "comment": "A record.",
                "superclasses": [], "equivalent_classes": [], "disjoint_with": [],
            }
        )
        envelope = self.submit(
            envelope["pending_work_details"][0],
            {"version": 1, "round": 1, "target": "SCHEMA_CARD", "replacement": repaired},
            "fixer-repeat.json",
        )
        completed = self.submit(
            envelope["pending_work_details"][0], self.gate_result(2, [finding]), "qa-repeat-2.json"
        )
        self.assertEqual("FORCED_WITH_ERRORS", completed["delivery_status"])
        release = self.output / "releases" / completed["snapshot_id"]
        qa = json.loads((release / "artifacts" / "qa_report.json").read_text())
        self.assertIn("REPEATED_FINDINGS", qa["reason_codes"])

    def test_twentieth_failed_round_never_creates_another_fixer(self) -> None:
        envelope, card = self.drive_to_qa()
        current = card
        for round_number in range(1, 20):
            finding = {
                "reason_code": f"ROUND_{round_number:02d}", "target": "SCHEMA_CARD",
                "detail": f"Round {round_number} requires a distinct complete replacement.",
            }
            envelope = self.submit(
                envelope["pending_work_details"][0],
                self.gate_result(round_number, [finding]),
                f"qa-cap-{round_number:02d}.json",
            )
            revised = json.loads(json.dumps(current))
            revised["classes"].append(
                {
                    "iri": revised["entity_namespace"] + f"Term{round_number:02d}",
                    "label": f"Term {round_number}", "comment": f"Round {round_number} term.",
                    "superclasses": [], "equivalent_classes": [], "disjoint_with": [],
                }
            )
            envelope = self.submit(
                envelope["pending_work_details"][0],
                {
                    "version": 1, "round": round_number,
                    "target": "SCHEMA_CARD", "replacement": revised,
                },
                f"fixer-cap-{round_number:02d}.json",
            )
            self.assertEqual(round_number + 1, envelope["pending_work_details"][0]["input"]["round"])
            current = revised

        final_finding = {
            "reason_code": "ROUND_20", "target": "SCHEMA_CARD",
            "detail": "The final allowed QA round still fails.",
        }
        completed = self.submit(
            envelope["pending_work_details"][0], self.gate_result(20, [final_finding]),
            "qa-cap-20.json",
        )
        self.assertEqual("FORCED_WITH_ERRORS", completed["delivery_status"])
        self.assertEqual([], completed["pending_work_items"])
        release = self.output / "releases" / completed["snapshot_id"]
        qa = json.loads((release / "artifacts" / "qa_report.json").read_text())
        self.assertEqual(20, qa["round"])
        self.assertIn("ROUND_LIMIT", qa["reason_codes"])

    def test_post_build_tampering_publishes_failed_attempt_without_replacing_delivery(self) -> None:
        envelope, _ = self.drive_to_qa()
        passed = self.submit(envelope["pending_work_details"][0], self.gate_result(1), "baseline-qa.json")
        baseline_delivery = json.loads((self.output / "latest_delivery.json").read_text())
        self.assertEqual(passed["snapshot_id"], baseline_delivery["snapshot_id"])

        project = json.loads((self.output / "project.json").read_text())
        failed_card = {
            "version": 1,
            "ontology_iri": project["ontology_iri"],
            "entity_namespace": project["entity_namespace"],
            "classes": [
                {
                    "iri": project["entity_namespace"] + "FailedAttemptOnly",
                    "label": "Failed attempt only",
                    "comment": "This term must not enter the published identity registry.",
                    "superclasses": [],
                    "equivalent_classes": [],
                    "disjoint_with": [],
                }
            ],
            "object_properties": [],
            "datatype_properties": [],
        }
        envelope, _ = self.drive_to_qa(failed_card)
        (self.output / ".staging" / envelope["run_id"] / "ontology.owl").write_text(
            "tampered after deterministic validation\n", encoding="utf-8"
        )
        failed = self.submit(
            envelope["pending_work_details"][0], self.gate_result(1), "tampered-qa.json"
        )
        self.assertEqual("FAILED", failed["run_state"])
        self.assertEqual("FAILED", failed["delivery_status"])
        failed_release = self.output / "releases" / failed["snapshot_id"]
        self.assertFalse((failed_release / "ontology.owl").exists())
        delivery = json.loads((failed_release / "delivery_status.json").read_text())
        self.assertEqual(["STATE_LEDGER_INVALID"], delivery["reason_codes"])
        self.assertEqual(baseline_delivery, json.loads((self.output / "latest_delivery.json").read_text()))
        self.assertEqual(failed["snapshot_id"], json.loads((self.output / "latest_attempt.json").read_text())["snapshot_id"])
        assert_release_golden(
            self,
            "failed",
            output=self.output,
            workspace=self.workspace,
            terminal_envelope=failed,
            run_cli=self.run_cli,
        )

        restarted = self.run_cli(
            "run", "start", "--workspace", str(self.workspace), "--output", str(self.output),
            "--source", "empty.md",
        )
        self.assertEqual(0, restarted.returncode, restarted.stderr or restarted.stdout)
        envelope = json.loads(restarted.stdout)
        envelope = self.submit(envelope["pending_work_details"][0], "# CQs\n", "registry-cqs.md")
        envelope = self.submit(envelope["pending_work_details"][0], "# SRD\n", "registry-srd.md")
        registry = envelope["pending_work_details"][0]["input"]["term_identity_registry"]
        baseline_release = self.output / "releases" / baseline_delivery["snapshot_id"]
        self.assertEqual(
            json.loads((baseline_release / "artifacts" / "schema_card.json").read_text()),
            registry,
        )
        self.assertNotIn("FailedAttemptOnly", json.dumps(registry))

    def test_resume_finishes_an_interrupted_terminal_publication(self) -> None:
        envelope, _ = self.drive_to_qa()
        completed = self.submit(
            envelope["pending_work_details"][0], self.gate_result(1), "terminal-pass.json"
        )
        attempt = (self.output / "latest_attempt.json").read_bytes()
        delivery = (self.output / "latest_delivery.json").read_bytes()

        ledger_path = self.output / "ledger.json"
        ledger = json.loads(ledger_path.read_text())
        ledger["active_run"].update(
            {
                "run_state": "ACTIVE",
                "current_stage": "ORCHESTRATION",
                "delivery_status": None,
                "pending_work": ["orchestration"],
                "recent_errors": [],
            }
        )
        ledger["latest_attempt"] = None
        ledger["latest_delivery"] = None
        payload = {key: value for key, value in ledger.items() if key != "_integrity"}
        ledger["_integrity"] = {
            "algorithm": "sha256",
            "digest": hashlib.sha256(
                json.dumps(
                    payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest(),
        }
        ledger_path.write_text(
            json.dumps(ledger, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        (self.output / ".ontology-project.lock").write_text(
            envelope["run_id"] + "\n", encoding="utf-8"
        )
        status = self.run_cli("run", "status", "--output", str(self.output), "--json")
        self.assertEqual(0, status.returncode, status.stderr or status.stdout)
        self.assertEqual("ACTIVE", json.loads(status.stdout)["run_state"])

        (self.workspace / "empty.md").write_text("# Drift\n", encoding="utf-8")
        rejected = self.run_cli("run", "resume", "--output", str(self.output))
        self.assertEqual(3, rejected.returncode)
        self.assertEqual("config_drift", json.loads(rejected.stdout)["error_code"])
        (self.workspace / "empty.md").write_text("", encoding="utf-8")
        resumed = self.run_cli("run", "resume", "--output", str(self.output))
        self.assertEqual(0, resumed.returncode, resumed.stderr or resumed.stdout)
        payload = json.loads(resumed.stdout)
        self.assertEqual(completed["snapshot_id"], payload["snapshot_id"])
        self.assertEqual("PASS", payload["delivery_status"])
        self.assertEqual(attempt, (self.output / "latest_attempt.json").read_bytes())
        self.assertEqual(delivery, (self.output / "latest_delivery.json").read_bytes())

    def test_resume_republishes_after_terminal_commit_crash(self) -> None:
        envelope, _ = self.drive_to_qa()
        work = envelope["pending_work_details"][0]
        result_path = self.root / "pre-marker-pass.json"
        result_path.write_text(
            json.dumps(self.gate_result(1)), encoding="utf-8"
        )
        run_root = self.output / ".staging" / envelope["run_id"]
        original_atomic_text = pipeline._atomic_text

        def crash_before_terminal_commit(path: Path, content: str) -> None:
            if Path(path).name == "terminal_commit.json":
                raise RuntimeError("simulated crash before terminal commit")
            original_atomic_text(path, content)

        with mock.patch.object(
            pipeline, "_atomic_text", side_effect=crash_before_terminal_commit
        ):
            with self.assertRaisesRegex(
                RuntimeError, "simulated crash before terminal commit"
            ):
                pipeline._run_submit(
                    SimpleNamespace(
                        output_dir=self.output,
                        work_item_id=work["work_item_id"],
                        input_digest=work["input_digest"],
                        result=result_path,
                    )
                )

        ledger = json.loads((self.output / "ledger.json").read_text())
        self.assertEqual("ORCHESTRATION", ledger["active_run"]["current_stage"])
        self.assertEqual(["orchestration"], ledger["active_run"]["pending_work"])
        self.assertFalse((run_root / "terminal_commit.json").exists())
        self.assertTrue((run_root / "artifacts" / "qa_report.json").exists())
        self.assertTrue((run_root / "delivery_status.json").exists())

        canonical_candidates = run_root / "artifacts" / "abox_candidates.json"
        legacy_candidates = run_root / "artifacts" / "aggregate_candidates.json"
        canonical_candidates.replace(legacy_candidates)
        crashed_output = self.root / "pre-marker-crash"
        shutil.copytree(self.output, crashed_output)

        (self.workspace / "empty.md").write_text("# Drift\n", encoding="utf-8")
        rejected = self.run_cli("run", "resume", "--output", str(self.output))
        self.assertEqual(3, rejected.returncode)
        self.assertEqual("config_drift", json.loads(rejected.stdout)["error_code"])

        (self.workspace / "empty.md").write_text("", encoding="utf-8")
        (run_root / "artifacts" / "schema_lock.json").write_text(
            "{}\n", encoding="utf-8"
        )
        rejected = self.run_cli("run", "resume", "--output", str(self.output))
        self.assertEqual(5, rejected.returncode)
        self.assertEqual("ledger_corrupt", json.loads(rejected.stdout)["error_code"])

        shutil.rmtree(self.output)
        shutil.copytree(crashed_output, self.output)
        resumed = self.run_cli("run", "resume", "--output", str(self.output))

        self.assertEqual(0, resumed.returncode, resumed.stderr or resumed.stdout)
        result = json.loads(resumed.stdout)
        self.assertEqual("PASS", result["delivery_status"])
        release = self.output / "releases" / result["snapshot_id"]
        self.assertTrue((release / "artifacts" / "abox_candidates.json").is_file())
        self.assertFalse(
            (release / "artifacts" / "aggregate_candidates.json").exists()
        )

    def test_pending_legacy_qa_work_migrates_candidate_artifact_contract(self) -> None:
        envelope, _ = self.drive_to_qa()
        work = envelope["pending_work_details"][0]
        run_root = self.output / ".staging" / envelope["run_id"]
        artifacts = run_root / "artifacts"
        (artifacts / "abox_candidates.json").replace(
            artifacts / "aggregate_candidates.json"
        )

        old_work_root = run_root / "work_items" / work["work_item_id"]
        payload = json.loads((old_work_root / "input.json").read_text())
        payload["artifact_sha256"]["artifacts/aggregate_candidates.json"] = (
            payload["artifact_sha256"].pop("artifacts/abox_candidates.json")
        )
        payload["review_bundle"]["aggregate_candidates"] = payload[
            "review_bundle"
        ].pop("abox_candidates")
        input_digest = pipeline._digest_text(pipeline._canonical_json(payload))
        work_item_id = pipeline._work_item_id(
            "QA_GATE_1", work["logical_sequence"], input_digest
        )
        state = json.loads((old_work_root / "state.json").read_text())
        state["work_item_id"] = work_item_id
        state["input_digest"] = input_digest
        new_work_root = run_root / "work_items" / work_item_id
        old_work_root.rename(new_work_root)
        (new_work_root / "input.json").write_text(
            pipeline._canonical_json(payload) + "\n", encoding="utf-8"
        )
        (new_work_root / "state.json").write_text(
            pipeline._canonical_json(state) + "\n", encoding="utf-8"
        )

        ledger_path = self.output / "ledger.json"
        ledger = json.loads(ledger_path.read_text())
        ledger["active_run"]["pending_work"] = [work_item_id]
        unsigned = {key: value for key, value in ledger.items() if key != "_integrity"}
        ledger["_integrity"] = {
            "algorithm": "sha256",
            "digest": hashlib.sha256(
                json.dumps(
                    unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest(),
        }
        ledger_path.write_text(
            json.dumps(
                ledger, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            + "\n",
            encoding="utf-8",
        )
        migrated_work = {
            **work,
            "work_item_id": work_item_id,
            "input_digest": input_digest,
            "input": payload,
        }

        completed = self.submit(
            migrated_work, self.gate_result(1), "legacy-qa-pass.json"
        )

        self.assertEqual("PASS", completed["delivery_status"])
        release = self.output / "releases" / completed["snapshot_id"]
        self.assertTrue((release / "artifacts" / "abox_candidates.json").is_file())
        self.assertFalse(
            (release / "artifacts" / "aggregate_candidates.json").exists()
        )


if __name__ == "__main__":
    unittest.main()
