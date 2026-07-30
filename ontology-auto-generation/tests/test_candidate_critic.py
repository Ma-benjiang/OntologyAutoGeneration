from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from rdflib import Graph, RDF, OWL, URIRef

try:
    from release_goldens import assert_release_golden, seed_golden_identity
except ImportError:
    from .release_goldens import assert_release_golden, seed_golden_identity


SKILL_DIR = Path(__file__).resolve().parents[1]
CLI = SKILL_DIR / "scripts" / "ontology_pipeline.py"


class CandidateCriticCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.output = self.root / "output"
        self.auto_qa = True
        self.workspace.mkdir()
        (self.workspace / "source.md").write_text(
            "# Orders\nCustomer Alice placed order O-1.\n", encoding="utf-8"
        )
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

    def submit_result(self, work: dict, result: Path) -> subprocess.CompletedProcess[str]:
        return self.run_cli(
            "run", "submit", "--output", str(self.output), "--work-item-id", work["work_item_id"],
            "--input-digest", work["input_digest"], "--result", str(result),
        )

    def submit(self, work: dict, result: Path) -> dict:
        completed = self.submit_result(work, result)
        self.assertEqual(0, completed.returncode, completed.stderr or completed.stdout)
        envelope = json.loads(completed.stdout)
        pending = envelope.get("pending_work_details", [])
        if self.auto_qa and len(pending) == 1 and pending[0]["stage"] == "QA_GATE_1":
            qa = {
                "version": 1, "round": pending[0]["input"]["round"], "status": "PASS", "findings": [],
            }
            return self.submit(
                pending[0], self.write_json(f"qa-{pending[0]['work_item_id']}.json", qa)
            )
        return envelope

    def schema_card(self) -> dict:
        project = json.loads((self.output / "project.json").read_text())
        namespace = project["entity_namespace"]
        return {
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
                }
            ],
            "datatype_properties": [],
        }

    @staticmethod
    def evidence() -> dict:
        return {
            "source": "source.md", "heading_path": ["Orders"], "line_start": 2, "line_end": 2,
            "quote": "Customer Alice placed order O-1.",
        }

    def drive_to_critic(self) -> tuple[dict, dict, dict, dict]:
        started = self.run_cli(
            "run", "start", "--workspace", str(self.workspace), "--output", str(self.output),
            "--source", "source.md",
        )
        self.assertEqual(0, started.returncode, started.stderr)
        envelope = json.loads(started.stdout)
        cq = self.root / "cqs.md"
        cq.write_text("# CQs\n- Which customer placed O-1?\n", encoding="utf-8")
        envelope = self.submit(envelope["pending_work_details"][0], cq)
        srd = self.root / "srd.md"
        srd.write_text("# SRD\nCustomers place orders.\n", encoding="utf-8")
        envelope = self.submit(envelope["pending_work_details"][0], srd)
        card = self.schema_card()
        envelope = self.submit(
            envelope["pending_work_details"][0], self.write_json("schema.json", card)
        )

        entity_work = envelope["pending_work_details"][0]
        prefix = entity_work["input"]["candidate_id_prefix"]
        entity_result = {
            "version": 1, "chunk_id": entity_work["input"]["chunk"]["chunk_id"], "status": "complete",
            "entities": [
                {
                    "candidate_id": prefix + ".entity.001", "class_iri": card["classes"][0]["iri"],
                    "name": "Alice", "business_identifier": None, "evidence": self.evidence(),
                },
                {
                    "candidate_id": prefix + ".entity.002", "class_iri": card["classes"][1]["iri"],
                    "name": "O-1", "business_identifier": None, "evidence": self.evidence(),
                },
            ],
            "ambiguities": [], "failure": None,
        }
        envelope = self.submit(entity_work, self.write_json("entities.json", entity_result))
        assertion_work = envelope["pending_work_details"][0]
        assertion_result = {
            "version": 1, "chunk_id": assertion_work["input"]["chunk"]["chunk_id"], "status": "complete",
            "assertions": [
                {
                    "candidate_id": prefix + ".assertion.001", "kind": "object",
                    "subject_candidate_id": prefix + ".entity.001",
                    "property_iri": card["object_properties"][0]["iri"],
                    "object_candidate_id": prefix + ".entity.002", "evidence": self.evidence(),
                }
            ],
            "exclusions": [], "failure": None,
        }
        envelope = self.submit(assertion_work, self.write_json("assertions.json", assertion_result))
        critic_work = envelope["pending_work_details"][0]
        self.assertEqual("CANDIDATE_CRITIC", critic_work["stage"])
        self.assertEqual(card, critic_work["input"]["schema_card"])
        self.assertEqual(entity_result, critic_work["input"]["entity_result"])
        self.assertEqual(assertion_result, critic_work["input"]["assertion_result"])
        self.assertNotIn("attempt", json.dumps(critic_work["input"]))
        self.assertNotIn("budget", json.dumps(critic_work["input"]))
        self.assertNotIn("admission", json.dumps(critic_work["input"]))
        return critic_work, card, entity_result, assertion_result

    def drive_to_two_entities(self, *, shared_identity: bool = False) -> tuple[list[dict], dict]:
        if shared_identity:
            (self.workspace / "source.md").write_text(
                "# Orders\nCustomer Alice (C-001) placed order O-1.\n", encoding="utf-8"
            )
            second_text = "# Orders\nCustomer Alicia (C-001) placed order O-2.\n"
        else:
            second_text = "# Orders\nCustomer Bob placed order O-2.\n"
        (self.workspace / "second.md").write_text(second_text, encoding="utf-8")
        started = self.run_cli(
            "run", "start", "--workspace", str(self.workspace), "--output", str(self.output),
            "--source", "source.md", "--source", "second.md",
        )
        self.assertEqual(0, started.returncode, started.stderr)
        envelope = json.loads(started.stdout)
        cq = self.root / "two-cqs.md"
        cq.write_text("# CQs\n- Which customers placed which orders?\n", encoding="utf-8")
        envelope = self.submit(envelope["pending_work_details"][0], cq)
        srd = self.root / "two-srd.md"
        srd.write_text("# SRD\nCustomers place orders.\n", encoding="utf-8")
        envelope = self.submit(envelope["pending_work_details"][0], srd)
        card = self.schema_card()
        if shared_identity:
            card["datatype_properties"].append(
                {
                    "iri": card["entity_namespace"] + "customerId", "label": "customer ID",
                    "comment": "Stable customer identifier.", "domain": card["entity_namespace"] + "Customer",
                    "range": "http://www.w3.org/2001/XMLSchema#string", "subproperty_of": [],
                    "equivalent_properties": [], "max_count": 1, "identity": True,
                }
            )
        envelope = self.submit(
            envelope["pending_work_details"][0], self.write_json("two-schema.json", card)
        )
        self.assertEqual(2, len(envelope["pending_work_details"]))
        return envelope["pending_work_details"], card

    def entity_payload(self, work: dict, card: dict, customer: str, order: str) -> dict:
        prefix = work["input"]["candidate_id_prefix"]
        evidence = {
            "source": work["input"]["chunk"]["source_path"],
            "heading_path": ["Orders"], "line_start": 2, "line_end": 2,
            "quote": f"Customer {customer} placed order {order}.",
        }
        return {
            "version": 1, "chunk_id": work["input"]["chunk"]["chunk_id"], "status": "complete",
            "entities": [
                {
                    "candidate_id": prefix + ".entity.001", "class_iri": card["classes"][0]["iri"],
                    "name": customer, "business_identifier": None, "evidence": evidence,
                },
                {
                    "candidate_id": prefix + ".entity.002", "class_iri": card["classes"][1]["iri"],
                    "name": order, "business_identifier": None, "evidence": evidence,
                },
            ],
            "ambiguities": [], "failure": None,
        }

    def assertion_payload(self, work: dict, card: dict) -> dict:
        prefix = work["input"]["candidate_id_prefix"]
        return {
            "version": 1, "chunk_id": work["input"]["chunk"]["chunk_id"], "status": "complete",
            "assertions": [
                {
                    "candidate_id": prefix + ".assertion.001", "kind": "object",
                    "subject_candidate_id": prefix + ".entity.001",
                    "property_iri": card["object_properties"][0]["iri"],
                    "object_candidate_id": prefix + ".entity.002",
                    "evidence": work["input"]["entity_result"]["entities"][0]["evidence"],
                }
            ],
            "exclusions": [], "failure": None,
        }

    @staticmethod
    def review_payload(work: dict, *, assertion_disposition: str = "retain") -> dict:
        prefix = work["input"]["candidate_id_prefix"]
        reviews = [
            {
                "candidate_id": prefix + ".entity.001", "candidate_kind": "entity",
                "disposition": "retain",
            },
            {
                "candidate_id": prefix + ".entity.002", "candidate_kind": "entity",
                "disposition": "retain",
            },
        ]
        if assertion_disposition == "retain":
            reviews.append(
                {
                    "candidate_id": prefix + ".assertion.001", "candidate_kind": "assertion",
                    "disposition": "retain",
                }
            )
        else:
            reviews.append(
                {
                    "candidate_id": prefix + ".assertion.001", "candidate_kind": "assertion",
                    "disposition": "reject", "reason_code": "UNSUPPORTED_BY_PRIMARY",
                    "evidence": CandidateCriticCliTest.evidence(),
                    "detail": "The primary does not semantically support this candidate.",
                }
            )
        return {
            "version": 1, "review_id": work["input"]["review_id"], "status": "complete",
            "reviews": reviews, "batch_reextraction_requests": [],
        }

    @staticmethod
    def retain_all_payload(work: dict) -> dict:
        reviews = [
            {
                "candidate_id": candidate["candidate_id"],
                "candidate_kind": kind,
                "disposition": "retain",
            }
            for kind, key in (("entity", "entities"), ("assertion", "assertions"))
            for candidate in work["input"][f"{kind}_result"][key]
        ]
        return {
            "version": 1,
            "review_id": work["input"]["review_id"],
            "status": "complete",
            "reviews": reviews,
            "batch_reextraction_requests": [],
        }

    def request_payload(self, work: dict, target: str) -> dict:
        payload = self.retain_all_payload(work)
        payload["status"] = "request_reextraction"
        payload["batch_reextraction_requests"] = [
            {
                "target_pass": target,
                "reason_code": "ENTITY_OMITTED" if target == "entity" else "ASSERTION_OMITTED",
                "trigger_candidate_id": None,
                "evidence": self.evidence(),
                "detail": f"The {target} pass must be rerun from the locked input.",
            }
        ]
        return payload

    def test_non_empty_pass_publishes_complete_audited_snapshot_deterministically(self) -> None:
        first_work, _, entity_result, assertion_result = self.drive_to_critic()
        first = self.submit(first_work, self.write_json("critic.json", self.review_payload(first_work)))

        self.assertEqual("COMPLETE", first["run_state"])
        self.assertEqual("PASS", first["delivery_status"])
        first_release = self.output / "releases" / first["snapshot_id"]
        artifact_paths = {
            item["path"] for item in json.loads((first_release / "release_manifest.json").read_text())["artifacts"]
        }
        self.assertTrue(
            {
                "artifacts/abox_candidates.json", "artifacts/critic_reviews.json",
                "artifacts/resolved_instances.json", "artifacts/evidence.jsonl",
                "artifacts/rejections.jsonl", "artifacts/coverage.json", "artifacts/qa_report.json",
                "schema.owl", "instances.owl", "ontology.owl", "delivery_status.json",
            }.issubset(artifact_paths)
        )
        self.assertNotIn("artifacts/aggregate_candidates.json", artifact_paths)
        self.assertTrue(any(path.endswith("/attempts/0001/raw_output.json") for path in artifact_paths))
        aggregate = json.loads((first_release / "artifacts" / "abox_candidates.json").read_text())
        self.assertEqual(entity_result["entities"], aggregate["entities"])
        self.assertEqual(assertion_result["assertions"], aggregate["assertions"])
        critic = json.loads((first_release / "artifacts" / "critic_reviews.json").read_text())
        self.assertEqual(self.review_payload(first_work), critic["reviews"][0]["result"])
        coverage = json.loads((first_release / "artifacts" / "coverage.json").read_text())
        self.assertEqual("COMPLETE", coverage["status"])
        self.assertEqual(coverage["expected_chunk_ids"], coverage["completed_chunk_ids"])
        resolved = json.loads((first_release / "artifacts" / "resolved_instances.json").read_text())
        self.assertEqual(2, len(resolved["individuals"]))
        self.assertEqual(1, len(resolved["assertions"]))
        self.assertGreaterEqual(len((first_release / "artifacts" / "evidence.jsonl").read_text().splitlines()), 3)
        self.assertEqual("", (first_release / "artifacts" / "rejections.jsonl").read_text())

        schema_graph = Graph().parse(first_release / "schema.owl", format="xml")
        instances_graph = Graph().parse(first_release / "instances.owl", format="xml")
        combined_graph = Graph().parse(first_release / "ontology.owl", format="xml")
        declaration = (URIRef(resolved["ontology_iri"]), RDF.type, OWL.Ontology)
        self.assertEqual(set(schema_graph) | set(instances_graph) | {declaration}, set(combined_graph))
        self.assertEqual(2, len(set(combined_graph.subjects(RDF.type, OWL.NamedIndividual))))
        ontology_text = (first_release / "ontology.owl").read_text()
        self.assertNotIn("candidate_id", ontology_text)
        self.assertNotIn("review_id", ontology_text)
        self.assertNotIn("source.md", ontology_text)

        canonical_paths = [
            "artifacts/abox_candidates.json", "artifacts/critic_reviews.json",
            "artifacts/resolved_instances.json", "artifacts/evidence.jsonl", "artifacts/rejections.jsonl",
            "artifacts/coverage.json", "artifacts/qa_report.json",
            "schema.owl", "instances.owl", "ontology.owl",
        ]
        expected_bytes = {path: (first_release / path).read_bytes() for path in canonical_paths}
        expected_graphs = {
            filename: set(Graph().parse(first_release / filename, format="xml"))
            for filename in ("schema.owl", "instances.owl", "ontology.owl")
        }
        second_work, _, _, _ = self.drive_to_critic()
        second = self.submit(second_work, self.write_json("critic-second.json", self.review_payload(second_work)))
        second_release = self.output / "releases" / second["snapshot_id"]
        self.assertEqual(expected_bytes, {path: (second_release / path).read_bytes() for path in canonical_paths})
        self.assertEqual(
            expected_graphs,
            {
                filename: set(Graph().parse(second_release / filename, format="xml"))
                for filename in ("schema.owl", "instances.owl", "ontology.owl")
            },
        )

    def test_concurrent_chunk_submits_preserve_both_accepted_transitions(self) -> None:
        works, card = self.drive_to_two_entities()
        submissions = []
        for work in works:
            source = work["input"]["chunk"]["source_path"]
            customer, order = ("Bob", "O-2") if source == "second.md" else ("Alice", "O-1")
            submissions.append(
                (
                    work,
                    self.write_json(
                        f"concurrent-{source}.json",
                        self.entity_payload(work, card, customer, order),
                    ),
                )
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(lambda item: self.submit_result(item[0], item[1]), submissions)
            )

        self.assertEqual([0, 0], sorted(result.returncode for result in results))
        status = self.run_cli("run", "status", "--output", str(self.output), "--json")
        self.assertEqual(0, status.returncode, status.stderr or status.stdout)
        pending = json.loads(status.stdout)["pending_work_details"]
        self.assertEqual(2, len(pending))
        self.assertEqual({"ASSERTION"}, {work["stage"] for work in pending})

    def test_critic_is_exhaustive_strict_and_rejections_keep_original_evidence(self) -> None:
        work, _, _, _ = self.drive_to_critic()
        valid = self.review_payload(work, assertion_disposition="reject")
        invalid_payloads = []
        missing = json.loads(json.dumps(valid))
        missing["reviews"].pop()
        invalid_payloads.append(missing)
        wrong_id = json.loads(json.dumps(valid))
        wrong_id["review_id"] = "review-v1-" + "0" * 64
        invalid_payloads.append(wrong_id)
        confidence = json.loads(json.dumps(valid))
        confidence["reviews"][0]["confidence"] = 0.9
        invalid_payloads.append(confidence)
        unsorted = json.loads(json.dumps(valid))
        unsorted["reviews"][0], unsorted["reviews"][1] = unsorted["reviews"][1], unsorted["reviews"][0]
        invalid_payloads.append(unsorted)
        outside_primary = json.loads(json.dumps(valid))
        outside_primary["reviews"][2]["evidence"]["line_start"] = 99
        outside_primary["reviews"][2]["evidence"]["line_end"] = 99
        invalid_payloads.append(outside_primary)

        snapshot = self.root / "critic-pending"
        shutil.copytree(self.output, snapshot)
        for ordinal, payload in enumerate(invalid_payloads, start=1):
            with self.subTest(ordinal=ordinal):
                shutil.rmtree(self.output)
                shutil.copytree(snapshot, self.output)
                result = self.submit_result(work, self.write_json(f"invalid-critic-{ordinal}.json", payload))
                self.assertEqual(4, result.returncode)
                self.assertEqual("invalid_submission", json.loads(result.stdout)["error_code"])

        shutil.rmtree(self.output)
        shutil.copytree(snapshot, self.output)
        completed = self.submit(work, self.write_json("critic-reject.json", valid))
        release = self.output / "releases" / completed["snapshot_id"]
        aggregate = json.loads((release / "artifacts" / "abox_candidates.json").read_text())
        self.assertEqual([], aggregate["assertions"])
        rejections = [json.loads(line) for line in (release / "artifacts" / "rejections.jsonl").read_text().splitlines()]
        self.assertEqual(["CRITIC_UNSUPPORTED_BY_PRIMARY"], rejections[0]["reasons"])
        self.assertEqual(self.evidence(), rejections[0]["evidence"])
        self.assertEqual(work["input"]["review_id"], rejections[0]["review"]["review_id"])

    def test_valid_reextraction_requests_are_audited_and_remain_pending(self) -> None:
        work, _, _, _ = self.drive_to_critic()
        prefix = work["input"]["candidate_id_prefix"]
        payload = self.review_payload(work)
        payload["status"] = "request_reextraction"
        payload["reviews"][0] = {
            "candidate_id": prefix + ".entity.001", "candidate_kind": "entity",
            "disposition": "request_reextraction", "reason_code": "ENTITY_NAME_MISMATCH",
            "evidence": self.evidence(), "detail": "The extracted name is misaligned.",
        }
        payload["batch_reextraction_requests"] = [
            {
                "target_pass": "assertion", "reason_code": "ASSERTION_OMITTED",
                "trigger_candidate_id": None, "evidence": self.evidence(),
                "detail": "One explicit assertion is absent.",
            }
        ]

        result = self.submit(work, self.write_json("critic-request.json", payload))

        self.assertEqual("ACTIVE", result["run_state"])
        self.assertEqual("ENTITY", result["pending_work_details"][0]["stage"])
        self.assertEqual("CRITIC_REEXTRACTION", result["pending_work_details"][0]["input"]["invocation_kind"])
        self.assertEqual("request_reextraction", result["work_result"]["status"])
        status = self.run_cli("run", "status", "--output", str(self.output), "--json")
        self.assertEqual(0, status.returncode, status.stderr)
        response = json.loads(status.stdout)
        self.assertEqual(0, response["pending_work_details"][0]["attempt_count"])
        critic_rows = [row for row in response["work_item_results"] if row["stage"] == "CANDIDATE_CRITIC"]
        self.assertEqual(payload, critic_rows[0]["result"])

    def test_execution_retry_recovers_with_same_input_and_deterministic_attempt_ids(self) -> None:
        work, _, _, _ = self.drive_to_critic()
        invalid = self.root / "invalid.json"
        invalid.write_text("{", encoding="utf-8")

        first = self.submit_result(work, invalid)
        self.assertEqual(4, first.returncode)
        completed = self.submit(work, self.write_json("retry-critic.json", self.retain_all_payload(work)))

        self.assertEqual("PASS", completed["delivery_status"])
        release = self.output / "releases" / completed["snapshot_id"]
        attempt_roots = sorted(
            path for path in release.glob("work_items/*/attempts/*/attempt.json")
            if json.loads(path.read_text())["stage"] == "CANDIDATE_CRITIC"
        )
        attempts = [json.loads(path.read_text()) for path in attempt_roots]
        self.assertEqual([1, 2], [attempt["execution_attempt"] for attempt in attempts])
        self.assertEqual(2, len({attempt["attempt_id"] for attempt in attempts}))
        self.assertEqual(1, len({attempt["input_digest"] for attempt in attempts}))

    def test_entity_reextraction_dominates_assertion_and_restarts_the_chain(self) -> None:
        work, _, entity_result, assertion_result = self.drive_to_critic()
        requested = self.request_payload(work, "assertion")
        requested["batch_reextraction_requests"].append(
            {
                "target_pass": "entity", "reason_code": "ENTITY_OMITTED",
                "trigger_candidate_id": None, "evidence": self.evidence(),
                "detail": "Entity correction dominates the assertion request.",
            }
        )
        requested["batch_reextraction_requests"].sort(
            key=lambda request: 0 if request["target_pass"] == "entity" else 1
        )
        envelope = self.submit(work, self.write_json("request-both.json", requested))
        entity_work = envelope["pending_work_details"][0]
        self.assertEqual("ENTITY", entity_work["stage"])
        self.assertEqual(2, entity_work["input"]["invocation_sequence"])

        revised_entities = json.loads(json.dumps(entity_result))
        revised_entities["ambiguities"].append(
            {
                "ambiguity_id": work["input"]["candidate_id_prefix"] + ".ambiguity.001",
                "candidate_id": None,
                "field": "name", "mention": "Alice", "alternatives": ["Alicia"],
                "reason": "The source mention has an unresolved spelling alternative.",
                "evidence": self.evidence(),
            }
        )
        envelope = self.submit(entity_work, self.write_json("revised-entities.json", revised_entities))
        assertion_work = envelope["pending_work_details"][0]
        self.assertEqual("ENTITY_DEPENDENCY_RERUN", assertion_work["input"]["invocation_kind"])
        envelope = self.submit(assertion_work, self.write_json("rerun-assertions.json", assertion_result))
        critic_work = envelope["pending_work_details"][0]
        completed = self.submit(critic_work, self.write_json("rerun-critic.json", self.retain_all_payload(critic_work)))
        self.assertEqual("PASS", completed["delivery_status"])

    def test_assertion_reextraction_keeps_entity_and_no_change_fails_atomically(self) -> None:
        work, _, entity_result, assertion_result = self.drive_to_critic()
        envelope = self.submit(
            work, self.write_json("request-assertion.json", self.request_payload(work, "assertion"))
        )
        assertion_work = envelope["pending_work_details"][0]
        self.assertEqual("ASSERTION", assertion_work["stage"])
        self.assertEqual(entity_result, assertion_work["input"]["entity_result"])

        completed = self.submit(
            assertion_work, self.write_json("unchanged-assertions.json", assertion_result)
        )
        self.assertEqual("FORCED_WITH_ERRORS", completed["delivery_status"])
        release = self.output / "releases" / completed["snapshot_id"]
        failures = [
            json.loads(line)
            for line in (release / "artifacts" / "failed_chunks.jsonl").read_text().splitlines()
        ]
        self.assertEqual(["REEXTRACTION_NO_CHANGE"], [row["reason_code"] for row in failures])
        self.assertEqual(1, failures[0]["counters"]["semantic_reextractions"]["ASSERTION"])
        coverage = json.loads((release / "artifacts" / "coverage.json").read_text())
        self.assertEqual(coverage["expected_chunk_ids"], coverage["failed_chunk_ids"])
        self.assertEqual([], coverage["completed_chunk_ids"])

    def test_repeated_semantic_request_exhausts_budget(self) -> None:
        work, _, entity_result, assertion_result = self.drive_to_critic()
        envelope = self.submit(
            work, self.write_json("request-entity.json", self.request_payload(work, "entity"))
        )
        revised_entities = json.loads(json.dumps(entity_result))
        revised_entities["ambiguities"].append(
            {
                "ambiguity_id": work["input"]["candidate_id_prefix"] + ".ambiguity.001",
                "candidate_id": None,
                "field": "name", "mention": "Alice", "alternatives": ["Alicia"],
                "reason": "The source mention has an unresolved spelling alternative.",
                "evidence": self.evidence(),
            }
        )
        envelope = self.submit(
            envelope["pending_work_details"][0],
            self.write_json("budget-entities.json", revised_entities),
        )
        envelope = self.submit(
            envelope["pending_work_details"][0],
            self.write_json("budget-assertions.json", assertion_result),
        )
        critic_work = envelope["pending_work_details"][0]
        completed = self.submit(
            critic_work, self.write_json("request-entity-again.json", self.request_payload(critic_work, "entity"))
        )
        self.assertEqual("FORCED_WITH_ERRORS", completed["delivery_status"])
        release = self.output / "releases" / completed["snapshot_id"]
        failure = json.loads((release / "artifacts" / "failed_chunks.jsonl").read_text().strip())
        self.assertEqual("REEXTRACTION_BUDGET_EXHAUSTED", failure["reason_code"])

    def test_failed_chunk_does_not_block_or_pollute_another_chunk(self) -> None:
        self.auto_qa = False
        entity_works, card = self.drive_to_two_entities()
        failed_work = entity_works[0]
        invalid = self.root / "invalid-entity.json"
        invalid.write_text("{", encoding="utf-8")
        first = self.submit_result(failed_work, invalid)
        self.assertEqual(4, first.returncode)
        exhausted = self.submit(failed_work, invalid)
        self.assertEqual("ACTIVE", exhausted["run_state"])
        self.assertEqual(failed_work["input"]["chunk"]["chunk_id"], exhausted["failed_chunk_id"])

        surviving_work = exhausted["pending_work_details"][0]
        source = surviving_work["input"]["chunk"]["source_path"]
        customer, order = ("Alice", "O-1") if source == "source.md" else ("Bob", "O-2")
        envelope = self.submit(
            surviving_work,
            self.write_json("surviving-entities.json", self.entity_payload(surviving_work, card, customer, order)),
        )
        assertion_work = envelope["pending_work_details"][0]
        envelope = self.submit(
            assertion_work,
            self.write_json("surviving-assertions.json", self.assertion_payload(assertion_work, card)),
        )
        critic_work = envelope["pending_work_details"][0]
        envelope = self.submit(
            critic_work, self.write_json("surviving-critic.json", self.retain_all_payload(critic_work))
        )
        qa_work = envelope["pending_work_details"][0]
        completed = self.submit(
            qa_work,
            self.write_json(
                "incomplete-coverage-qa.json",
                {
                    "version": 1, "round": 1, "status": "FAIL",
                    "findings": [
                        {
                            "reason_code": "SCHEMA_REVIEW_REQUEST", "target": "SCHEMA_CARD",
                            "detail": "Incomplete Coverage must stop rather than create a repair round.",
                        }
                    ],
                },
            ),
        )

        self.assertEqual("FORCED_WITH_ERRORS", completed["delivery_status"])
        release = self.output / "releases" / completed["snapshot_id"]
        coverage = json.loads((release / "artifacts" / "coverage.json").read_text())
        self.assertEqual(2, len(coverage["expected_chunk_ids"]))
        self.assertEqual(1, len(coverage["failed_chunk_ids"]))
        self.assertEqual(1, len(coverage["completed_chunk_ids"]))
        qa = json.loads((release / "artifacts" / "qa_report.json").read_text())
        self.assertEqual(1, qa["round"])
        self.assertIn("INCOMPLETE_COVERAGE_NO_REPAIR", qa["reason_codes"])
        aggregate = json.loads((release / "artifacts" / "abox_candidates.json").read_text())
        self.assertTrue(all(candidate["candidate_id"].startswith(critic_work["input"]["candidate_id_prefix"])
                            for candidate in aggregate["entities"] + aggregate["assertions"]))
        assert_release_golden(
            self,
            "forced_with_errors",
            output=self.output,
            workspace=self.workspace,
            terminal_envelope=completed,
            run_cli=self.run_cli,
        )

    def test_cross_chunk_business_identity_is_stable_through_public_full_rebuild(self) -> None:
        def build(reverse: bool, suffix: str) -> tuple[dict, Path]:
            entity_works, card = self.drive_to_two_entities(shared_identity=True)
            for work in reversed(entity_works) if reverse else entity_works:
                source = work["input"]["chunk"]["source_path"]
                name, order = ("Alice", "O-1") if source == "source.md" else ("Alicia", "O-2")
                prefix = work["input"]["candidate_id_prefix"]
                quote = f"Customer {name} (C-001) placed order {order}."
                payload = {
                    "version": 1, "chunk_id": work["input"]["chunk"]["chunk_id"], "status": "complete",
                    "entities": [
                        {
                            "candidate_id": prefix + ".entity.001", "class_iri": card["classes"][0]["iri"],
                            "name": name,
                            "business_identifier": {
                                "property_iri": card["datatype_properties"][0]["iri"], "value": "C-001",
                            },
                            "evidence": {
                                "source": source, "heading_path": ["Orders"], "line_start": 2, "line_end": 2,
                                "quote": quote,
                            },
                        }
                    ],
                    "ambiguities": [], "failure": None,
                }
                self.submit(work, self.write_json(f"entities-{suffix}-{source}.json", payload))

            status = json.loads(self.run_cli("run", "status", "--output", str(self.output), "--json").stdout)
            assertion_works = status["pending_work_details"]
            for work in reversed(assertion_works) if reverse else assertion_works:
                payload = {
                    "version": 1, "chunk_id": work["input"]["chunk"]["chunk_id"], "status": "complete",
                    "assertions": [], "exclusions": [], "failure": None,
                }
                self.submit(work, self.write_json(f"assertions-{suffix}-{work['work_item_id']}.json", payload))

            status = json.loads(self.run_cli("run", "status", "--output", str(self.output), "--json").stdout)
            critic_works = status["pending_work_details"]
            completed = None
            for work in reversed(critic_works) if reverse else critic_works:
                completed = self.submit(
                    work, self.write_json(f"critic-{suffix}-{work['work_item_id']}.json", self.retain_all_payload(work))
                )
            assert completed is not None
            release = self.output / "releases" / completed["snapshot_id"]
            return completed, release

        first_completed, first_release = build(reverse=True, suffix="reverse")
        canonical_paths = (
            "artifacts/resolved_instances.json", "artifacts/evidence.jsonl", "artifacts/rejections.jsonl",
            "schema.owl", "instances.owl", "ontology.owl",
        )
        expected = {path: (first_release / path).read_bytes() for path in canonical_paths}
        resolved = json.loads(expected["artifacts/resolved_instances.json"])
        self.assertEqual(1, len(resolved["individuals"]))
        self.assertEqual("Alicia", resolved["individuals"][0]["label"])
        self.assertEqual(["Alice"], [row["name"] for row in resolved["individuals"][0]["observed_aliases"]])
        self.assertRegex(resolved["individuals"][0]["observed_aliases"][0]["alias_id"], r"^alias-v1-[0-9a-f]{64}$")
        self.assertEqual(1, len(resolved["assertions"]))
        self.assertEqual(2, len(resolved["assertions"][0]["candidate_ids"]))
        evidence_rows = [
            json.loads(line) for line in expected["artifacts/evidence.jsonl"].decode().splitlines()
        ]
        identity_rows = [
            row for row in evidence_rows if row["predicate"] == resolved["individuals"][0]["business_identifier"]["property_iri"]
        ]
        self.assertEqual(2, len(identity_rows))
        self.assertEqual(1, len({row["fact_id"] for row in identity_rows}))
        self.assertRegex(identity_rows[0]["fact_id"], r"^fact-v1-[0-9a-f]{64}$")

        first_tree = {
            path.relative_to(first_release).as_posix(): path.read_bytes()
            for path in first_release.rglob("*")
            if path.is_file()
        }
        shutil.rmtree(self.output)
        seed_golden_identity(self.output)
        second_completed, second_release = build(reverse=False, suffix="forward")
        self.assertEqual(expected, {path: (second_release / path).read_bytes() for path in canonical_paths})
        self.assertEqual(first_completed["snapshot_id"], second_completed["snapshot_id"])
        self.assertEqual(
            first_tree,
            {
                path.relative_to(second_release).as_posix(): path.read_bytes()
                for path in second_release.rglob("*")
                if path.is_file()
            },
        )

    def test_schema_fixer_creates_a_fresh_abox_generation_and_budgets(self) -> None:
        self.auto_qa = False
        critic_work, card, _, _ = self.drive_to_critic()
        envelope = self.submit(
            critic_work, self.write_json("generation-critic.json", self.retain_all_payload(critic_work))
        )
        qa_work = envelope["pending_work_details"][0]
        finding = {
            "reason_code": "SCHEMA_COMMENT_INACCURATE", "target": "SCHEMA_CARD",
            "detail": "The class comment needs a complete Schema Card replacement.",
        }
        envelope = self.submit(
            qa_work,
            self.write_json(
                "generation-qa-fail.json",
                {"version": 1, "round": 1, "status": "FAIL", "findings": [finding]},
            ),
        )
        repaired = json.loads(json.dumps(card))
        repaired["classes"][0]["comment"] = "A source-backed customer."
        envelope = self.submit(
            envelope["pending_work_details"][0],
            self.write_json(
                "generation-fixer.json",
                {"version": 1, "round": 1, "target": "SCHEMA_CARD", "replacement": repaired},
            ),
        )
        entity_work = envelope["pending_work_details"][0]
        self.assertEqual("ENTITY", entity_work["stage"])
        self.assertEqual(0, entity_work["attempt_count"])
        self.assertEqual("INITIAL", entity_work["input"]["invocation_kind"])
        self.assertEqual(1, entity_work["input"]["invocation_sequence"])
        self.assertNotEqual(
            critic_work["input"]["schema_card_sha256"], entity_work["input"]["schema_card_sha256"]
        )

        envelope = self.submit(
            entity_work,
            self.write_json(
                "generation-entities.json", self.entity_payload(entity_work, repaired, "Alice", "O-1")
            ),
        )
        assertion_work = envelope["pending_work_details"][0]
        envelope = self.submit(
            assertion_work,
            self.write_json("generation-assertions.json", self.assertion_payload(assertion_work, repaired)),
        )
        critic_work = envelope["pending_work_details"][0]
        envelope = self.submit(
            critic_work,
            self.write_json("generation-final-critic.json", self.retain_all_payload(critic_work)),
        )
        qa_work = envelope["pending_work_details"][0]
        self.assertEqual(2, qa_work["input"]["round"])
        completed = self.submit(
            qa_work,
            self.write_json(
                "generation-qa-pass.json",
                {"version": 1, "round": 2, "status": "PASS", "findings": []},
            ),
        )
        self.assertEqual("PASS", completed["delivery_status"])

    def test_terminal_abox_finding_cannot_create_a_fixer(self) -> None:
        self.auto_qa = False
        critic_work, _, _, _ = self.drive_to_critic()
        envelope = self.submit(
            critic_work, self.write_json("abox-qa-critic.json", self.retain_all_payload(critic_work))
        )
        qa_work = envelope["pending_work_details"][0]
        chunk_id = qa_work["input"]["coverage"]["completed_chunk_ids"][0]
        completed = self.submit(
            qa_work,
            self.write_json(
                "abox-qa-fail.json",
                {
                    "version": 1, "round": 1, "status": "FAIL",
                    "findings": [
                        {
                            "reason_code": "ABOX_FACT_UNFAITHFUL", "target": "ABOX_CHUNK",
                            "chunk_id": chunk_id, "detail": "The terminal ABox chunk cannot be repaired in-run.",
                        }
                    ],
                },
            ),
        )
        self.assertEqual("FORCED_WITH_ERRORS", completed["delivery_status"])
        self.assertEqual([], completed["pending_work_items"])


if __name__ == "__main__":
    unittest.main()
