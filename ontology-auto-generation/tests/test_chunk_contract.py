from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
CLI = SCRIPTS_DIR / "ontology_pipeline.py"
sys.path.insert(0, str(SCRIPTS_DIR))

from chunk_contract import ChunkPolicy, chunk_document, chunk_source_bytes, parse_blocks


class ChunkContractTest(unittest.TestCase):
    def test_normalization_equivalence_and_strict_utf8(self) -> None:
        expected = chunk_source_bytes("docs/a.md", b"# A\nBody\n", "abox")
        actual = chunk_source_bytes("docs/a.md", b"\xef\xbb\xbf# A\r\nBody\r", "abox")
        self.assertEqual(expected, actual)
        with self.assertRaises(UnicodeDecodeError):
            chunk_source_bytes("docs/a.md", b"\xff", "abox")

    def test_rejects_noncanonical_paths(self) -> None:
        for path in ("/a.md", "../a.md", "docs\\a.md", "./a.md", "docs//a.md", ""):
            with self.subTest(path=path), self.assertRaises(ValueError):
                chunk_document(path, "x", "abox")

    def test_structured_parser_and_exact_primary_reconstruction(self) -> None:
        markdown = (
            "# Title\n\nIntro\n\n"
            "| Name | Value |\n| --- | --- |\n| A | 1 |\n\n"
            "```json\n{\"a\": 1}\n```\n\n- item\n"
        )
        blocks = parse_blocks(markdown)
        self.assertEqual(
            ["heading", "blank", "prose", "blank", "table", "blank", "fenced_code", "blank", "prose"],
            [block.kind for block in blocks],
        )
        for view in ("tbox", "abox"):
            manifest = chunk_document("source.md", markdown, view)
            self.assertEqual(markdown, "".join(chunk["primary"]["text"] for chunk in manifest["chunks"]))

    def test_dual_view_boundaries_overlap_and_stable_identity(self) -> None:
        markdown = "# A\n" + "a" * 30 + "\n## B\n" + "b" * 30 + "\n### C\n" + "c" * 30 + "\n"
        tbox = chunk_document("source.md", markdown, "tbox", ChunkPolicy(100, 140, 20, ()))
        abox = chunk_document("source.md", markdown, "abox", ChunkPolicy(70, 100, 20, (1, 2)))
        self.assertLess(len(tbox["chunks"]), len(abox["chunks"]))
        self.assertTrue(all(chunk["context_before"] is None or len(chunk["context_before"]["text"]) <= 20 for chunk in abox["chunks"]))

        shifted = chunk_document("source.md", "preface\n" + markdown, "abox", ChunkPolicy(70, 100, 20, (1, 2)))
        original_b = next(chunk for chunk in abox["chunks"] if chunk["heading_path"] == ["A", "B"])
        shifted_b = next(chunk for chunk in shifted["chunks"] if chunk["heading_path"] == ["A", "B"])
        self.assertEqual(original_b["chunk_id"], shifted_b["chunk_id"])
        self.assertNotEqual(original_b["primary"]["line_start"], shifted_b["primary"]["line_start"])

    def test_protected_and_physical_line_oversize_warnings(self) -> None:
        policy = ChunkPolicy(20, 30, 5, (1, 2))
        table = "| A | B |\n| --- | --- |\n| " + "x" * 40 + " | y |\n"
        manifest = chunk_document("source.md", table, "abox", policy)
        self.assertEqual(1, len(manifest["chunks"]))
        self.assertIn("PROTECTED_BLOCK_OVERSIZE", {row["code"] for row in manifest["chunks"][0]["warnings"]})

        unclosed = chunk_document("source.md", "```\n" + "x" * 40, "abox", policy)
        codes = {row["code"] for row in unclosed["chunks"][0]["warnings"]}
        self.assertEqual({"UNCLOSED_FENCE", "PROTECTED_BLOCK_OVERSIZE"}, codes)

        line = chunk_document("source.md", "x" * 31 + "\nshort\n", "abox", policy)
        self.assertIn("SOURCE_LINE_OVERSIZE", {row["code"] for row in line["chunks"][0]["warnings"]})

    def test_empty_source_and_duplicate_identity(self) -> None:
        empty = chunk_document("empty.md", "", "abox")
        self.assertEqual([], empty["chunks"])
        self.assertEqual([{"code": "EMPTY_SOURCE"}], empty["warnings"])

        repeated = chunk_document("source.md", "same\nsame\n", "abox", ChunkPolicy(4, 5, 0, ()))
        ids = [chunk["chunk_id"] for chunk in repeated["chunks"]]
        self.assertEqual(len(ids), len(set(ids)))


class ChunkLifecycleIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.output = self.root / "output"
        self.workspace.mkdir()
        (self.workspace / "b.md").write_text("# B\nBody\n", encoding="utf-8")
        (self.workspace / "a.md").write_text("", encoding="utf-8")

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

    def test_start_persists_sorted_dual_view_manifests_and_resume_observes_stage(self) -> None:
        result = self.run_cli(
            "run", "start", "--workspace", str(self.workspace), "--output", str(self.output),
            "--source", "b.md", "--source", "a.md", "--source", "b.md",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        envelope = json.loads(result.stdout)
        self.assertEqual("MANIFESTS_READY", envelope["current_stage"])
        run_root = self.output / ".staging" / envelope["run_id"]
        for view in ("tbox", "abox"):
            a_manifest = json.loads((run_root / "manifests" / view / "a.md.json").read_text())
            b_manifest = json.loads((run_root / "manifests" / view / "b.md.json").read_text())
            self.assertEqual([], a_manifest["chunks"])
            self.assertEqual("b.md", b_manifest["source"]["path"])

        ledger = json.loads((self.output / "ledger.json").read_text())
        self.assertEqual(["a.md", "b.md"], [row["path"] for row in ledger["active_run"]["config"]["sources"]])
        self.assertEqual("MANIFESTS_READY", ledger["active_run"]["current_stage"])
        self.assertEqual(1, len(ledger["active_run"]["pending_work"]))
        self.assertTrue(ledger["active_run"]["pending_work"][0].startswith("work-v1-cq-"))

        resumed = self.run_cli("run", "resume", "--output", str(self.output))
        self.assertEqual(0, resumed.returncode, resumed.stderr)
        self.assertEqual("MANIFESTS_READY", json.loads(resumed.stdout)["current_stage"])


if __name__ == "__main__":
    unittest.main()
