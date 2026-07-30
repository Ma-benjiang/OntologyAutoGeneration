from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


GOLDEN_IRI = "https://example.org/ontology/abox-v1-golden"
GOLDEN_ROOT = Path(__file__).resolve().parent / "golden"


def seed_golden_identity(output: Path) -> None:
    artifacts = output / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "schema_card.json").write_text(
        json.dumps(
            {
                "version": 1,
                "ontology_iri": GOLDEN_IRI,
                "entity_namespace": GOLDEN_IRI + "#",
                "classes": [],
                "object_properties": [],
                "datatype_properties": [],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )


def _normalize_public_state(value: Any, *, output: Path, workspace: Path) -> Any:
    if isinstance(value, list):
        return [
            _normalize_public_state(item, output=output, workspace=workspace)
            for item in value
        ]
    if not isinstance(value, dict):
        return value
    normalized = {}
    for key, item in value.items():
        if key == "run_id":
            normalized[key] = "<RUN_ID>"
        elif key == "workspace":
            normalized[key] = "<WORKSPACE>"
        elif key == "project_digest":
            normalized[key] = "<PROJECT_DIGEST>"
        elif key == "config_digest":
            normalized[key] = "<CONFIG_DIGEST>"
        elif key == "digest" and set(value) == {"algorithm", "digest"}:
            normalized[key] = "<INTEGRITY_DIGEST>"
        else:
            normalized[key] = _normalize_public_state(
                item, output=output, workspace=workspace
            )
    return normalized


def assert_release_golden(
    test,
    name: str,
    *,
    output: Path,
    workspace: Path,
    terminal_envelope: dict,
    run_cli,
) -> None:
    snapshot_id = terminal_envelope["snapshot_id"]
    release = output / "releases" / snapshot_id
    snapshot_files = {
        path.relative_to(release).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(release.rglob("*"))
        if path.is_file()
    }
    status_result = run_cli("run", "status", "--output", str(output), "--json")
    test.assertEqual(0, status_result.returncode, status_result.stderr or status_result.stdout)
    actual = {
        "snapshot_id": snapshot_id,
        "snapshot_files": snapshot_files,
        "latest_attempt": _normalize_public_state(
            json.loads((output / "latest_attempt.json").read_text()),
            output=output,
            workspace=workspace,
        ),
        "latest_delivery": (
            _normalize_public_state(
                json.loads((output / "latest_delivery.json").read_text()),
                output=output,
                workspace=workspace,
            )
            if (output / "latest_delivery.json").exists()
            else None
        ),
        "ledger": _normalize_public_state(
            json.loads((output / "ledger.json").read_text()),
            output=output,
            workspace=workspace,
        ),
        "terminal_envelope": _normalize_public_state(
            terminal_envelope, output=output, workspace=workspace
        ),
        "terminal_status": _normalize_public_state(
            json.loads(status_result.stdout), output=output, workspace=workspace
        ),
    }
    golden = GOLDEN_ROOT / f"{name}.json"
    content = json.dumps(actual, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if os.environ.get("UPDATE_RELEASE_GOLDENS") == "1":
        GOLDEN_ROOT.mkdir(parents=True, exist_ok=True)
        golden.write_text(content, encoding="utf-8")
    test.assertTrue(golden.exists(), f"missing golden fixture: {golden}")
    test.assertEqual(golden.read_text(encoding="utf-8"), content)
