from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
CLI = SKILL_DIR / "scripts" / "ontology_pipeline.py"


class RunLifecycleCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.output = self.root / "output"
        self.workspace.mkdir()
        (self.workspace / "source.md").write_text("# Source\nAlice owns order O-1.\n", encoding="utf-8")

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
            "run",
            "start",
            "--workspace",
            str(self.workspace),
            "--output",
            str(self.output),
            "--source",
            "source.md",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stderr)
        return json.loads(result.stdout)

    def submit(self, work: dict, value: dict | str, name: str) -> dict:
        path = self.root / name
        if isinstance(value, str):
            path.write_text(value, encoding="utf-8")
        else:
            path.write_text(json.dumps(value), encoding="utf-8")
        result = self.run_cli(
            "run",
            "submit",
            "--output",
            str(self.output),
            "--work-item-id",
            work["work_item_id"],
            "--input-digest",
            work["input_digest"],
            "--result",
            str(path),
        )
        self.assertEqual(0, result.returncode, result.stderr or result.stdout)
        return json.loads(result.stdout)

    @staticmethod
    def resign(path: Path, value: dict) -> None:
        payload = {key: item for key, item in value.items() if key != "_integrity"}
        value["_integrity"] = {
            "algorithm": "sha256",
            "digest": hashlib.sha256(
                json.dumps(
                    payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest(),
        }
        path.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

    def strip_term_identity_registry(self, run_root: Path) -> None:
        registry_path = run_root / "inputs" / "term_identity_registry.json"
        if registry_path.exists():
            registry_path.unlink()
        manifest_path = run_root / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["artifacts"] = [
            artifact
            for artifact in manifest["artifacts"]
            if artifact["path"] != "inputs/term_identity_registry.json"
        ]
        self.resign(manifest_path, manifest)

    def publish_pass_delivery(self) -> dict:
        source_path = self.workspace / "source.md"
        original = source_path.read_text(encoding="utf-8")
        source_path.write_text("", encoding="utf-8")
        try:
            envelope = self.start()
            envelope = self.submit(
                envelope["pending_work_details"][0],
                "# CQs\n- No business facts.\n",
                "baseline-cqs.md",
            )
            envelope = self.submit(
                envelope["pending_work_details"][0],
                "# SRD\nNo business facts.\n",
                "baseline-srd.md",
            )
            project = json.loads((self.output / "project.json").read_text())
            card = {
                "version": 1,
                "ontology_iri": project["ontology_iri"],
                "entity_namespace": project["entity_namespace"],
                "classes": [],
                "object_properties": [],
                "datatype_properties": [],
            }
            envelope = self.submit(
                envelope["pending_work_details"][0], card, "baseline-schema.json"
            )
            return self.submit(
                envelope["pending_work_details"][0],
                {"version": 1, "round": 1, "status": "PASS", "findings": []},
                "baseline-gate1.json",
            )
        finally:
            source_path.write_text(original, encoding="utf-8")

    def test_start_creates_immutable_project_and_active_run(self) -> None:
        envelope = self.start()

        self.assertTrue(envelope["accepted"])
        self.assertEqual("run start", envelope["command"])
        run_id = envelope["run_id"]
        project = json.loads((self.output / "project.json").read_text())
        ledger = json.loads((self.output / "ledger.json").read_text())
        self.assertTrue(project["ontology_iri"].startswith("https://example.org/ontology/"))
        self.assertEqual(project["ontology_iri"] + "#", project["entity_namespace"])
        self.assertEqual(run_id, ledger["active_run"]["run_id"])
        self.assertEqual("ACTIVE", ledger["active_run"]["run_state"])
        self.assertEqual(12, len(project["location_summary"]["path_digest"]))

    def test_project_iri_uses_output_basename_slug_and_location_digest(self) -> None:
        self.output = self.root / "Sales Orders"

        envelope = self.start()

        digest = envelope["ontology_iri"].rsplit("-", 1)[-1]
        self.assertEqual(f"https://example.org/ontology/sales-orders-{digest}", envelope["ontology_iri"])
        self.assertEqual(12, len(digest))

    def test_start_migrates_existing_schema_card_project_identity(self) -> None:
        artifacts = self.output / "artifacts"
        artifacts.mkdir(parents=True)
        ontology_iri = "https://example.org/ontology/established"
        legacy_card = {
            "version": 1,
            "ontology_iri": ontology_iri,
            "entity_namespace": ontology_iri + "#",
            "classes": [
                {
                    "iri": ontology_iri + "#Customer",
                    "label": "Customer",
                    "comment": "An established customer.",
                    "superclasses": [],
                    "equivalent_classes": [],
                    "disjoint_with": [],
                }
            ],
            "object_properties": [],
            "datatype_properties": [
                {
                    "iri": ontology_iri + "#customerId",
                    "label": "customer ID",
                    "comment": "An established customer identifier.",
                    "domain": ontology_iri + "#Customer",
                    "range": "http://www.w3.org/2001/XMLSchema#string",
                    "subproperty_of": [],
                    "equivalent_properties": [],
                    "max_count": 1,
                    "identity": True,
                }
            ],
        }
        (artifacts / "schema_card.json").write_text(
            json.dumps(legacy_card), encoding="utf-8"
        )

        envelope = self.start()

        self.assertEqual(ontology_iri, envelope["ontology_iri"])
        self.assertEqual(ontology_iri, json.loads((self.output / "project.json").read_text())["ontology_iri"])
        changed_card = json.loads(json.dumps(legacy_card))
        changed_card["classes"][0]["iri"] = ontology_iri + "#ChangedAfterStart"
        changed_card["classes"][0]["label"] = "Changed after start"
        changed_card["datatype_properties"] = []
        (artifacts / "schema_card.json").write_text(
            json.dumps(changed_card), encoding="utf-8"
        )
        for name, content in (
            ("legacy-cqs.md", "# CQs\n- Which customers are established?\n"),
            ("legacy-srd.md", "# SRD\nEstablished customers retain their identifiers.\n"),
        ):
            result_path = self.root / name
            result_path.write_text(content, encoding="utf-8")
            work = envelope["pending_work_details"][0]
            submitted = self.run_cli(
                "run",
                "submit",
                "--output",
                str(self.output),
                "--work-item-id",
                work["work_item_id"],
                "--input-digest",
                work["input_digest"],
                "--result",
                str(result_path),
            )
            self.assertEqual(0, submitted.returncode, submitted.stderr or submitted.stdout)
            envelope = json.loads(submitted.stdout)
        self.assertEqual(
            legacy_card,
            envelope["pending_work_details"][0]["input"]["term_identity_registry"],
        )

    def test_submit_backfills_legacy_term_registry_before_validation(self) -> None:
        baseline = self.publish_pass_delivery()
        envelope = self.start()
        run_root = self.output / ".staging" / envelope["run_id"]
        self.strip_term_identity_registry(run_root)
        work = envelope["pending_work_details"][0]
        result_path = self.root / "legacy-cqs.md"
        result_path.write_text("# CQs\n- What changed?\n", encoding="utf-8")

        submitted = self.run_cli(
            "run",
            "submit",
            "--output",
            str(self.output),
            "--work-item-id",
            work["work_item_id"],
            "--input-digest",
            work["input_digest"],
            "--result",
            str(result_path),
        )

        self.assertEqual(0, submitted.returncode, submitted.stderr or submitted.stdout)
        self.assertTrue((run_root / "inputs" / "term_identity_registry.json").exists())
        registry = json.loads((run_root / "inputs" / "term_identity_registry.json").read_text())
        baseline_release = self.output / "releases" / baseline["snapshot_id"]
        self.assertEqual(
            json.loads((baseline_release / "artifacts" / "schema_card.json").read_text()),
            registry,
        )

    def test_status_leaves_legacy_term_registry_unwritten_and_resume_backfills_it(self) -> None:
        baseline = self.publish_pass_delivery()
        envelope = self.start()
        run_root = self.output / ".staging" / envelope["run_id"]
        self.strip_term_identity_registry(run_root)

        status = self.run_cli("run", "status", "--output", str(self.output), "--json")
        self.assertEqual(0, status.returncode, status.stderr)
        self.assertFalse((run_root / "inputs" / "term_identity_registry.json").exists())

        resumed = self.run_cli(
            "run",
            "resume",
            "--workspace",
            str(self.workspace),
            "--output",
            str(self.output),
            "--source",
            "source.md",
        )
        self.assertEqual(0, resumed.returncode, resumed.stderr or resumed.stdout)
        self.assertTrue((run_root / "inputs" / "term_identity_registry.json").exists())
        registry = json.loads((run_root / "inputs" / "term_identity_registry.json").read_text())
        baseline_release = self.output / "releases" / baseline["snapshot_id"]
        self.assertEqual(
            json.loads((baseline_release / "artifacts" / "schema_card.json").read_text()),
            registry,
        )

    def test_second_start_is_a_closed_lock_conflict(self) -> None:
        self.start()
        ledger_before = (self.output / "ledger.json").read_bytes()
        staging_before = sorted(path.relative_to(self.output) for path in (self.output / ".staging").rglob("*"))
        result = self.run_cli(
            "run",
            "start",
            "--workspace",
            str(self.workspace),
            "--output",
            str(self.output),
            "--source",
            "source.md",
        )

        self.assertEqual(3, result.returncode)
        self.assertEqual("lock_conflict", json.loads(result.stdout)["error"]["code"])
        self.assertEqual("", result.stderr)
        self.assertEqual(ledger_before, (self.output / "ledger.json").read_bytes())
        self.assertEqual(staging_before, sorted(path.relative_to(self.output) for path in (self.output / ".staging").rglob("*")))

    def test_concurrent_first_start_has_one_winner(self) -> None:
        def concurrent_start(_: int) -> subprocess.CompletedProcess[str]:
            return self.run_cli(
                "run",
                "start",
                "--workspace",
                str(self.workspace),
                "--output",
                str(self.output),
                "--source",
                "source.md",
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(concurrent_start, range(2)))

        accepted = [json.loads(result.stdout) for result in results if result.returncode == 0]
        rejected = [result for result in results if result.returncode == 3]
        self.assertEqual(1, len(accepted))
        self.assertEqual(1, len(rejected))
        project = json.loads((self.output / "project.json").read_text())
        self.assertEqual(accepted[0]["ontology_iri"], project["ontology_iri"])

    def test_invalid_source_returns_json_and_does_not_create_project_identity(self) -> None:
        result = self.run_cli(
            "run",
            "start",
            "--workspace",
            str(self.workspace),
            "--output",
            str(self.output),
            "--source",
            "missing.md",
        )

        self.assertEqual(2, result.returncode)
        self.assertFalse(json.loads(result.stdout)["accepted"])
        self.assertEqual("invalid_input", json.loads(result.stdout)["error_code"])
        self.assertEqual("", result.stderr)
        self.assertFalse((self.output / "project.json").exists())

    def test_absolute_source_path_is_rejected_by_run_contract(self) -> None:
        result = self.run_cli(
            "run",
            "start",
            "--workspace",
            str(self.workspace),
            "--output",
            str(self.output),
            "--source",
            str(self.workspace / "source.md"),
        )

        self.assertEqual(2, result.returncode)
        self.assertEqual("invalid_input", json.loads(result.stdout)["error_code"])

    def test_invalid_utf8_source_is_rejected_before_identity_creation(self) -> None:
        (self.workspace / "source.md").write_bytes(b"\xff\xfe")

        result = self.run_cli(
            "run",
            "start",
            "--workspace",
            str(self.workspace),
            "--output",
            str(self.output),
            "--source",
            "source.md",
        )

        self.assertEqual(2, result.returncode)
        self.assertEqual("invalid_input", json.loads(result.stdout)["error_code"])
        self.assertFalse((self.output / "project.json").exists())

    def test_missing_required_argument_still_returns_json_envelope(self) -> None:
        result = self.run_cli("run", "start", "--workspace", str(self.workspace), "--output", str(self.output))

        self.assertEqual(2, result.returncode)
        self.assertFalse(json.loads(result.stdout)["accepted"])
        self.assertEqual("invalid_input", json.loads(result.stdout)["error_code"])

    def test_persistence_failure_returns_json_and_exit_five(self) -> None:
        self.output.mkdir()
        (self.output / ".staging").write_text("blocks staging directory", encoding="utf-8")

        result = self.run_cli(
            "run",
            "start",
            "--workspace",
            str(self.workspace),
            "--output",
            str(self.output),
            "--source",
            "source.md",
        )

        self.assertEqual(5, result.returncode)
        self.assertFalse(json.loads(result.stdout)["accepted"])
        self.assertEqual("persistence_failed", json.loads(result.stdout)["error_code"])

    def test_status_json_reads_only_persisted_state(self) -> None:
        started = self.start()
        result = self.run_cli("run", "status", "--output", str(self.output), "--json")

        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual("ok", payload["status"])
        self.assertEqual(started["run_id"], payload["data"]["run_id"])
        self.assertEqual("ACTIVE", payload["data"]["run_state"])
        self.assertIn("current_stage", payload["data"])
        self.assertIn("pending_work", payload["data"])
        self.assertIn("recent_errors", payload["data"])

    def test_status_does_not_create_a_missing_output_directory(self) -> None:
        result = self.run_cli("run", "status", "--output", str(self.output), "--json")

        self.assertEqual(2, result.returncode)
        self.assertFalse(self.output.exists())

    def test_resume_rejects_tampered_ledger_without_writing_terminal_state(self) -> None:
        self.start()
        ledger_path = self.output / "ledger.json"
        ledger = json.loads(ledger_path.read_text())
        ledger["active_run"]["current_stage"] = "tampered"
        ledger_path.write_text(json.dumps(ledger), encoding="utf-8")

        result = self.run_cli("run", "resume", "--output", str(self.output))

        self.assertEqual(5, result.returncode)
        self.assertEqual("ledger_corrupt", json.loads(result.stdout)["error"]["code"])
        self.assertTrue(json.loads(ledger_path.read_text())["active_run"]["run_state"] == "ACTIVE")

    def test_resume_rejects_resigned_ledger_with_unknown_fields(self) -> None:
        self.start()
        ledger_path = self.output / "ledger.json"
        ledger = json.loads(ledger_path.read_text())
        ledger["unexpected"] = True
        payload = {key: value for key, value in ledger.items() if key != "_integrity"}
        ledger["_integrity"] = {
            "algorithm": "sha256",
            "digest": hashlib.sha256(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        }
        content = json.dumps(ledger, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        ledger_path.write_text(content, encoding="utf-8")

        result = self.run_cli("run", "resume", "--output", str(self.output))

        self.assertEqual(5, result.returncode)
        self.assertEqual("ledger_corrupt", json.loads(result.stdout)["error_code"])

    def test_resume_rejects_resigned_ledger_with_invalid_snapshot_id(self) -> None:
        self.start()
        ledger_path = self.output / "ledger.json"
        ledger = json.loads(ledger_path.read_text())
        ledger["latest_attempt"] = "garbage"
        payload = {key: value for key, value in ledger.items() if key != "_integrity"}
        ledger["_integrity"] = {
            "algorithm": "sha256",
            "digest": hashlib.sha256(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        }
        ledger_path.write_text(
            json.dumps(ledger, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

        result = self.run_cli("run", "resume", "--output", str(self.output))

        self.assertEqual(5, result.returncode)
        self.assertEqual("ledger_corrupt", json.loads(result.stdout)["error_code"])

    def test_resume_rejects_latest_delivery_without_matching_pointer(self) -> None:
        self.start()
        ledger_path = self.output / "ledger.json"
        ledger = json.loads(ledger_path.read_text())
        ledger["latest_delivery"] = "a" * 64
        payload = {key: value for key, value in ledger.items() if key != "_integrity"}
        ledger["_integrity"] = {
            "algorithm": "sha256",
            "digest": hashlib.sha256(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        }
        ledger_path.write_text(
            json.dumps(ledger, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

        result = self.run_cli("run", "resume", "--output", str(self.output))

        self.assertEqual(5, result.returncode)
        self.assertEqual("ledger_corrupt", json.loads(result.stdout)["error_code"])

    def test_resume_accepts_unchanged_trusted_staging(self) -> None:
        started = self.start()

        result = self.run_cli("run", "resume", "--output", str(self.output))

        self.assertEqual(0, result.returncode, result.stderr)
        envelope = json.loads(result.stdout)
        self.assertTrue(envelope["accepted"])
        self.assertEqual(started["run_id"], envelope["run_id"])
        self.assertEqual("ACTIVE", envelope["run_state"])

    def test_resume_uses_normalized_source_digest(self) -> None:
        (self.workspace / "source.md").write_bytes(b"\xef\xbb\xbf# Source\r\nAlice.\r\n")
        self.start()
        (self.workspace / "source.md").write_text("# Source\nAlice.\n", encoding="utf-8")

        result = self.run_cli("run", "resume", "--output", str(self.output))

        self.assertEqual(0, result.returncode, result.stderr)

    def test_resume_rejects_source_config_drift(self) -> None:
        self.start()
        (self.workspace / "source.md").write_text("# Source\nChanged evidence.\n", encoding="utf-8")

        result = self.run_cli("run", "resume", "--output", str(self.output))

        self.assertEqual(3, result.returncode)
        self.assertEqual("config_drift", json.loads(result.stdout)["error"]["code"])

    def test_resume_rejects_source_set_drift(self) -> None:
        self.start()
        (self.workspace / "second.md").write_text("# Second\nBob.\n", encoding="utf-8")

        result = self.run_cli(
            "run", "resume", "--output", str(self.output),
            "--source", "source.md", "--source", "second.md",
        )

        self.assertEqual(3, result.returncode)
        self.assertEqual("config_drift", json.loads(result.stdout)["error_code"])

    def test_resume_rejects_project_identity_drift(self) -> None:
        self.start()
        project_path = self.output / "project.json"
        project = json.loads(project_path.read_text())
        project["ontology_iri"] = "https://example.org/ontology/tampered"
        project["entity_namespace"] = project["ontology_iri"] + "#"
        self.resign(project_path, project)

        result = self.run_cli("run", "resume", "--output", str(self.output))

        self.assertEqual(5, result.returncode)
        self.assertEqual("ledger_corrupt", json.loads(result.stdout)["error_code"])

    def test_resume_rejects_contract_version_drift(self) -> None:
        self.start()
        project_path = self.output / "project.json"
        project = json.loads(project_path.read_text())
        project["version"] = 2
        self.resign(project_path, project)

        result = self.run_cli("run", "resume", "--output", str(self.output))

        self.assertEqual(3, result.returncode)
        self.assertEqual("contract_mismatch", json.loads(result.stdout)["error_code"])

    def test_abort_publishes_failed_snapshot_and_keeps_latest_delivery(self) -> None:
        started = self.start()
        latest_delivery = self.output / "latest_delivery.json"
        latest_delivery.write_text('{"version":1,"snapshot_id":"prior-delivery"}\n', encoding="utf-8")
        latest_delivery_before = latest_delivery.read_bytes()
        result = self.run_cli(
            "run",
            "abort",
            "--output",
            str(self.output),
        )

        self.assertEqual(0, result.returncode, result.stderr)
        envelope = json.loads(result.stdout)
        self.assertTrue(envelope["accepted"])
        snapshot = json.loads(
            (self.output / "releases" / envelope["snapshot_id"] / "delivery_status.json").read_text()
        )
        self.assertEqual("FAILED", snapshot["delivery_status"])
        self.assertEqual("ORCHESTRATION_ABORTED", snapshot["failure_code"])
        run_state = json.loads(
            (self.output / "releases" / envelope["snapshot_id"] / "artifacts" / "run_state.json").read_text()
        )
        self.assertEqual(started["run_id"], run_state["run_id"])
        pointers = self.output
        pointer = json.loads((pointers / "latest_attempt.json").read_text())
        self.assertEqual(envelope["snapshot_id"], pointer["snapshot_id"])
        self.assertEqual(latest_delivery_before, (pointers / "latest_delivery.json").read_bytes())

        status = self.run_cli("run", "status", "--output", str(self.output), "--json")
        data = json.loads(status.stdout)["data"]
        self.assertEqual("FAILED", data["run_state"])
        self.assertEqual("FAILED", data["delivery_status"])
        self.assertEqual("RELEASE_SNAPSHOT", data["current_stage"])

        restarted = self.run_cli(
            "run",
            "start",
            "--workspace",
            str(self.workspace),
            "--output",
            str(self.output),
            "--source",
            "source.md",
        )
        self.assertEqual(0, restarted.returncode, restarted.stderr)
        self.assertEqual(envelope["snapshot_id"], json.loads((self.output / "ledger.json").read_text())["latest_attempt"])

    def test_resume_finishes_an_interrupted_abort_commit(self) -> None:
        started = self.start()
        aborted = self.run_cli("run", "abort", "--output", str(self.output))
        self.assertEqual(0, aborted.returncode, aborted.stderr)
        (self.output / ".ontology-project.lock").write_text(started["run_id"] + "\n", encoding="utf-8")

        result = self.run_cli("run", "resume", "--output", str(self.output))

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("FAILED", json.loads(result.stdout)["run_state"])
        self.assertFalse((self.output / ".ontology-project.lock").exists())

    def test_resume_rejects_snapshot_resigned_under_an_old_content_address(self) -> None:
        started = self.start()
        aborted = self.run_cli("run", "abort", "--output", str(self.output))
        snapshot_id = json.loads(aborted.stdout)["snapshot_id"]
        release = self.output / "releases" / snapshot_id
        run_state_path = release / "artifacts" / "run_state.json"
        run_state = json.loads(run_state_path.read_text())
        run_state["failure_code"] = "TAMPERED"
        run_state_content = json.dumps(run_state, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        run_state_path.write_text(run_state_content, encoding="utf-8")
        manifest_path = release / "release_manifest.json"
        manifest = json.loads(manifest_path.read_text())
        for artifact in manifest["artifacts"]:
            if artifact["path"] == "artifacts/run_state.json":
                artifact["sha256"] = hashlib.sha256(run_state_content.encode("utf-8")).hexdigest()
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        (self.output / ".ontology-project.lock").write_text(started["run_id"] + "\n", encoding="utf-8")

        result = self.run_cli("run", "resume", "--output", str(self.output))

        self.assertEqual(5, result.returncode)
        self.assertEqual("ledger_corrupt", json.loads(result.stdout)["error_code"])

    def test_malformed_snapshot_manifest_still_returns_json_error(self) -> None:
        started = self.start()
        aborted = self.run_cli("run", "abort", "--output", str(self.output))
        snapshot_id = json.loads(aborted.stdout)["snapshot_id"]
        manifest_path = self.output / "releases" / snapshot_id / "release_manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["artifacts"][0]["path"] = 7
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        (self.output / ".ontology-project.lock").write_text(started["run_id"] + "\n", encoding="utf-8")

        result = self.run_cli("run", "resume", "--output", str(self.output))

        self.assertEqual(5, result.returncode)
        self.assertEqual("ledger_corrupt", json.loads(result.stdout)["error_code"])

    def test_non_object_snapshot_manifest_still_returns_json_error(self) -> None:
        started = self.start()
        aborted = self.run_cli("run", "abort", "--output", str(self.output))
        snapshot_id = json.loads(aborted.stdout)["snapshot_id"]
        manifest_path = self.output / "releases" / snapshot_id / "release_manifest.json"
        manifest_path.write_text("[]\n", encoding="utf-8")
        (self.output / ".ontology-project.lock").write_text(started["run_id"] + "\n", encoding="utf-8")

        result = self.run_cli("run", "resume", "--output", str(self.output))

        self.assertEqual(5, result.returncode)
        self.assertEqual("ledger_corrupt", json.loads(result.stdout)["error_code"])

    def test_invalid_utf8_snapshot_manifest_still_returns_json_error(self) -> None:
        started = self.start()
        aborted = self.run_cli("run", "abort", "--output", str(self.output))
        snapshot_id = json.loads(aborted.stdout)["snapshot_id"]
        manifest_path = self.output / "releases" / snapshot_id / "release_manifest.json"
        manifest_path.write_bytes(b"\xff\xfe")
        (self.output / ".ontology-project.lock").write_text(started["run_id"] + "\n", encoding="utf-8")

        result = self.run_cli("run", "resume", "--output", str(self.output))

        self.assertEqual(5, result.returncode)
        self.assertEqual("ledger_corrupt", json.loads(result.stdout)["error_code"])

    def test_status_tolerates_pointer_leading_ledger_during_abort_commit(self) -> None:
        self.start()
        first_abort = self.run_cli("run", "abort", "--output", str(self.output))
        self.assertEqual(0, first_abort.returncode, first_abort.stderr)
        second = self.start()
        active_ledger = (self.output / "ledger.json").read_bytes()
        second_abort = self.run_cli("run", "abort", "--output", str(self.output))
        self.assertEqual(0, second_abort.returncode, second_abort.stderr)
        (self.output / "ledger.json").write_bytes(active_ledger)
        (self.output / ".ontology-project.lock").write_text(second["run_id"] + "\n", encoding="utf-8")

        result = self.run_cli("run", "status", "--output", str(self.output), "--json")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("ACTIVE", json.loads(result.stdout)["run_state"])

    def test_status_rejects_malformed_pointer_with_json_error(self) -> None:
        self.start()
        aborted = self.run_cli("run", "abort", "--output", str(self.output))
        self.assertEqual(0, aborted.returncode, aborted.stderr)
        pointer_path = self.output / "latest_attempt.json"
        pointer = json.loads(pointer_path.read_text())
        pointer["delivery_status"] = []
        pointer_path.write_text(json.dumps(pointer), encoding="utf-8")

        result = self.run_cli("run", "status", "--output", str(self.output), "--json")

        self.assertEqual(5, result.returncode)
        self.assertEqual("ledger_corrupt", json.loads(result.stdout)["error_code"])

    def test_status_validates_latest_attempt_after_abort_commit_completed(self) -> None:
        self.start()
        aborted = self.run_cli("run", "abort", "--output", str(self.output))
        self.assertEqual(0, aborted.returncode, aborted.stderr)
        (self.output / "latest_attempt.json").write_text("{}\n", encoding="utf-8")

        result = self.run_cli("run", "status", "--output", str(self.output), "--json")

        self.assertEqual(5, result.returncode)
        self.assertEqual("ledger_corrupt", json.loads(result.stdout)["error_code"])


if __name__ == "__main__":
    unittest.main()
