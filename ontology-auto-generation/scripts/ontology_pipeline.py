#!/usr/bin/env python3
"""Resolve grounded ABox candidates and build RDF/XML OWL artifacts.

The public interface is intentionally small:

    ontology_pipeline.py run start --workspace DIR --output DIR --source FILE...
    ontology_pipeline.py run status --output DIR --json
    ontology_pipeline.py run resume --output DIR
    ontology_pipeline.py run abort --output DIR
    ontology_pipeline.py resolve SCHEMA CANDIDATES --workspace DIR --source FILE...
    ontology_pipeline.py build SCHEMA RESOLVED --output-dir DIR

OWL files are derived artifacts. Edit the Schema Card or candidate JSON, then rerun
this script instead of editing generated OWL by hand.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import unicodedata
import uuid
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from chunk_contract import chunk_source_bytes
from xsd_profile import ALLOWED_XSD_DATATYPES, XSD_PREFIX, literal_is_valid

LOCAL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


class PipelineError(ValueError):
    pass


class RunLifecycleError(PipelineError):
    """A closed, machine-readable failure in the public run lifecycle."""

    def __init__(self, code: str, message: str, exit_code: int = 2):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise RunLifecycleError("invalid_input", message, 2)


RUN_CONTRACT_VERSION = 1
GLOBAL_STAGES = {"CQ", "SRD", "SCHEMA_CARD"}
QA_STAGES = {"QA_GATE_1", "FIXER"}
def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _rejected_response(command: str, error: RunLifecycleError) -> dict:
    return {
        "command": command,
        "accepted": False,
        "status": "rejected",
        "run_state": None,
        "delivery_status": None,
        "pending_work_items": [],
        "error_code": error.code,
        "error": {"code": error.code, "message": str(error)},
    }


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _digest_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _integrity_write(path: Path, value: dict) -> str:
    digest = _digest_text(_canonical_json(value))
    document = {**value, "_integrity": {"algorithm": "sha256", "digest": digest}}
    _atomic_text(path, _canonical_json(document) + "\n")
    return digest


def _integrity_read(path: Path, label: str) -> tuple[dict, str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RunLifecycleError("ledger_corrupt", f"{label} 无法读取或解析", 5) from exc
    if not isinstance(value, dict):
        raise RunLifecycleError("ledger_corrupt", f"{label} 顶层必须是对象", 5)
    integrity = value.pop("_integrity", None)
    if not isinstance(integrity, dict) or set(integrity) != {"algorithm", "digest"}:
        raise RunLifecycleError("ledger_corrupt", f"{label} 完整性记录无效", 5)
    actual = _digest_text(_canonical_json(value))
    if integrity.get("algorithm") != "sha256" or integrity.get("digest") != actual:
        raise RunLifecycleError("ledger_corrupt", f"{label} 完整性校验失败", 5)
    return value, actual


def _project_paths(output_dir: Path) -> dict[str, Path]:
    root = output_dir
    return {
        "root": root,
        "project": root / "project.json",
        "ledger": root / "ledger.json",
        "lock": root / ".ontology-project.lock",
        "transaction_lock": root / ".ontology-project.transaction.lock",
        "latest_attempt": root / "latest_attempt.json",
        "latest_delivery": root / "latest_delivery.json",
        "staging": root / ".staging",
        "releases": root / "releases",
    }


@contextmanager
def _project_transaction(paths: dict[str, Path]):
    try:
        handle = paths["transaction_lock"].open("a+b")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except OSError as exc:
        raise RunLifecycleError("lock_conflict", "无法锁定 Ontology Project 状态事务", 3) from exc
    try:
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _ensure_directory(path: Path, label: str, *, create: bool = False, writable: bool = False) -> Path:
    try:
        resolved = path.resolve()
        if not resolved.exists():
            if not create:
                raise RunLifecycleError("invalid_input", f"{label} 不存在")
            resolved.mkdir(parents=True, exist_ok=True)
        if not resolved.is_dir():
            raise RunLifecycleError("invalid_input", f"{label} 必须是目录")
        required_access = (os.W_OK if writable else os.R_OK) | os.X_OK
        if not os.access(resolved, required_access):
            permission = "写入" if writable else "读取"
            raise RunLifecycleError("invalid_input", f"{label} 无法{permission}")
        return resolved
    except RunLifecycleError:
        raise
    except OSError as exc:
        raise RunLifecycleError("invalid_input", f"{label} 基础路径或权限校验失败") from exc


def _source_records(workspace: Path, sources: list[Path]) -> list[dict]:
    root = _ensure_directory(workspace, "workspace")
    for source in sources:
        text = str(source)
        pure = PurePosixPath(text)
        if source.is_absolute() or pure.is_absolute() or ".." in pure.parts or "\\" in text or pure.as_posix() != text:
            raise RunLifecycleError("invalid_input", f"Source Document 必须是规范的 workspace-relative POSIX path: {text}")
    try:
        selected = _canonical_sources(root, sources)
    except PipelineError as exc:
        raise RunLifecycleError("invalid_input", str(exc)) from exc
    records = []
    for relative in sorted(selected):
        source = root / relative
        try:
            payload = source.read_bytes()
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RunLifecycleError("invalid_input", f"Source Document 不是有效 UTF-8: {relative}") from exc
        except OSError as exc:
            raise RunLifecycleError("invalid_input", f"无法读取 Source Document: {relative}") from exc
        normalized = text.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
        records.append({"path": relative, "sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest()})
    return records


def _derive_ontology_iri(output_dir: Path) -> str:
    ascii_name = unicodedata.normalize("NFKD", output_dir.name).encode("ascii", "ignore").decode("ascii")
    slug = "-".join(re.findall(r"[a-z0-9]+", ascii_name.casefold()))[:48] or "ontology"
    return f"https://example.org/ontology/{slug}-{_digest(str(output_dir), 12)}"


def _require_exact_keys(value: dict, expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise RunLifecycleError("ledger_corrupt", f"{label} 字段集合无效", 5)


def _initial_ontology_iri(output_dir: Path) -> str:
    legacy_card = output_dir / "artifacts" / "schema_card.json"
    if not legacy_card.exists():
        if any((output_dir / name).exists() for name in ("ontology.owl", "schema.owl", "instances.owl")):
            raise RunLifecycleError("ledger_corrupt", "既有本体缺少可恢复的 Schema Card identity", 5)
        return _derive_ontology_iri(output_dir)
    try:
        card = _read_json(legacy_card)
        validate_schema_card(card)
    except PipelineError as exc:
        raise RunLifecycleError("ledger_corrupt", "既有 Schema Card identity 不可信", 5) from exc
    return card["ontology_iri"]


def _read_project(paths: dict[str, Path]) -> tuple[dict, str]:
    if not paths["project"].exists():
        raise RunLifecycleError("project_missing", "Ontology Project 尚未创建")
    project, digest = _integrity_read(paths["project"], "project identity")
    _require_exact_keys(project, {"version", "ontology_iri", "entity_namespace", "location_summary"}, "project identity")
    if project.get("version") != RUN_CONTRACT_VERSION:
        raise RunLifecycleError("contract_mismatch", "project identity contract version 不受支持", 3)
    location = project.get("location_summary")
    if not isinstance(location, dict):
        raise RunLifecycleError("ledger_corrupt", "项目位置摘要无效", 5)
    _require_exact_keys(location, {"path_digest", "output_name"}, "项目位置摘要")
    if (
        not _valid_absolute_iri(project.get("ontology_iri"))
        or project.get("entity_namespace") != str(project.get("ontology_iri", "")).rstrip("#") + "#"
        or location.get("path_digest") != _digest(str(paths["root"]), 12)
        or location.get("output_name") != paths["root"].name
    ):
        raise RunLifecycleError("ledger_corrupt", "项目身份的 Entity Namespace 不一致", 5)
    return project, digest


def _validate_run_config(config: object) -> None:
    if not isinstance(config, dict):
        raise RunLifecycleError("ledger_corrupt", "运行配置结构无效", 5)
    _require_exact_keys(config, {"contract_version", "workspace", "sources"}, "运行配置")
    sources = config.get("sources")
    if (
        config.get("contract_version") != RUN_CONTRACT_VERSION
        or not isinstance(config.get("workspace"), str)
        or not config["workspace"]
        or not isinstance(sources, list)
    ):
        raise RunLifecycleError("ledger_corrupt", "运行配置结构无效", 5)
    for source in sources:
        if not isinstance(source, dict):
            raise RunLifecycleError("ledger_corrupt", "Source Document 摘要结构无效", 5)
        _require_exact_keys(source, {"path", "sha256"}, "Source Document 摘要")
        if (
            not isinstance(source["path"], str)
            or not source["path"]
            or PurePosixPath(source["path"]).is_absolute()
            or ".." in PurePosixPath(source["path"]).parts
            or "\\" in source["path"]
            or PurePosixPath(source["path"]).as_posix() != source["path"]
            or not isinstance(source["sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", source["sha256"]) is None
        ):
            raise RunLifecycleError("ledger_corrupt", "Source Document 摘要结构无效", 5)


def _validate_run_record(active: dict) -> None:
    _require_exact_keys(
        active,
        {
            "run_id",
            "run_state",
            "current_stage",
            "delivery_status",
            "pending_work",
            "recent_errors",
            "project_digest",
            "config_digest",
            "config",
        },
        "运行记录",
    )
    if (
        not isinstance(active["run_id"], str)
        or not active["run_id"].startswith("run-")
        or re.fullmatch(r"[0-9a-f]{64}", str(active["project_digest"])) is None
        or re.fullmatch(r"[0-9a-f]{64}", str(active["config_digest"])) is None
        or not isinstance(active["pending_work"], list)
        or not all(isinstance(item, str) for item in active["pending_work"])
        or not isinstance(active["recent_errors"], list)
    ):
        raise RunLifecycleError("ledger_corrupt", "运行记录结构无效", 5)
    for error in active["recent_errors"]:
        if not isinstance(error, dict) or set(error) != {"code"} or not isinstance(error["code"], str):
            raise RunLifecycleError("ledger_corrupt", "最近错误结构无效", 5)
    if active["run_state"] == "ACTIVE":
        stage_prefixes = {
            "MANIFESTS_READY": ("work-v1-cq-",),
            "CQ": ("work-v1-cq-",),
            "SRD": ("work-v1-srd-",),
            "SCHEMA_CARD": ("work-v1-schema-card-",),
            "SCHEMA_LOCKED": ("work-v1-entity-",),
            "ABOX_WORK": ("work-v1-entity-", "work-v1-assertion-", "work-v1-candidate-critic-"),
            "QA_GATE_1": ("work-v1-qa-gate-1-",),
            "FIXER": ("work-v1-fixer-",),
        }
        valid_stage = active["current_stage"] == "ORCHESTRATION" and active["pending_work"] == ["orchestration"]
        if active["current_stage"] in stage_prefixes:
            prefixes = stage_prefixes[active["current_stage"]]
            valid_stage = bool(active["pending_work"]) and all(
                isinstance(item, str) and item.startswith(prefixes) for item in active["pending_work"]
            )
            if active["current_stage"] == "SCHEMA_LOCKED":
                valid_stage = all(isinstance(item, str) and item.startswith(prefixes) for item in active["pending_work"])
        valid_state = valid_stage and active["delivery_status"] is None and not active["recent_errors"]
    elif active["run_state"] == "FAILED":
        valid_state = (
            active["current_stage"] == "RELEASE_SNAPSHOT"
            and active["delivery_status"] == "FAILED"
            and not active["pending_work"]
            and len(active["recent_errors"]) == 1
        )
    elif active["run_state"] == "COMPLETE":
        valid_state = (
            active["current_stage"] == "RELEASE_SNAPSHOT"
            and active["delivery_status"] in {"PASS", "FORCED_WITH_ERRORS"}
            and not active["pending_work"]
            and not active["recent_errors"]
        )
    else:
        valid_state = False
    if not valid_state:
        raise RunLifecycleError("ledger_corrupt", "运行状态组合无效", 5)
    _validate_run_config(active["config"])
    if active["config_digest"] != _digest_text(_canonical_json(active["config"])):
        raise RunLifecycleError("ledger_corrupt", "运行配置摘要不可信", 5)


def _read_ledger(paths: dict[str, Path], project_digest: str | None = None) -> tuple[dict, str]:
    if not paths["ledger"].exists():
        raise RunLifecycleError("ledger_corrupt", "运行账本不存在", 5)
    ledger, digest = _integrity_read(paths["ledger"], "运行账本")
    _require_exact_keys(ledger, {"version", "project_digest", "active_run", "latest_attempt", "latest_delivery"}, "运行账本")
    if (
        ledger.get("version") != RUN_CONTRACT_VERSION
        or not isinstance(ledger.get("active_run"), (dict, type(None)))
        or not isinstance(ledger.get("latest_attempt"), (str, type(None)))
        or not isinstance(ledger.get("latest_delivery"), (str, type(None)))
    ):
        raise RunLifecycleError("ledger_corrupt", "运行账本结构无效", 5)
    if isinstance(ledger["active_run"], dict):
        _validate_run_record(ledger["active_run"])
    for field in ("latest_attempt", "latest_delivery"):
        value = ledger[field]
        if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise RunLifecycleError("ledger_corrupt", f"{field} snapshot ID 无效", 5)
    if project_digest is not None and ledger.get("project_digest") != project_digest:
        raise RunLifecycleError("ledger_corrupt", "运行账本与项目身份不一致", 5)
    return ledger, digest


def _validate_latest_attempt_pointer(paths: dict[str, Path], ledger: dict) -> None:
    snapshot_id = ledger.get("latest_attempt")
    if snapshot_id is None:
        return
    try:
        pointer = json.loads(paths["latest_attempt"].read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RunLifecycleError("ledger_corrupt", "Latest Attempt 指针不可信", 5) from exc
    if not isinstance(pointer, dict):
        raise RunLifecycleError("ledger_corrupt", "Latest Attempt 指针结构无效", 5)
    _require_exact_keys(pointer, {"version", "snapshot_id", "run_id", "delivery_status"}, "Latest Attempt 指针")
    if (
        pointer["version"] != RUN_CONTRACT_VERSION
        or pointer["snapshot_id"] != snapshot_id
        or not isinstance(pointer["run_id"], str)
        or not isinstance(pointer["delivery_status"], str)
        or pointer["delivery_status"] not in {"PASS", "FORCED_WITH_ERRORS", "FAILED"}
    ):
        raise RunLifecycleError("ledger_corrupt", "Latest Attempt 指针与运行账本不一致", 5)
    _verify_release_snapshot(paths, snapshot_id)


def _validate_latest_delivery_pointer(paths: dict[str, Path], ledger: dict) -> None:
    snapshot_id = ledger.get("latest_delivery")
    if snapshot_id is None:
        return
    try:
        pointer = json.loads(paths["latest_delivery"].read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RunLifecycleError("ledger_corrupt", "Latest Delivery 指针不可信", 5) from exc
    if not isinstance(pointer, dict):
        raise RunLifecycleError("ledger_corrupt", "Latest Delivery 指针结构无效", 5)
    _require_exact_keys(pointer, {"version", "snapshot_id", "run_id", "delivery_status"}, "Latest Delivery 指针")
    if (
        pointer["version"] != RUN_CONTRACT_VERSION
        or pointer["snapshot_id"] != snapshot_id
        or not isinstance(pointer["run_id"], str)
        or not isinstance(pointer["delivery_status"], str)
        or pointer["delivery_status"] not in {"PASS", "FORCED_WITH_ERRORS"}
    ):
        raise RunLifecycleError("ledger_corrupt", "Latest Delivery 指针与运行账本不一致", 5)
    _verify_release_snapshot(paths, snapshot_id)


def _validate_release_pointers(paths: dict[str, Path], ledger: dict) -> None:
    _validate_latest_attempt_pointer(paths, ledger)
    _validate_latest_delivery_pointer(paths, ledger)


def _validate_release_pointers_with_pending(
    paths: dict[str, Path],
    ledger: dict,
    snapshot_id: str,
    delivery_status: str,
) -> None:
    def validate(field: str) -> None:
        path = paths[field]
        if not path.exists():
            if ledger.get(field) is not None:
                raise RunLifecycleError("ledger_corrupt", f"{field} 指针缺失", 5)
            return
        try:
            pointer = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RunLifecycleError("ledger_corrupt", f"{field} 指针不可信", 5) from exc
        candidate = dict(ledger)
        if isinstance(pointer, dict) and pointer.get("snapshot_id") == snapshot_id:
            candidate[field] = snapshot_id
        if field == "latest_attempt":
            _validate_latest_attempt_pointer(paths, candidate)
        else:
            _validate_latest_delivery_pointer(paths, candidate)

    validate("latest_attempt")
    if delivery_status != "FAILED":
        validate("latest_delivery")
    else:
        _validate_latest_delivery_pointer(paths, ledger)


def _acquire_lock(paths: dict[str, Path], run_id: str) -> None:
    paths["root"].mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=paths["root"], prefix=".lock.", suffix=".tmp", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(run_id + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, paths["lock"])
    except FileExistsError as exc:
        raise RunLifecycleError("lock_conflict", "该 Ontology Project 已有 active Full Rebuild", 3) from exc
    except OSError as exc:
        raise RunLifecycleError("lock_conflict", "无法创建项目锁", 3) from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _release_lock(paths: dict[str, Path]) -> None:
    try:
        paths["lock"].unlink()
    except FileNotFoundError:
        pass


def _active_run(ledger: dict, run_id: str | None = None) -> dict:
    active = ledger.get("active_run")
    if not isinstance(active, dict) or active.get("run_state") != "ACTIVE":
        raise RunLifecycleError("run_not_active", "当前没有可操作的 active Full Rebuild", 3)
    if run_id is not None and active.get("run_id") != run_id:
        raise RunLifecycleError("run_not_found", "指定 run_id 不是当前 active Full Rebuild", 3)
    return active


def _validate_resume_config(active: dict, workspace: Path | None, sources: list[Path] | None) -> None:
    config = active.get("config")
    _validate_run_config(config)
    configured_workspace_value = config.get("workspace")
    configured_sources = config.get("sources")
    configured_workspace = Path(configured_workspace_value).resolve()
    if workspace is not None and workspace.resolve() != configured_workspace:
        raise RunLifecycleError("config_drift", "workspace 配置已漂移", 3)
    current_records = _source_records(configured_workspace, [Path(item["path"]) for item in configured_sources])
    if current_records != configured_sources:
        raise RunLifecycleError("config_drift", "Source Document 内容或选择已漂移", 3)
    if sources is not None:
        requested = _source_records(configured_workspace, sources)
        if requested != configured_sources:
            raise RunLifecycleError("config_drift", "Source Document 选择已漂移", 3)


def _validate_staging(
    paths: dict[str, Path],
    active: dict,
    project_digest: str,
    *,
    allow_legacy_term_registry: bool = False,
) -> None:
    run_id = active.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise RunLifecycleError("ledger_corrupt", "active run_id 无效", 5)
    manifest, _ = _integrity_read(paths["staging"] / run_id / "manifest.json", "staging manifest")
    expected = {
        "version": RUN_CONTRACT_VERSION,
        "run_id": run_id,
        "project_digest": project_digest,
        "config_digest": active.get("config_digest"),
        "artifacts": manifest.get("artifacts"),
    }
    if manifest != expected or active.get("project_digest") != project_digest:
        raise RunLifecycleError("ledger_corrupt", "staging manifest 与运行账本不一致", 5)
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise RunLifecycleError("ledger_corrupt", "staging artifact index 无效", 5)
    expected_paths = [
        f"manifests/{view}/{source['path']}.json"
        for view in ("abox", "tbox")
        for source in active["config"]["sources"]
    ]
    legacy_paths = list(expected_paths)
    expected_paths.append("inputs/term_identity_registry.json")
    artifact_paths: list[str] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict) or set(artifact) != {"path", "sha256"}:
            raise RunLifecycleError("ledger_corrupt", "staging artifact index 无效", 5)
        relative = artifact.get("path")
        digest = artifact.get("sha256")
        if (
            not isinstance(relative, str)
            or relative not in expected_paths
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise RunLifecycleError("ledger_corrupt", "staging artifact index 无效", 5)
        try:
            content = (paths["staging"] / run_id).joinpath(*PurePosixPath(relative).parts).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise RunLifecycleError("ledger_corrupt", "staging artifact 无法读取", 5) from exc
        if _digest_text(content) != digest:
            raise RunLifecycleError("ledger_corrupt", "staging artifact 摘要不一致", 5)
        artifact_paths.append(relative)
    allowed_paths = {tuple(sorted(expected_paths))}
    if allow_legacy_term_registry:
        allowed_paths.add(tuple(sorted(legacy_paths)))
    if tuple(sorted(artifact_paths)) not in allowed_paths:
        raise RunLifecycleError("ledger_corrupt", "staging artifact 集合无效", 5)
    _validate_work_item_store(paths["staging"] / run_id)


def _write_chunk_manifests(workspace: Path, records: list[dict], run_root: Path) -> list[dict]:
    artifacts: list[dict] = []
    for view in ("abox", "tbox"):
        for source in records:
            relative = source["path"]
            try:
                payload = (workspace / relative).read_bytes()
                manifest = chunk_source_bytes(relative, payload, view)
            except (OSError, UnicodeError, ValueError) as exc:
                raise RunLifecycleError("invalid_input", f"无法生成 Source Document manifest: {relative}") from exc
            content = _canonical_json(manifest) + "\n"
            artifact_path = f"manifests/{view}/{relative}.json"
            _atomic_text(run_root.joinpath(*PurePosixPath(artifact_path).parts), content)
            artifacts.append({"path": artifact_path, "sha256": _digest_text(content)})
    return sorted(artifacts, key=lambda item: item["path"])


def _read_json_strict(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RunLifecycleError("ledger_corrupt", f"{label} 无法读取或解析", 5) from exc
    if not isinstance(value, dict):
        raise RunLifecycleError("ledger_corrupt", f"{label} 顶层必须是对象", 5)
    return value


def _ordered_view_chunks(run_root: Path, records: list[dict], view: str) -> list[dict]:
    chunks: list[dict] = []
    for source in records:
        relative = PurePosixPath(f"manifests/{view}/{source['path']}.json")
        manifest = _read_json_strict(run_root.joinpath(*relative.parts), f"{view} manifest")
        manifest_chunks = manifest.get("chunks")
        if not isinstance(manifest_chunks, list):
            raise RunLifecycleError("ledger_corrupt", f"{view} manifest chunks 无效", 5)
        chunks.extend(sorted(manifest_chunks, key=lambda item: item.get("ordinal", 0)))
    return chunks


def _work_item_id(stage: str, sequence: int, input_digest: str) -> str:
    material = _canonical_json([RUN_CONTRACT_VERSION, stage, sequence, input_digest])
    return f"work-v{RUN_CONTRACT_VERSION}-{stage.lower().replace('_', '-')}-{_digest_text(material)}"


def _create_work_item(run_root: Path, stage: str, sequence: int, payload: dict) -> dict:
    input_digest = _digest_text(_canonical_json(payload))
    work_item_id = _work_item_id(stage, sequence, input_digest)
    root = run_root / "work_items" / work_item_id
    state = {
        "version": RUN_CONTRACT_VERSION,
        "work_item_id": work_item_id,
        "stage": stage,
        "logical_sequence": sequence,
        "input_digest": input_digest,
        "status": "PENDING",
        "output_digest": None,
    }
    if root.exists():
        existing_input = _read_json_strict(root / "input.json", "work item input")
        existing_state = _read_json_strict(root / "state.json", "work item state")
        if existing_input != payload or existing_state != state:
            raise RunLifecycleError("ledger_corrupt", "deterministic work item identity 冲突", 5)
    else:
        _atomic_text(root / "input.json", _canonical_json(payload) + "\n")
        _atomic_text(root / "state.json", _canonical_json(state) + "\n")
    return state


def _read_work_item(run_root: Path, work_item_id: str) -> tuple[dict, dict]:
    if re.fullmatch(r"work-v1-[a-z][a-z0-9-]*-[0-9a-f]{64}", work_item_id) is None:
        raise RunLifecycleError("unknown_work_item", "work item identity 无效", 4)
    root = run_root / "work_items" / work_item_id
    if not root.exists():
        raise RunLifecycleError("unknown_work_item", "work item 不存在", 4)
    state = _read_json_strict(root / "state.json", "work item state")
    payload = _read_json_strict(root / "input.json", "work item input")
    expected_keys = {
        "version", "work_item_id", "stage", "logical_sequence", "input_digest", "status", "output_digest"
    }
    if (
        set(state) != expected_keys
        or state.get("version") != RUN_CONTRACT_VERSION
        or state.get("work_item_id") != work_item_id
        or not isinstance(state.get("stage"), str)
        or not isinstance(state.get("logical_sequence"), int)
        or state.get("logical_sequence", 0) < 1
        or state.get("status") not in {"PENDING", "COMPLETE", "FAILED"}
        or not isinstance(state.get("input_digest"), str)
        or re.fullmatch(r"[0-9a-f]{64}", state["input_digest"]) is None
        or not isinstance(state.get("output_digest"), (str, type(None)))
    ):
        raise RunLifecycleError("ledger_corrupt", "work item state 无效", 5)
    digest = _digest_text(_canonical_json(payload))
    if state.get("input_digest") != digest or _work_item_id(state.get("stage"), state.get("logical_sequence"), digest) != work_item_id:
        raise RunLifecycleError("ledger_corrupt", "work item identity 无效", 5)
    return state, payload


def _is_latest_complete_semantic_invocation(
    run_root: Path, state: dict, payload: dict
) -> bool:
    chunk_id = payload.get("chunk", {}).get("chunk_id")
    current_sequence = payload.get("invocation_sequence", 1)
    for item_root in (run_root / "work_items").iterdir():
        if not item_root.is_dir() or item_root.name == state["work_item_id"]:
            continue
        other_state, other_payload = _read_work_item(run_root, item_root.name)
        if (
            other_state["stage"] == state["stage"]
            and other_state["status"] == "COMPLETE"
            and other_payload.get("chunk", {}).get("chunk_id") == chunk_id
            and other_payload.get("invocation_sequence", 1) > current_sequence
        ):
            reported = item_root / "reported_result.json"
            if (
                reported.exists()
                and _read_json_strict(reported, "reported work result").get("status") == "complete"
            ):
                return False
    return True


def _validate_work_item_store(run_root: Path) -> None:
    work_root = run_root / "work_items"
    if not work_root.is_dir():
        raise RunLifecycleError("ledger_corrupt", "work item store 不存在", 5)
    for item_root in sorted(work_root.iterdir()):
        if not item_root.is_dir():
            raise RunLifecycleError("ledger_corrupt", "work item store 包含无效文件", 5)
        state, payload = _read_work_item(run_root, item_root.name)
        attempts_root = item_root / "attempts"
        attempts = sorted(path for path in attempts_root.iterdir() if path.is_dir()) if attempts_root.exists() else []
        if attempts_root.exists() and any(not path.is_dir() for path in attempts_root.iterdir()):
            raise RunLifecycleError("ledger_corrupt", "work item attempts 包含无效文件", 5)
        if [path.name for path in attempts] != [f"{index:04d}" for index in range(1, len(attempts) + 1)]:
            raise RunLifecycleError("ledger_corrupt", "work item attempt sequence 无效", 5)
        last_digest = None
        for number, attempt_root in enumerate(attempts, start=1):
            attempt = _read_json_strict(attempt_root / "attempt.json", "work item attempt")
            expected_keys = {
                "version", "attempt", "work_item_id", "stage", "input_digest",
                "attempt_id", "chunk_id", "invocation_kind", "invocation_sequence", "execution_attempt",
                "raw_output_sha256", "normalized_output_sha256",
            }
            chunk = payload.get("chunk")
            chunk_id = chunk.get("chunk_id") if isinstance(chunk, dict) else None
            invocation_kind = payload.get("invocation_kind", "INITIAL")
            invocation_sequence = payload.get("invocation_sequence", 1)
            expected_attempt_id = "attempt-v1-" + _digest_text(
                _canonical_json(
                    [chunk_id, state["stage"], invocation_kind, state["logical_sequence"], invocation_sequence, number]
                )
            )
            if (
                set(attempt) != expected_keys
                or attempt["version"] != RUN_CONTRACT_VERSION
                or attempt["attempt"] != number
                or attempt["attempt_id"] != expected_attempt_id
                or attempt["chunk_id"] != chunk_id
                or attempt["invocation_kind"] != invocation_kind
                or attempt["invocation_sequence"] != invocation_sequence
                or attempt["execution_attempt"] != number
                or attempt["work_item_id"] != state["work_item_id"]
                or attempt["stage"] != state["stage"]
                or attempt["input_digest"] != state["input_digest"]
            ):
                raise RunLifecycleError("ledger_corrupt", "work item attempt manifest 无效", 5)
            attempt_input = _read_json_strict(attempt_root / "input.json", "work item attempt input")
            raw_files = sorted(attempt_root.glob("raw_output.*"))
            normalized_files = sorted(attempt_root.glob("normalized_output.*"))
            expected_files = {"attempt.json", "input.json"}
            expected_files.update(path.name for path in raw_files + normalized_files)
            actual_files = {path.name for path in attempt_root.iterdir() if path.is_file()}
            if attempt_input != payload or len(raw_files) != 1 or len(normalized_files) != 1 or actual_files != expected_files:
                raise RunLifecycleError("ledger_corrupt", "work item attempt artifact 集合无效", 5)
            try:
                raw = raw_files[0].read_bytes().decode("utf-8", errors="strict")
                normalized = normalized_files[0].read_bytes().decode("utf-8", errors="strict")
            except (OSError, UnicodeError) as exc:
                raise RunLifecycleError("ledger_corrupt", "work item attempt artifact 无法读取", 5) from exc
            if _digest_text(raw) != attempt["raw_output_sha256"] or _digest_text(normalized) != attempt["normalized_output_sha256"]:
                raise RunLifecycleError("ledger_corrupt", "work item attempt artifact 摘要不一致", 5)
            last_digest = attempt["normalized_output_sha256"]
        if state["status"] in {"COMPLETE", "FAILED"}:
            if not attempts or state["output_digest"] != last_digest:
                raise RunLifecycleError("ledger_corrupt", "terminal work item output digest 无效", 5)
        elif state["output_digest"] is not None:
            raise RunLifecycleError("ledger_corrupt", "pending work item 不得有 output digest", 5)
        reported_path = item_root / "reported_result.json"
        if reported_path.exists():
            reported = _read_json_strict(reported_path, "reported work result")
            reported_content = _canonical_json(reported) + "\n"
            if not attempts or _digest_text(reported_content) != last_digest:
                raise RunLifecycleError("ledger_corrupt", "reported work result 摘要不一致", 5)
            if state["stage"] in {"ENTITY", "ASSERTION", "CANDIDATE_CRITIC"}:
                _, final_path = _semantic_result_paths(run_root, state, reported)
                label = state["stage"].title()
                if (
                    state["status"] == "COMPLETE"
                    and reported.get("status") == "complete"
                    and _is_latest_complete_semantic_invocation(run_root, state, payload)
                ):
                    try:
                        final_content = final_path.read_bytes().decode("utf-8", errors="strict")
                    except (OSError, UnicodeError) as exc:
                        raise RunLifecycleError("ledger_corrupt", f"{label} final 无法读取", 5) from exc
                    if final_content != reported_content or reported.get("status") != "complete":
                        raise RunLifecycleError("ledger_corrupt", f"{label} final 与 reported result 不一致", 5)
                elif state["status"] == "PENDING":
                    pending_result_status = (
                        "request_reextraction" if state["stage"] == "CANDIDATE_CRITIC" else "retryable_failure"
                    )
                    if reported.get("status") != pending_result_status:
                        raise RunLifecycleError("ledger_corrupt", f"pending {label} reported result 状态无效", 5)
        elif state["stage"] in {"ENTITY", "ASSERTION", "CANDIDATE_CRITIC"} | QA_STAGES and state["status"] == "COMPLETE":
            raise RunLifecycleError("ledger_corrupt", f"completed {state['stage'].title()} 缺少 reported result", 5)


def _work_details(run_root: Path, work_ids: list[str]) -> list[dict]:
    details = []
    for work_id in work_ids:
        state, payload = _read_work_item(run_root, work_id)
        if state["status"] != "PENDING":
            raise RunLifecycleError("ledger_corrupt", "pending work set 引用了非 pending work item", 5)
        attempts_root = run_root / "work_items" / work_id / "attempts"
        attempt_count = len([path for path in attempts_root.iterdir() if path.is_dir()]) if attempts_root.exists() else 0
        details.append(
            {
                "work_item_id": work_id,
                "stage": state["stage"],
                "logical_sequence": state["logical_sequence"],
                "input_digest": state["input_digest"],
                "input": payload,
                "attempt_count": attempt_count,
            }
        )
    return details


def _global_input(run_root: Path, active_config: dict, stage: str, dependencies: dict[str, str]) -> dict:
    return {
        "version": RUN_CONTRACT_VERSION,
        "stage": stage,
        "logical_sequence": {"CQ": 1, "SRD": 2, "SCHEMA_CARD": 3}[stage],
        "tbox_chunks": _ordered_view_chunks(run_root, active_config["sources"], "tbox"),
        "dependencies": dependencies,
    }


def _latest_term_registry(paths: dict[str, Path], ledger: dict, project: dict) -> dict:
    snapshot_id = ledger.get("latest_delivery")
    if snapshot_id is None:
        registry_path = paths["root"] / "artifacts" / "schema_card.json"
        if not registry_path.exists():
            return {
                "version": RUN_CONTRACT_VERSION,
                "ontology_iri": project["ontology_iri"],
                "entity_namespace": project["entity_namespace"],
                "classes": [],
                "object_properties": [],
                "datatype_properties": [],
            }
        label = "Migrated Schema Card identity registry"
    else:
        registry_path = paths["releases"] / snapshot_id / "artifacts" / "schema_card.json"
        label = "Latest Delivery Schema Card identity registry"
    registry = _read_json_strict(registry_path, label)
    return _validate_term_registry(registry, project, label)


def _validate_term_registry(registry: dict, project: dict, label: str) -> dict:
    try:
        validate_schema_card(registry)
    except PipelineError as exc:
        raise RunLifecycleError("ledger_corrupt", f"{label} 不可信", 5) from exc
    if (
        registry["ontology_iri"] != project["ontology_iri"]
        or registry["entity_namespace"] != project["entity_namespace"]
    ):
        raise RunLifecycleError("ledger_corrupt", f"{label} 与 Ontology Project identity 不一致", 5)
    return registry


def _captured_term_registry(run_root: Path, project: dict) -> dict:
    registry = _read_json_strict(
        run_root / "inputs" / "term_identity_registry.json",
        "captured Schema Card identity registry",
    )
    return _validate_term_registry(
        registry, project, "captured Schema Card identity registry"
    )


def _migratable_term_registry(
    paths: dict[str, Path], ledger: dict, run_root: Path, project: dict
) -> dict:
    if ledger.get("latest_delivery") is not None:
        return _latest_term_registry(paths, ledger, project)
    staged_registries: dict[str, dict] = {}
    work_items_root = run_root / "work_items"
    if work_items_root.exists():
        for item_root in work_items_root.iterdir():
            if not item_root.is_dir():
                continue
            _, payload = _read_work_item(run_root, item_root.name)
            registry = payload.get("term_identity_registry")
            if registry is None:
                continue
            if not isinstance(registry, dict):
                raise RunLifecycleError(
                    "ledger_corrupt", "legacy term identity registry snapshot 无效", 5
                )
            validated = _validate_term_registry(
                registry, project, "legacy staged term identity registry"
            )
            staged_registries[_canonical_json(validated)] = validated
    if len(staged_registries) > 1:
        raise RunLifecycleError(
            "ledger_corrupt", "legacy term identity registry snapshot 不唯一", 5
        )
    if staged_registries:
        return next(iter(staged_registries.values()))
    schema_card_path = run_root / "artifacts" / "schema_card.json"
    if schema_card_path.exists():
        return _validate_term_registry(
            _read_json_strict(schema_card_path, "locked Schema Card identity registry"),
            project,
            "locked Schema Card identity registry",
        )
    raise RunLifecycleError(
        "ledger_corrupt", "legacy term identity registry 缺少可恢复快照", 5
    )


def _recover_active_lock(paths: dict[str, Path], run_id: str) -> None:
    if not paths["lock"].exists():
        _acquire_lock(paths, run_id)
        return
    try:
        owner = paths["lock"].read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RunLifecycleError("lock_conflict", "项目锁不可读取", 3) from exc
    if owner != run_id:
        raise RunLifecycleError("lock_conflict", "项目锁不属于当前 active Full Rebuild", 3)


def _migrate_term_identity_registry(
    paths: dict[str, Path], ledger: dict, project: dict, active: dict
) -> None:
    run_id = active.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return
    manifest_path = paths["staging"] / run_id / "manifest.json"
    if not manifest_path.exists():
        return
    manifest, _ = _integrity_read(manifest_path, "staging manifest")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        return
    registry_path = "inputs/term_identity_registry.json"
    if any(
        isinstance(artifact, dict) and artifact.get("path") == registry_path
        for artifact in artifacts
    ):
        return
    registry_content = _canonical_json(
        _migratable_term_registry(paths, ledger, paths["staging"] / run_id, project)
    ) + "\n"
    _atomic_text(
        (paths["staging"] / run_id).joinpath(*PurePosixPath(registry_path).parts),
        registry_content,
    )
    manifest["artifacts"] = sorted(
        [*artifacts, {"path": registry_path, "sha256": _digest_text(registry_content)}],
        key=lambda item: item["path"],
    )
    _integrity_write(manifest_path, manifest)


def _migrate_abox_candidates_artifact(run_root: Path) -> None:
    artifacts = run_root / "artifacts"
    canonical = artifacts / "abox_candidates.json"
    legacy = artifacts / "aggregate_candidates.json"
    if not legacy.exists():
        return
    try:
        if canonical.exists():
            if canonical.read_bytes() != legacy.read_bytes():
                raise RunLifecycleError(
                    "ledger_corrupt", "ABox candidate artifact migration 内容冲突", 5
                )
            legacy.unlink()
        else:
            os.replace(legacy, canonical)
    except RunLifecycleError:
        raise
    except OSError as exc:
        raise RunLifecycleError(
            "persistence_failed", "ABox candidate artifact migration 失败", 5
        ) from exc


def _run_start(args: argparse.Namespace) -> dict:
    workspace = _ensure_directory(args.workspace, "workspace")
    output_dir = _ensure_directory(args.output_dir, "output-dir", create=True, writable=True)
    paths = _project_paths(output_dir)
    records = _source_records(workspace, args.source)
    run_id = "run-" + uuid.uuid4().hex
    _acquire_lock(paths, run_id)
    created_project = False
    run_root = paths["staging"] / run_id
    try:
        latest_attempt = None
        latest_delivery = None
        if paths["project"].exists():
            project, project_digest = _read_project(paths)
            if paths["ledger"].exists():
                ledger, _ = _read_ledger(paths, project_digest)
                _validate_release_pointers(paths, ledger)
                if isinstance(ledger.get("active_run"), dict) and ledger["active_run"].get("run_state") == "ACTIVE":
                    raise RunLifecycleError("lock_conflict", "该 Ontology Project 已有 active Full Rebuild", 3)
                latest_attempt = ledger.get("latest_attempt")
                latest_delivery = ledger.get("latest_delivery")
        else:
            if any(paths[name].exists() for name in ("ledger", "latest_attempt", "latest_delivery")):
                raise RunLifecycleError("ledger_corrupt", "项目身份缺失但已有运行状态文件", 5)
            ontology_iri = _initial_ontology_iri(output_dir)
            project = {
                "version": RUN_CONTRACT_VERSION,
                "ontology_iri": ontology_iri,
                "entity_namespace": ontology_iri + "#",
                "location_summary": {
                    "path_digest": _digest(str(output_dir), 12),
                    "output_name": output_dir.name,
                },
            }
            project_digest = _integrity_write(paths["project"], project)
            created_project = True

        config = {
            "contract_version": RUN_CONTRACT_VERSION,
            "workspace": str(workspace),
            "sources": records,
        }
        config_digest = _digest_text(_canonical_json(config))
        artifacts = _write_chunk_manifests(workspace, records, run_root)
        registry_content = (
            _canonical_json(
                _latest_term_registry(
                    paths, {"latest_delivery": latest_delivery}, project
                )
            )
            + "\n"
        )
        registry_path = "inputs/term_identity_registry.json"
        _atomic_text(
            run_root.joinpath(*PurePosixPath(registry_path).parts),
            registry_content,
        )
        artifacts.append(
            {"path": registry_path, "sha256": _digest_text(registry_content)}
        )
        artifacts.sort(key=lambda item: item["path"])
        cq_work = _create_work_item(run_root, "CQ", 1, _global_input(run_root, config, "CQ", {}))
        active = {
            "run_id": run_id,
            "run_state": "ACTIVE",
            "current_stage": "MANIFESTS_READY",
            "delivery_status": None,
            "pending_work": [cq_work["work_item_id"]],
            "recent_errors": [],
            "project_digest": project_digest,
            "config_digest": config_digest,
            "config": config,
        }
        _integrity_write(
            paths["staging"] / run_id / "manifest.json",
            {
                "version": RUN_CONTRACT_VERSION,
                "run_id": run_id,
                "project_digest": project_digest,
                "config_digest": config_digest,
                "artifacts": artifacts,
            },
        )
        ledger = {
            "version": RUN_CONTRACT_VERSION,
            "project_digest": project_digest,
            "active_run": active,
            "latest_attempt": latest_attempt,
            "latest_delivery": latest_delivery,
        }
        _integrity_write(paths["ledger"], ledger)
    except Exception:
        _release_lock(paths)
        shutil.rmtree(run_root, ignore_errors=True)
        if created_project:
            try:
                paths["project"].unlink()
            except FileNotFoundError:
                pass
        raise
    return {
        "status": "accepted",
        "run_id": run_id,
        "run_state": "ACTIVE",
        "current_stage": "MANIFESTS_READY",
        "delivery_status": None,
        "pending_work_items": [cq_work["work_item_id"]],
        "pending_work_details": _work_details(run_root, [cq_work["work_item_id"]]),
        "ontology_iri": project["ontology_iri"],
        "entity_namespace": project["entity_namespace"],
    }


def _run_status(args: argparse.Namespace) -> dict:
    output_dir = _ensure_directory(args.output_dir, "output-dir")
    paths = _project_paths(output_dir)
    _, project_digest = _read_project(paths)
    ledger, _ = _read_ledger(paths, project_digest)
    pending_terminal = _pending_terminal_snapshot(paths, ledger, project_digest)
    if pending_terminal is None:
        _validate_latest_delivery_pointer(paths, ledger)
    else:
        _validate_release_pointers_with_pending(
            paths, ledger, pending_terminal[1], pending_terminal[2]
        )
    pending_abort = _pending_abort_snapshot(paths, ledger, project_digest)
    active = ledger.get("active_run")
    if isinstance(active, dict):
        _validate_staging(
            paths, active, project_digest, allow_legacy_term_registry=True
        )
    pointer_is_leading = (
        pending_abort is not None
        and isinstance(active, dict)
        and active.get("run_state") == "ACTIVE"
        and ledger.get("latest_attempt") != pending_abort[1]
    )
    if pending_terminal is None and not pointer_is_leading:
        _validate_latest_attempt_pointer(paths, ledger)
    if not isinstance(active, dict):
        return {
            "status": "ok",
            "data": {
                "project_digest": project_digest,
                "run_id": None,
                "current_stage": None,
                "run_state": "IDLE",
                "delivery_status": None,
                "pending_work": [],
                "recent_errors": [],
            },
        }
    return {
        "status": "ok",
        "data": {
            "project_digest": project_digest,
            "run_id": active.get("run_id"),
            "current_stage": active.get("current_stage"),
            "run_state": active.get("run_state"),
            "delivery_status": active.get("delivery_status"),
            "pending_work": active.get("pending_work", []),
            "recent_errors": active.get("recent_errors", []),
        },
        "pending_work_details": _work_details(
            paths["staging"] / active["run_id"],
            [work_id for work_id in active.get("pending_work", []) if work_id != "orchestration"],
        ),
        "work_item_results": _work_item_results(paths["staging"] / active["run_id"]),
    }


def _run_resume(args: argparse.Namespace) -> dict:
    output_dir = _ensure_directory(args.output_dir, "output-dir", writable=True)
    paths = _project_paths(output_dir)
    project, project_digest = _read_project(paths)
    ledger, _ = _read_ledger(paths, project_digest)
    recovered = _recover_terminal_commit(
        paths, ledger, project, project_digest, args.workspace, args.source
    )
    if recovered is not None:
        return recovered
    recovered = _recover_abort_commit(paths, ledger, project_digest)
    if recovered is not None:
        return recovered
    _validate_release_pointers(paths, ledger)
    active = _active_run(ledger)
    _migrate_term_identity_registry(paths, ledger, project, active)
    _validate_staging(paths, active, project_digest)
    _validate_resume_config(active, args.workspace, args.source)
    _recover_active_lock(paths, active["run_id"])
    run_root = paths["staging"] / active["run_id"]
    _migrate_abox_candidates_artifact(run_root)
    if (
        active["current_stage"] == "ORCHESTRATION"
        and active["pending_work"] == ["orchestration"]
    ):
        intent = (
            _terminal_intent(run_root, active)
            if (run_root / "terminal_intent.json").exists()
            else _legacy_terminal_intent(run_root, active)
        )
        _materialize_terminal_intent(run_root, intent)
        return _publish_run_completion(paths, ledger, active)
    if active["current_stage"] == "SCHEMA_LOCKED" and not active["pending_work"]:
        card = _read_json_strict(run_root / "artifacts" / "schema_card.json", "locked Schema Card")
        return _complete_empty_pass(paths, ledger, active, card)
    return {
        "status": "accepted",
        "run_id": active["run_id"],
        "run_state": active["run_state"],
        "current_stage": active["current_stage"],
        "delivery_status": active.get("delivery_status"),
        "pending_work_items": active.get("pending_work", []),
        "pending_work_details": _work_details(run_root, active.get("pending_work", [])),
        "work_item_results": _work_item_results(run_root),
        "resumed": True,
    }


def _normalize_submitted_output(raw: str, stage: str) -> tuple[str, object, str]:
    normalized_text = raw.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    if stage not in {"SCHEMA_CARD", "ENTITY", "ASSERTION", "CANDIDATE_CRITIC"} | QA_STAGES:
        return normalized_text, normalized_text, "md"
    try:
        value = json.loads(normalized_text)
    except json.JSONDecodeError as exc:
        code = "SCHEMA_CARD_INVALID" if stage == "SCHEMA_CARD" else "invalid_submission"
        raise RunLifecycleError(code, f"{stage} result 不是有效 JSON", 4) from exc
    if not isinstance(value, dict):
        code = "SCHEMA_CARD_INVALID" if stage == "SCHEMA_CARD" else "invalid_submission"
        raise RunLifecycleError(code, f"{stage} result 顶层必须是对象", 4)
    return _canonical_json(value) + "\n", value, "json"


def _record_attempt(
    run_root: Path,
    state: dict,
    payload: dict,
    raw: str,
    normalized: str,
    suffix: str,
) -> tuple[Path, dict]:
    attempts_root = run_root / "work_items" / state["work_item_id"] / "attempts"
    existing = [path for path in attempts_root.iterdir() if path.is_dir()] if attempts_root.exists() else []
    attempt_number = len(existing) + 1
    attempt_root = attempts_root / f"{attempt_number:04d}"
    if attempt_root.exists():
        raise RunLifecycleError("ledger_corrupt", "work item attempt identity 冲突", 5)
    manifest = {
        "version": RUN_CONTRACT_VERSION,
        "attempt": attempt_number,
        "attempt_id": "attempt-v1-" + _digest_text(
            _canonical_json(
                [
                    payload.get("chunk", {}).get("chunk_id"),
                    state["stage"],
                    payload.get("invocation_kind", "INITIAL"),
                    state["logical_sequence"],
                    payload.get("invocation_sequence", 1),
                    attempt_number,
                ]
            )
        ),
        "chunk_id": payload.get("chunk", {}).get("chunk_id"),
        "invocation_kind": payload.get("invocation_kind", "INITIAL"),
        "invocation_sequence": payload.get("invocation_sequence", 1),
        "execution_attempt": attempt_number,
        "work_item_id": state["work_item_id"],
        "stage": state["stage"],
        "input_digest": state["input_digest"],
        "raw_output_sha256": _digest_text(raw),
        "normalized_output_sha256": _digest_text(normalized),
    }
    _atomic_text(attempt_root / "input.json", _canonical_json(payload) + "\n")
    _atomic_text(attempt_root / f"raw_output.{suffix}", raw)
    _atomic_text(attempt_root / f"normalized_output.{suffix}", normalized)
    _atomic_text(attempt_root / "attempt.json", _canonical_json(manifest) + "\n")
    return attempt_root, manifest


def _complete_work_item(run_root: Path, state: dict, output_digest: str) -> None:
    completed = {**state, "status": "COMPLETE", "output_digest": output_digest}
    _atomic_text(run_root / "work_items" / state["work_item_id"] / "state.json", _canonical_json(completed) + "\n")


def _fail_work_item(run_root: Path, state: dict, output_digest: str) -> None:
    failed = {**state, "status": "FAILED", "output_digest": output_digest}
    _atomic_text(run_root / "work_items" / state["work_item_id"] / "state.json", _canonical_json(failed) + "\n")


def _dynamic_shapes(card: dict) -> str:
    index = validate_schema_card(card)
    closure = index["closure"]

    def iri_list(values: object) -> str:
        ordered = sorted(values)  # type: ignore[arg-type]
        return ", ".join(f"<{iri}>" for iri in ordered) if ordered else "<urn:ontology-auto-generation:no-locked-term>"

    lines = [
        "@prefix sh: <http://www.w3.org/ns/shacl#> .",
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .",
        "@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .",
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .",
        "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .",
        "",
    ]

    declared_properties = sorted(
        prop["iri"] for prop in card["object_properties"] + card["datatype_properties"]
    )
    allowed_predicates = ["rdf:type", "rdfs:label"] + [f"<{iri}>" for iri in declared_properties]
    lines.extend(
        [
            "<urn:ontology-auto-generation:dynamic-shape:whitelist> a sh:NodeShape ;",
            "  sh:targetClass owl:NamedIndividual ;",
            "  sh:sparql [",
            '    sh:message "NamedIndividual uses a predicate outside the locked Schema Card" ;',
            '    sh:select """',
            "      SELECT $this WHERE {",
            "        $this ?predicate ?value .",
            f"        FILTER (?predicate NOT IN ({', '.join(allowed_predicates)}))",
            "      }",
            '    """',
            "  ] ;",
            "  sh:sparql [",
            '    sh:message "NamedIndividual uses a Class outside the locked Schema Card" ;',
            '    sh:select """',
            "      SELECT $this WHERE {",
            "        $this rdf:type ?class .",
            "        FILTER (?class != owl:NamedIndividual)",
            f"        FILTER (?class NOT IN ({iri_list(index['classes'])}))",
            "      }",
            '    """',
            "  ] .",
            "",
            "<urn:ontology-auto-generation:dynamic-shape:term-whitelist> a sh:NodeShape ;",
            "  sh:targetSubjectsOf rdf:type ;",
            "  sh:sparql [",
            '    sh:message "OWL declaration is outside the locked Schema Card" ;',
            '    sh:select """',
            "      SELECT $this WHERE {",
            "        $this rdf:type ?kind .",
            "        FILTER (?kind IN (owl:Class, owl:ObjectProperty, owl:DatatypeProperty))",
            "        FILTER (",
            f"          (?kind = owl:Class && $this NOT IN ({iri_list(index['classes'])})) ||",
            f"          (?kind = owl:ObjectProperty && $this NOT IN ({iri_list(index['object_properties'])})) ||",
            f"          (?kind = owl:DatatypeProperty && $this NOT IN ({iri_list(index['datatype_properties'])}))",
            "        )",
            "      }",
            '    """',
            "  ] .",
            "",
        ]
    )

    class_representatives = closure["class_representatives"]
    class_edges = {tuple(edge) for edge in closure["class_super_edges"]}

    def reaches(edges: set[tuple[str, str]], start: str, target: str) -> bool:
        pending = [start]
        seen: set[str] = set()
        while pending:
            current = pending.pop()
            if current == target:
                return True
            if current in seen:
                continue
            seen.add(current)
            pending.extend(parent for child, parent in edges if child == current)
        return False

    def compatible_classes(expected: str) -> list[str]:
        expected_rep = class_representatives[expected]
        return sorted(
            iri
            for iri in index["classes"]
            if reaches(class_edges, class_representatives[iri], expected_rep)
        )

    properties = sorted(
        [("object", item) for item in card["object_properties"]]
        + [("datatype", item) for item in card["datatype_properties"]],
        key=lambda item: item[1]["iri"],
    )
    for ordinal, (kind, prop) in enumerate(properties, start=1):
        domain_values = ", ".join(f"<{iri}>" for iri in compatible_classes(prop["domain"]))
        lines.extend(
            [
                f"<urn:ontology-auto-generation:dynamic-shape:property:{ordinal}> a sh:NodeShape ;",
                f"  sh:targetSubjectsOf <{prop['iri']}> ;",
                "  sh:sparql [",
                f'    sh:message "Schema Card domain violation for {prop["iri"]}" ;',
                '    sh:select """',
                "      SELECT $this WHERE {",
                f"        $this <{prop['iri']}> ?value .",
                "        FILTER NOT EXISTS {",
                "          $this rdf:type ?actual .",
                f"          FILTER (?actual IN ({domain_values}))",
                "        }",
                "      }",
                '    """',
                "  ] ;",
            ]
        )
        if kind == "object":
            range_values = ", ".join(f"<{iri}>" for iri in compatible_classes(prop["range"]))
            lines.extend(
                [
                    "  sh:sparql [",
                    f'    sh:message "Schema Card object range violation for {prop["iri"]}" ;',
                    '    sh:select """',
                    "      SELECT $this WHERE {",
                    f"        $this <{prop['iri']}> ?value .",
                    "        FILTER NOT EXISTS {",
                    "          ?value rdf:type owl:NamedIndividual ; rdf:type ?actual .",
                    f"          FILTER (?actual IN ({range_values}))",
                    "        }",
                    "      }",
                    '    """',
                    "  ] .",
                ]
            )
        else:
            lines.extend(
                [
                    "  sh:sparql [",
                    f'    sh:message "Schema Card datatype violation for {prop["iri"]}" ;',
                    '    sh:select """',
                    "      SELECT $this WHERE {",
                    f"        $this <{prop['iri']}> ?value .",
                    f"        FILTER (!isLiteral(?value) || DATATYPE(?value) != <{prop['range']}>)",
                    "      }",
                    '    """',
                    "  ] .",
                ]
            )
        lines.append("")

    def semantic_contributors(kind: str, target: str) -> list[tuple[str, bool]]:
        if kind == "object":
            representatives = closure["object_property_representatives"]
            super_edges = {tuple(edge) for edge in closure["object_subproperty_edges"]}
            inverse_adjacency: dict[str, set[str]] = defaultdict(set)
            for left, right in closure["inverse_pairs"]:
                inverse_adjacency[left].add(right)
                inverse_adjacency[right].add(left)
        else:
            representatives = closure["datatype_property_representatives"]
            super_edges = {tuple(edge) for edge in closure["datatype_subproperty_edges"]}
            inverse_adjacency = defaultdict(set)
        target_rep = representatives[target]
        contributors: list[tuple[str, bool]] = []
        for iri in sorted(representatives):
            pending = [(representatives[iri], False)]
            seen: set[tuple[str, bool]] = set()
            while pending:
                state = pending.pop()
                if state in seen:
                    continue
                seen.add(state)
                representative, inverse = state
                pending.extend((parent, inverse) for child, parent in super_edges if child == representative)
                pending.extend((other, not inverse) for other in inverse_adjacency[representative])
            contributors.extend((iri, inverse) for representative, inverse in seen if representative == target_rep)
        return sorted(set(contributors))

    constrained = sorted(
        [("object", item) for item in card["object_properties"] if "max_count" in item]
        + [("datatype", item) for item in card["datatype_properties"] if "max_count" in item],
        key=lambda item: (item[0], item[1]["iri"]),
    )
    for ordinal, (kind, prop) in enumerate(constrained, start=1):
        branches = []
        for iri, inverse in semantic_contributors(kind, prop["iri"]):
            branches.append(
                f"{{ ?value <{iri}> $this }}" if inverse else f"{{ $this <{iri}> ?value }}"
            )
        union = " UNION\n        ".join(branches)
        lines.extend(
            [
                f"<urn:ontology-auto-generation:dynamic-shape:max-count:{ordinal}> a sh:NodeShape ;",
                "  sh:targetClass owl:NamedIndividual ;",
                "  sh:sparql [",
                f'    sh:message "Semantic max_count violation for {prop["iri"]}" ;',
                '    sh:select """',
                "      SELECT $this WHERE {",
                f"        {union}",
                "      }",
                "      GROUP BY $this",
                f"      HAVING (COUNT(DISTINCT ?value) > {prop['max_count']})",
                '    """',
                "  ] .",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _lock_schema(run_root: Path, active: dict, project: dict, card: dict) -> tuple[list[str], dict]:
    if card.get("ontology_iri") != project["ontology_iri"] or card.get("entity_namespace") != project["entity_namespace"]:
        raise PipelineError("SCHEMA_CARD_INVALID: Ontology Project identity mismatch")
    index = validate_schema_card(card)
    normalized = _canonical_json(card) + "\n"
    schema_digest = _digest_text(normalized)
    _atomic_text(run_root / "artifacts" / "schema_card.json", normalized)
    _atomic_text(run_root / "artifacts" / "dynamic_shapes.ttl", _dynamic_shapes(card))
    closure = index["closure"]
    lock = {
        "version": RUN_CONTRACT_VERSION,
        "schema_card_sha256": schema_digest,
        "closure_sha256": _digest_text(_canonical_json(closure)),
        "closure": closure,
    }
    _atomic_text(run_root / "artifacts" / "schema_lock.json", _canonical_json(lock) + "\n")

    chunks = _ordered_view_chunks(run_root, active["config"]["sources"], "abox")
    expected = {"version": RUN_CONTRACT_VERSION, "chunk_ids": [chunk["chunk_id"] for chunk in chunks]}
    _atomic_text(run_root / "artifacts" / "expected_abox_chunks.json", _canonical_json(expected) + "\n")
    entity_ids = []
    for sequence, chunk in enumerate(chunks, start=1):
        candidate_id_prefix = "c_" + chunk["chunk_id"].replace("-", "_")
        payload = {
            "version": RUN_CONTRACT_VERSION,
            "stage": "ENTITY",
            "logical_sequence": sequence,
            "invocation_kind": "INITIAL",
            "invocation_sequence": 1,
            "schema_card_sha256": schema_digest,
            "schema_card": card,
            "chunk": chunk,
            "candidate_id_prefix": candidate_id_prefix,
        }
        state = _create_work_item(run_root, "ENTITY", sequence, payload)
        entity_ids.append(state["work_item_id"])
    return entity_ids, lock


def _evaluate_deterministic_gates(run_root: Path, card: dict, *, require_empty: bool) -> dict:
    from rdflib import Graph, RDF, OWL, URIRef
    from validate import gate1_rdf_syntax, gate2_owl_consistency, gate3_owl_bundle

    artifacts = run_root / "artifacts"
    syntax = gate1_rdf_syntax(str(run_root / "ontology.owl"))
    owl = gate2_owl_consistency(str(run_root / "ontology.owl")) if syntax.passed else None
    bundle = gate3_owl_bundle(
        run_root,
        artifacts / "dynamic_shapes.ttl",
        card=card,
        expected_dynamic_shapes=_dynamic_shapes(card),
    ) if syntax.passed else None
    errors = []
    errors.extend(f"RDF_SYNTAX: {error}" for error in syntax.errors)
    if owl is not None:
        errors.extend(f"OWL_RL: {error}" for error in owl.errors)
    if bundle is not None:
        errors.extend(f"OUTPUT_CONSTRAINT: {error}" for error in bundle.errors)
    empty_errors = []
    if syntax.passed and require_empty:
        schema_graph = Graph().parse(run_root / "schema.owl", format="xml")
        instances_graph = Graph().parse(run_root / "instances.owl", format="xml")
        combined_graph = Graph().parse(run_root / "ontology.owl", format="xml")
        declaration = (URIRef(card["ontology_iri"]), RDF.type, OWL.Ontology)
        if set(schema_graph) | set(instances_graph) | {declaration} != set(combined_graph):
            empty_errors.append("EMPTY_GRAPH_UNION_INVALID")
        if any(instances_graph) or list(combined_graph.subjects(RDF.type, OWL.NamedIndividual)):
            empty_errors.append("EMPTY_ABOX_NOT_EMPTY")
    errors.extend(empty_errors)
    return {
        "version": RUN_CONTRACT_VERSION,
        "ontology_parseable": syntax.passed,
        "gate_2": "PASS" if syntax.passed and owl is not None and owl.passed else "FAIL",
        "gate_3": "PASS" if bundle is not None and bundle.passed and not empty_errors else "FAIL",
        "errors": errors,
    }


def _prepare_empty_pass(run_root: Path, card: dict) -> dict:
    coverage = {
        "version": RUN_CONTRACT_VERSION,
        "status": "COMPLETE",
        "expected_chunk_ids": [],
        "completed_chunk_ids": [],
        "failed_chunk_ids": [],
    }
    _validate_contract(coverage, "coverage.schema.json")
    resolved = {
        "version": RUN_CONTRACT_VERSION,
        "ontology_iri": card["ontology_iri"],
        "entity_namespace": card["entity_namespace"],
        "schema_card_sha256": _digest_text(_canonical_json(card) + "\n"),
        "individuals": [],
        "assertions": [],
    }
    artifacts = run_root / "artifacts"
    _atomic_text(artifacts / "coverage.json", _canonical_json(coverage) + "\n")
    _atomic_text(artifacts / "failed_chunks.jsonl", "")
    _atomic_text(artifacts / "abox_candidates.json", _canonical_json({"version": 1, "entities": [], "assertions": []}) + "\n")
    _atomic_text(artifacts / "critic_reviews.json", _canonical_json({"version": 1, "reviews": []}) + "\n")
    _atomic_text(artifacts / "resolved_instances.json", _canonical_json(resolved) + "\n")
    _atomic_text(artifacts / "evidence.jsonl", "")
    _atomic_text(artifacts / "rejections.jsonl", "")
    build_owl_files(card, resolved, run_root)
    return {"require_empty": True, "failed_chunk_count": 0}


def _completed_critic_records(run_root: Path, schema_digest: str) -> list[tuple[dict, dict, dict]]:
    records = []
    for item_root in (run_root / "work_items").iterdir():
        if not item_root.is_dir():
            continue
        state, payload = _read_work_item(run_root, item_root.name)
        if (
            state["stage"] != "CANDIDATE_CRITIC"
            or state["status"] != "COMPLETE"
            or payload.get("schema_card_sha256") != schema_digest
        ):
            continue
        result = _read_json_strict(item_root / "reported_result.json", "Candidate Critic final result")
        _validate_critic_result(result, payload)
        if result["status"] != "complete":
            continue
        records.append((state, payload, result))
    return sorted(records, key=lambda item: item[0]["logical_sequence"])


def _prepare_nonempty_pass(run_root: Path, active: dict, card: dict) -> dict:
    schema_digest = _digest_text(_canonical_json(card) + "\n")
    records = _completed_critic_records(run_root, schema_digest)
    expected = _read_json_strict(run_root / "artifacts" / "expected_abox_chunks.json", "expected ABox chunks")
    completed_chunk_ids = [payload["chunk"]["chunk_id"] for _, payload, _ in records]
    failed_records = _failed_chunk_records(run_root, expected["chunk_ids"], schema_digest)
    failed_chunk_ids = [record["chunk_id"] for record in failed_records]
    if (
        set(completed_chunk_ids) | set(failed_chunk_ids) != set(expected["chunk_ids"])
        or set(completed_chunk_ids) & set(failed_chunk_ids)
        or len(completed_chunk_ids) != len(set(completed_chunk_ids))
        or len(failed_chunk_ids) != len(set(failed_chunk_ids))
    ):
        raise PipelineError("Coverage cannot freeze before every expected ABox chunk is terminal")

    entities: list[dict] = []
    assertions: list[dict] = []
    critic_rejections: list[dict] = []
    critic_rows: list[dict] = []
    for _, payload, result in records:
        candidates = {
            ("entity", candidate["candidate_id"]): candidate
            for candidate in payload["entity_result"]["entities"]
        }
        candidates.update(
            {
                ("assertion", candidate["candidate_id"]): candidate
                for candidate in payload["assertion_result"]["assertions"]
            }
        )
        for review in result["reviews"]:
            candidate = candidates[(review["candidate_kind"], review["candidate_id"])]
            if review["disposition"] == "retain":
                target = entities if review["candidate_kind"] == "entity" else assertions
                target.append(candidate)
            elif review["disposition"] == "reject":
                audit = {
                    "review_id": result["review_id"],
                    "reason_code": review["reason_code"],
                    "evidence": review["evidence"],
                }
                if "detail" in review:
                    audit["detail"] = review["detail"]
                critic_rejections.append(
                    {
                        "candidate_id": review["candidate_id"],
                        "candidate_kind": review["candidate_kind"],
                        "reasons": ["CRITIC_" + review["reason_code"]],
                        "evidence": candidate["evidence"],
                        "review": audit,
                    }
                )
        critic_rows.append(
            {
                "chunk_id": payload["chunk"]["chunk_id"],
                "review_id": result["review_id"],
                "result": result,
            }
        )

    aggregate = {"version": RUN_CONTRACT_VERSION, "entities": entities, "assertions": assertions}
    workspace = Path(active["config"]["workspace"])
    sources = [Path(source["path"]) for source in active["config"]["sources"]]
    resolved, evidence, deterministic_rejections = resolve_candidates(card, aggregate, workspace, sources)
    rejections = deterministic_rejections + critic_rejections
    rejections.sort(
        key=lambda row: (row["candidate_kind"], row["candidate_id"], row["reasons"][0], _canonical_json(row))
    )
    for row in rejections:
        _validate_contract(row, "rejection-record.schema.json")

    artifacts = run_root / "artifacts"
    coverage = {
        "version": RUN_CONTRACT_VERSION,
        "status": "INCOMPLETE" if failed_chunk_ids else "COMPLETE",
        "expected_chunk_ids": expected["chunk_ids"],
        "completed_chunk_ids": completed_chunk_ids,
        "failed_chunk_ids": failed_chunk_ids,
    }
    _validate_contract(coverage, "coverage.schema.json")
    _atomic_text(artifacts / "coverage.json", _canonical_json(coverage) + "\n")
    _write_jsonl(artifacts / "failed_chunks.jsonl", failed_records)
    _atomic_text(artifacts / "abox_candidates.json", _canonical_json(aggregate) + "\n")
    _atomic_text(
        artifacts / "critic_reviews.json",
        _canonical_json({"version": RUN_CONTRACT_VERSION, "reviews": critic_rows}) + "\n",
    )
    _atomic_text(artifacts / "resolved_instances.json", _canonical_json(resolved) + "\n")
    _write_jsonl(artifacts / "evidence.jsonl", evidence)
    _write_jsonl(artifacts / "rejections.jsonl", rejections)
    build_owl_files(card, resolved, run_root)
    return {"require_empty": False, "failed_chunk_count": len(failed_chunk_ids)}


def _read_qa_state(run_root: Path) -> dict:
    path = run_root / "artifacts" / "qa_state.json"
    if not path.exists():
        state = {"version": RUN_CONTRACT_VERSION, "round": 1, "seen_findings": []}
        _validate_contract(state, "qa-state.schema.json")
        _atomic_text(path, _canonical_json(state) + "\n")
        return state
    state = _read_json_strict(path, "QA state")
    _validate_contract(state, "qa-state.schema.json")
    return state


def _write_qa_state(run_root: Path, state: dict) -> None:
    _validate_contract(state, "qa-state.schema.json")
    _atomic_text(run_root / "artifacts" / "qa_state.json", _canonical_json(state) + "\n")


def _validate_gate1_result(result: dict, payload: dict) -> None:
    _validate_contract(result, "qa-gate1-output.schema.json")
    if result["round"] != payload["round"]:
        raise PipelineError("QA Gate 1 round does not match its work item")
    if (result["status"] == "PASS") != (not result["findings"]):
        raise PipelineError("QA Gate 1 PASS requires no findings and FAIL requires findings")
    keys = [
        (finding["target"], finding["reason_code"], finding.get("chunk_id", ""))
        for finding in result["findings"]
    ]
    if keys != sorted(keys) or len(keys) != len(set(keys)):
        raise PipelineError("QA Gate 1 findings must be unique and canonically ordered")
    expected_chunks = set(payload["coverage"]["expected_chunk_ids"])
    for finding in result["findings"]:
        if finding["target"] == "ABOX_CHUNK" and finding["chunk_id"] not in expected_chunks:
            raise PipelineError("QA Gate 1 ABox finding must reference an expected terminal chunk")


def _findings_digest(findings: list[dict]) -> str:
    structural = [
        {
            "reason_code": finding["reason_code"],
            "target": finding["target"],
            **({"chunk_id": finding["chunk_id"]} if "chunk_id" in finding else {}),
        }
        for finding in findings
    ]
    return _digest_text(_canonical_json(structural))


def _repair_target(findings: list[dict]) -> str | None:
    targets = {finding["target"] for finding in findings}
    if len(targets) == 1:
        target = next(iter(targets))
        return target if target in {"CQ", "SRD", "SCHEMA_CARD"} else None
    return None


def _current_target(run_root: Path, target: str) -> tuple[object, str]:
    path = {
        "CQ": run_root / "artifacts" / "cqs.md",
        "SRD": run_root / "artifacts" / "srd.md",
        "SCHEMA_CARD": run_root / "artifacts" / "schema_card.json",
    }[target]
    if target == "SCHEMA_CARD":
        value: object = _read_json_strict(path, "Schema Card")
        normalized = _canonical_json(value) + "\n"
    else:
        normalized = path.read_bytes().decode("utf-8", errors="strict")
        value = normalized
    return value, _digest_text(normalized)


def _qa_artifact_digests(run_root: Path) -> dict[str, str]:
    paths = [
        "artifacts/cqs.md", "artifacts/srd.md", "artifacts/schema_card.json",
        "artifacts/coverage.json", "artifacts/resolved_instances.json",
        "artifacts/evidence.jsonl", "artifacts/rejections.jsonl",
        "artifacts/abox_candidates.json", "artifacts/critic_reviews.json",
        "artifacts/failed_chunks.jsonl",
        "artifacts/dynamic_shapes.ttl",
        "schema.owl", "instances.owl", "ontology.owl",
    ]
    result = {}
    for relative in paths:
        content = (run_root / relative).read_bytes().decode("utf-8", errors="strict")
        result[relative] = _digest_text(content)
    return result


def _expected_qa_artifact_digests(payload: dict) -> dict[str, str]:
    expected = dict(payload["artifact_sha256"])
    legacy = "artifacts/aggregate_candidates.json"
    canonical = "artifacts/abox_candidates.json"
    if legacy in expected:
        if canonical in expected:
            raise RunLifecycleError(
                "ledger_corrupt", "QA candidate artifact contract 同时包含新旧路径", 5
            )
        expected[canonical] = expected.pop(legacy)
    return expected


def _canonical_qa_review_bundle(payload: dict) -> dict:
    bundle = dict(payload["review_bundle"])
    legacy = "aggregate_candidates"
    canonical = "abox_candidates"
    if legacy in bundle:
        if canonical in bundle:
            raise RunLifecycleError(
                "ledger_corrupt", "QA review bundle 同时包含新旧 candidate key", 5
            )
        bundle[canonical] = bundle.pop(legacy)
    return bundle


def _qa_review_bundle(run_root: Path, active: dict) -> dict:
    artifacts = run_root / "artifacts"
    return {
        "cqs": (artifacts / "cqs.md").read_text(encoding="utf-8"),
        "srd": (artifacts / "srd.md").read_text(encoding="utf-8"),
        "schema_card": _read_json_strict(artifacts / "schema_card.json", "Schema Card"),
        "coverage": _read_json_strict(artifacts / "coverage.json", "Coverage"),
        "abox_candidates": _read_json_strict(
            artifacts / "abox_candidates.json", "ABox candidates"
        ),
        "critic_reviews": _read_json_strict(artifacts / "critic_reviews.json", "Critic reviews"),
        "resolved_instances": _read_json_strict(
            artifacts / "resolved_instances.json", "resolved instances"
        ),
        "evidence_jsonl": (artifacts / "evidence.jsonl").read_text(encoding="utf-8"),
        "rejections_jsonl": (artifacts / "rejections.jsonl").read_text(encoding="utf-8"),
        "tbox_chunks": _ordered_view_chunks(run_root, active["config"]["sources"], "tbox"),
        "abox_chunks": _ordered_view_chunks(run_root, active["config"]["sources"], "abox"),
    }


def _begin_qa_round(
    paths: dict[str, Path], ledger: dict, active: dict, card: dict, build: dict
) -> dict:
    run_root = paths["staging"] / active["run_id"]
    qa_state = _read_qa_state(run_root)
    round_number = qa_state["round"]
    deterministic = _evaluate_deterministic_gates(
        run_root, card, require_empty=build["require_empty"]
    )
    round_root = run_root / "artifacts" / "qa_rounds" / f"{round_number:02d}"
    _atomic_text(round_root / "deterministic_gates.json", _canonical_json(deterministic) + "\n")
    if not deterministic["ontology_parseable"]:
        return _finalize_qa_and_publish(
            paths, ledger, active, card, round_number=round_number,
            gates={"gate_1": "NOT_RUN", "gate_2": "FAIL", "gate_3": "NOT_RUN"},
            findings=[], qa_reasons=["ONTOLOGY_NOT_PARSEABLE"],
            failed_chunk_count=build["failed_chunk_count"],
        )
    if deterministic["gate_2"] == "FAIL" or deterministic["gate_3"] == "FAIL":
        return _finalize_qa_and_publish(
            paths, ledger, active, card, round_number=round_number,
            gates={
                "gate_1": "NOT_RUN",
                "gate_2": deterministic["gate_2"],
                "gate_3": deterministic["gate_3"],
            },
            findings=[], qa_reasons=["DETERMINISTIC_GATE_FAILURE"],
            failed_chunk_count=build["failed_chunk_count"],
        )
    coverage = _read_json_strict(run_root / "artifacts" / "coverage.json", "Coverage")
    payload = {
        "version": RUN_CONTRACT_VERSION,
        "stage": "QA_GATE_1",
        "logical_sequence": 100 + round_number,
        "round": round_number,
        "schema_card_sha256": _digest_text(_canonical_json(card) + "\n"),
        "coverage": coverage,
        "deterministic_gates": {"gate_2": "PASS", "gate_3": "PASS"},
        "artifact_sha256": _qa_artifact_digests(run_root),
        "review_bundle": _qa_review_bundle(run_root, active),
    }
    state = _create_work_item(run_root, "QA_GATE_1", 100 + round_number, payload)
    active["current_stage"] = "QA_GATE_1"
    active["pending_work"] = [state["work_item_id"]]
    ledger["active_run"] = active
    _integrity_write(paths["ledger"], ledger)
    return {
        "status": "accepted",
        "run_id": active["run_id"],
        "run_state": "ACTIVE",
        "current_stage": "QA_GATE_1",
        "delivery_status": None,
        "pending_work_items": active["pending_work"],
        "pending_work_details": _work_details(run_root, active["pending_work"]),
    }


def _terminal_snapshot_payloads(run_root: Path) -> dict[str, str]:
    payloads: dict[str, str] = {}
    roots = [run_root / "manifests", run_root / "work_items", run_root / "artifacts"]
    files = [path for root in roots for path in root.rglob("*") if path.is_file()]
    delivery = _read_json_strict(run_root / "delivery_status.json", "delivery status")
    files.append(run_root / "delivery_status.json")
    if delivery["delivery_status"] != "FAILED":
        files.extend(run_root / name for name in ("schema.owl", "instances.owl", "ontology.owl"))
    for path in sorted(files, key=lambda item: item.relative_to(run_root).as_posix()):
        relative = path.relative_to(run_root).as_posix()
        try:
            payloads[relative] = path.read_bytes().decode("utf-8", errors="strict")
        except (OSError, UnicodeError) as exc:
            raise RunLifecycleError("persistence_failed", f"terminal artifact 无法读取: {relative}", 5) from exc
    return payloads


def _terminal_intent_artifact_digests(run_root: Path) -> dict[str, str]:
    payloads: dict[str, str] = {}
    roots = [run_root / "manifests", run_root / "work_items", run_root / "artifacts"]
    files = [path for root in roots for path in root.rglob("*") if path.is_file()]
    files.extend(
        run_root / name
        for name in ("schema.owl", "instances.owl", "ontology.owl")
        if (run_root / name).exists()
    )
    for path in sorted(files, key=lambda item: item.relative_to(run_root).as_posix()):
        relative = path.relative_to(run_root).as_posix()
        if relative in {"artifacts/qa_report.json", "delivery_status.json"}:
            continue
        try:
            payloads[relative] = _digest_text(
                path.read_bytes().decode("utf-8", errors="strict")
            )
        except (OSError, UnicodeError) as exc:
            raise RunLifecycleError(
                "ledger_corrupt", f"terminal publication artifact 无法读取: {relative}", 5
            ) from exc
    return payloads


def _publish_payload_snapshot(paths: dict[str, Path], payloads: dict[str, str]) -> str:
    artifacts = [{"path": relative, "sha256": _digest_text(content)} for relative, content in sorted(payloads.items())]
    snapshot_id = _digest_text(_canonical_json(artifacts))
    release_manifest = {"version": RUN_CONTRACT_VERSION, "snapshot_id": snapshot_id, "artifacts": artifacts}
    release_dir = paths["releases"] / snapshot_id
    if release_dir.exists():
        _verify_release_snapshot(paths, snapshot_id)
        existing = json.loads((release_dir / "release_manifest.json").read_text(encoding="utf-8"))
        if existing != release_manifest:
            raise RunLifecycleError("ledger_corrupt", "content-addressed Release Snapshot 冲突", 5)
        return snapshot_id
    paths["releases"].mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(dir=paths["releases"], prefix=".snapshot."))
    try:
        for relative, content in payloads.items():
            _atomic_text(temporary.joinpath(*PurePosixPath(relative).parts), content)
        _atomic_text(temporary / "release_manifest.json", _canonical_json(release_manifest) + "\n")
        os.replace(temporary, release_dir)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    _verify_release_snapshot(paths, snapshot_id)
    return snapshot_id


def _publish_run_completion(paths: dict[str, Path], ledger: dict, active: dict) -> dict:
    run_root = paths["staging"] / active["run_id"]
    _validate_staging(paths, active, active["project_digest"])
    snapshot_id = _publish_payload_snapshot(paths, _terminal_snapshot_payloads(run_root))
    delivery = _read_json_strict(run_root / "delivery_status.json", "delivery status")
    delivery_status = delivery["delivery_status"]
    marker = {
        "version": RUN_CONTRACT_VERSION,
        "run_id": active["run_id"],
        "snapshot_id": snapshot_id,
        "project_digest": active["project_digest"],
        "delivery_status": delivery_status,
    }
    _integrity_write(run_root / "terminal_commit.json", marker)
    return _complete_terminal_publication(
        paths, ledger, active, snapshot_id, delivery_status
    )


def _complete_terminal_publication(
    paths: dict[str, Path],
    ledger: dict,
    active: dict,
    snapshot_id: str,
    delivery_status: str,
) -> dict:
    _verify_release_snapshot(paths, snapshot_id)
    delivery = _read_json_strict(
        paths["releases"] / snapshot_id / "delivery_status.json",
        "terminal Delivery Status",
    )
    if delivery.get("delivery_status") != delivery_status:
        raise RunLifecycleError("ledger_corrupt", "terminal Delivery Status 不一致", 5)
    pointer = {
        "version": RUN_CONTRACT_VERSION,
        "snapshot_id": snapshot_id,
        "run_id": active["run_id"],
        "delivery_status": delivery_status,
    }
    _atomic_text(paths["latest_attempt"], _canonical_json(pointer) + "\n")
    if delivery_status != "FAILED":
        _atomic_text(paths["latest_delivery"], _canonical_json(pointer) + "\n")
    active.update(
        {
            "run_state": "FAILED" if delivery_status == "FAILED" else "COMPLETE",
            "current_stage": "RELEASE_SNAPSHOT",
            "delivery_status": delivery_status,
            "pending_work": [],
            "recent_errors": (
                [{"code": delivery["reason_codes"][0]}] if delivery_status == "FAILED" else []
            ),
        }
    )
    ledger["active_run"] = active
    ledger["latest_attempt"] = snapshot_id
    if delivery_status != "FAILED":
        ledger["latest_delivery"] = snapshot_id
    _integrity_write(paths["ledger"], ledger)
    _release_lock(paths)
    return {
        "status": "accepted",
        "run_id": active["run_id"],
        "snapshot_id": snapshot_id,
        "run_state": active["run_state"],
        "current_stage": "RELEASE_SNAPSHOT",
        "delivery_status": delivery_status,
        "pending_work_items": [],
        "pending_work_details": [],
    }


def _pending_terminal_snapshot(
    paths: dict[str, Path], ledger: dict, project_digest: str
) -> tuple[dict, str, str] | None:
    active = ledger.get("active_run")
    if not isinstance(active, dict) or not isinstance(active.get("run_id"), str):
        return None
    marker_path = paths["staging"] / active["run_id"] / "terminal_commit.json"
    if not marker_path.exists():
        return None
    marker, _ = _integrity_read(marker_path, "terminal commit")
    _require_exact_keys(
        marker,
        {"version", "run_id", "snapshot_id", "project_digest", "delivery_status"},
        "terminal commit",
    )
    if (
        marker["version"] != RUN_CONTRACT_VERSION
        or marker["run_id"] != active["run_id"]
        or marker["project_digest"] != project_digest
        or marker["delivery_status"] not in {"PASS", "FORCED_WITH_ERRORS", "FAILED"}
        or re.fullmatch(r"[0-9a-f]{64}", str(marker["snapshot_id"])) is None
    ):
        raise RunLifecycleError("ledger_corrupt", "terminal commit 与运行账本不一致", 5)
    _verify_release_snapshot(paths, marker["snapshot_id"])
    delivery = _read_json_strict(
        paths["releases"] / marker["snapshot_id"] / "delivery_status.json",
        "terminal Delivery Status",
    )
    if delivery.get("delivery_status") != marker["delivery_status"]:
        raise RunLifecycleError("ledger_corrupt", "terminal commit Delivery Status 不一致", 5)
    return active, marker["snapshot_id"], marker["delivery_status"]


def _recover_terminal_commit(
    paths: dict[str, Path],
    ledger: dict,
    project: dict,
    project_digest: str,
    workspace: Path | None,
    sources: list[Path] | None,
) -> dict | None:
    pending = _pending_terminal_snapshot(paths, ledger, project_digest)
    if pending is None:
        return None
    active, snapshot_id, delivery_status = pending
    if active["run_state"] == "ACTIVE":
        _migrate_term_identity_registry(paths, ledger, project, active)
        _validate_staging(paths, active, project_digest)
        _validate_resume_config(active, workspace, sources)
        _recover_active_lock(paths, active["run_id"])
    return _complete_terminal_publication(
        paths, ledger, active, snapshot_id, delivery_status
    )


def _terminal_intent(run_root: Path, active: dict) -> dict:
    intent, _ = _integrity_read(
        run_root / "terminal_intent.json", "terminal publication intent"
    )
    _require_exact_keys(
        intent,
        {
            "version",
            "run_id",
            "round",
            "gates",
            "findings",
            "qa_reasons",
            "failed_chunk_count",
            "schema_card_sha256",
            "artifact_sha256",
        },
        "terminal publication intent",
    )
    if (
        intent["version"] != RUN_CONTRACT_VERSION
        or intent["run_id"] != active["run_id"]
        or not isinstance(intent["round"], int)
        or intent["round"] < 1
        or not isinstance(intent["gates"], dict)
        or set(intent["gates"]) != {"gate_1", "gate_2", "gate_3"}
        or any(
            value not in {"PASS", "FAIL", "NOT_RUN"}
            for value in intent["gates"].values()
        )
        or not isinstance(intent["findings"], list)
        or not isinstance(intent["qa_reasons"], list)
        or not all(isinstance(reason, str) for reason in intent["qa_reasons"])
        or not isinstance(intent["failed_chunk_count"], int)
        or intent["failed_chunk_count"] < 0
        or re.fullmatch(r"[0-9a-f]{64}", str(intent["schema_card_sha256"]))
        is None
        or not isinstance(intent["artifact_sha256"], dict)
        or not all(
            isinstance(path, str)
            and isinstance(digest, str)
            and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
            for path, digest in intent["artifact_sha256"].items()
        )
    ):
        raise RunLifecycleError(
            "ledger_corrupt", "terminal publication intent 无效", 5
        )
    return intent


def _legacy_terminal_intent(run_root: Path, active: dict) -> dict:
    qa_state = _read_qa_state(run_root)
    round_number = qa_state["round"]
    round_root = run_root / "artifacts" / "qa_rounds" / f"{round_number:02d}"
    deterministic = _read_json_strict(
        round_root / "deterministic_gates.json", "deterministic QA gates"
    )
    coverage = _read_json_strict(run_root / "artifacts" / "coverage.json", "Coverage")
    failed_chunk_count = len(coverage["failed_chunk_ids"])
    card = _read_json_strict(run_root / "artifacts" / "schema_card.json", "Schema Card")
    gate1_path = round_root / "gate1.json"
    gate1 = _read_json_strict(gate1_path, "QA Gate 1 result") if gate1_path.exists() else None
    if not deterministic["ontology_parseable"]:
        gates = {"gate_1": "NOT_RUN", "gate_2": "FAIL", "gate_3": "NOT_RUN"}
        findings: list[dict] = []
        qa_reasons = ["ONTOLOGY_NOT_PARSEABLE"]
    elif deterministic["gate_2"] == "FAIL" or deterministic["gate_3"] == "FAIL":
        gates = {
            "gate_1": "NOT_RUN",
            "gate_2": deterministic["gate_2"],
            "gate_3": deterministic["gate_3"],
        }
        findings = []
        qa_reasons = ["DETERMINISTIC_GATE_FAILURE"]
    elif gate1 is None:
        gates = {"gate_1": "NOT_RUN", "gate_2": "NOT_RUN", "gate_3": "NOT_RUN"}
        findings = []
        qa_reasons = ["STATE_LEDGER_INVALID"]
    else:
        findings = gate1["findings"]
        gates = {"gate_1": gate1["status"], "gate_2": "PASS", "gate_3": "PASS"}
        if gate1["status"] == "PASS":
            qa_reasons = []
        else:
            findings_digest = _findings_digest(findings)
            fixer_sequence = 200 + round_number
            has_completed_fixer = False
            for item_root in (run_root / "work_items").iterdir():
                if not item_root.is_dir():
                    continue
                state, payload = _read_work_item(run_root, item_root.name)
                if (
                    state["stage"] == "FIXER"
                    and state["logical_sequence"] == fixer_sequence
                    and state["status"] == "COMPLETE"
                    and payload.get("round") == round_number
                ):
                    has_completed_fixer = True
                    break
            if has_completed_fixer:
                qa_reasons = ["FIXER_NO_CHANGE"]
            elif failed_chunk_count:
                qa_reasons = ["INCOMPLETE_COVERAGE_NO_REPAIR"]
            elif _repair_target(findings) is None:
                qa_reasons = ["NO_VALID_REPAIR_TARGET"]
            elif findings_digest in qa_state["seen_findings"]:
                qa_reasons = ["REPEATED_FINDINGS"]
            elif round_number >= 20:
                qa_reasons = ["ROUND_LIMIT"]
            else:
                raise RunLifecycleError(
                    "ledger_corrupt", "legacy terminal publication intent 无法从 QA state 推导", 5
                )
    return {
        "version": RUN_CONTRACT_VERSION,
        "run_id": active["run_id"],
        "round": round_number,
        "gates": gates,
        "findings": findings,
        "qa_reasons": qa_reasons,
        "failed_chunk_count": failed_chunk_count,
        "schema_card_sha256": _digest_text(_canonical_json(card) + "\n"),
        "artifact_sha256": _terminal_intent_artifact_digests(run_root),
    }


def _materialize_terminal_intent(run_root: Path, intent: dict) -> None:
    current_artifacts = _terminal_intent_artifact_digests(run_root)
    if current_artifacts != intent["artifact_sha256"]:
        raise RunLifecycleError(
            "ledger_corrupt", "terminal publication artifact 摘要已漂移", 5
        )
    card = _read_json_strict(
        run_root / "artifacts" / "schema_card.json", "Schema Card"
    )
    if (
        _digest_text(_canonical_json(card) + "\n")
        != intent["schema_card_sha256"]
    ):
        raise RunLifecycleError(
            "ledger_corrupt", "terminal publication intent Schema Card 不一致", 5
        )
    round_number = intent["round"]
    gates = intent["gates"]
    findings = intent["findings"]
    qa_reasons = intent["qa_reasons"]
    failed_chunk_count = intent["failed_chunk_count"]
    coverage_status = "INCOMPLETE" if failed_chunk_count else "COMPLETE"
    qa_pass = all(value == "PASS" for value in gates.values())
    qa_report = {
        "version": RUN_CONTRACT_VERSION,
        "evaluation_status": "COMPLETE",
        "coverage": coverage_status,
        "status": "PASS" if qa_pass else "FAIL",
        "round": round_number,
        "gates": gates,
        "reason_codes": sorted(set(qa_reasons)),
        "gate_1_findings": findings,
    }
    _validate_contract(qa_report, "qa-report.schema.json")
    _atomic_text(
        run_root / "artifacts" / "qa_report.json",
        _canonical_json(qa_report) + "\n",
    )
    ontology_parseable = (
        gates["gate_2"] != "NOT_RUN" or gates["gate_3"] != "NOT_RUN"
    )
    if {"ONTOLOGY_NOT_PARSEABLE", "STATE_LEDGER_INVALID"} & set(qa_reasons):
        ontology_parseable = False
    if not ontology_parseable:
        delivery_value = "FAILED"
        delivery_reasons = [
            (
                "STATE_LEDGER_INVALID"
                if "STATE_LEDGER_INVALID" in qa_reasons
                else "ONTOLOGY_NOT_PARSEABLE"
            )
        ]
        ontology_iri = ontology_path = ontology_sha256 = None
    else:
        delivery_value = (
            "PASS"
            if qa_pass and coverage_status == "COMPLETE"
            else "FORCED_WITH_ERRORS"
        )
        delivery_reasons = []
        if failed_chunk_count:
            delivery_reasons.append("FAILED_CHUNKS_PRESENT")
        if not qa_pass:
            delivery_reasons.append("QA_REPAIR_BUDGET_EXHAUSTED")
        ontology_content = (run_root / "ontology.owl").read_bytes().decode(
            "utf-8", errors="strict"
        )
        ontology_iri = card["ontology_iri"]
        ontology_path = "ontology.owl"
        ontology_sha256 = _digest_text(ontology_content)
    delivery_status = {
        "version": RUN_CONTRACT_VERSION,
        "delivery_status": delivery_value,
        "coverage": coverage_status,
        "reason_codes": delivery_reasons,
        "failed_chunk_count": failed_chunk_count,
        "qa_rounds": round_number,
        "ontology_iri": ontology_iri,
        "ontology_path": ontology_path,
        "ontology_sha256": ontology_sha256,
    }
    _validate_contract(delivery_status, "delivery-status.schema.json")
    _atomic_text(
        run_root / "delivery_status.json",
        _canonical_json(delivery_status) + "\n",
    )


def _finalize_qa_and_publish(
    paths: dict[str, Path],
    ledger: dict,
    active: dict,
    card: dict,
    *,
    round_number: int,
    gates: dict[str, str],
    findings: list[dict],
    qa_reasons: list[str],
    failed_chunk_count: int,
) -> dict:
    run_root = paths["staging"] / active["run_id"]
    intent = {
        "version": RUN_CONTRACT_VERSION,
        "run_id": active["run_id"],
        "round": round_number,
        "gates": gates,
        "findings": findings,
        "qa_reasons": qa_reasons,
        "failed_chunk_count": failed_chunk_count,
        "schema_card_sha256": _digest_text(_canonical_json(card) + "\n"),
        "artifact_sha256": _terminal_intent_artifact_digests(run_root),
    }
    _integrity_write(run_root / "terminal_intent.json", intent)
    active["current_stage"] = "ORCHESTRATION"
    active["pending_work"] = ["orchestration"]
    ledger["active_run"] = active
    _integrity_write(paths["ledger"], ledger)
    _materialize_terminal_intent(run_root, intent)
    return _publish_run_completion(paths, ledger, active)


def _complete_empty_pass(paths: dict[str, Path], ledger: dict, active: dict, card: dict) -> dict:
    build = _prepare_empty_pass(paths["staging"] / active["run_id"], card)
    return _begin_qa_round(paths, ledger, active, card, build)


def _complete_nonempty_pass(paths: dict[str, Path], ledger: dict, active: dict, card: dict) -> dict:
    build = _prepare_nonempty_pass(paths["staging"] / active["run_id"], active, card)
    return _begin_qa_round(paths, ledger, active, card, build)


def _primary_evidence_offset(evidence: dict, chunk: dict) -> int:
    primary = chunk["primary"]
    if evidence["source"] != chunk["source_path"]:
        raise PipelineError("evidence.source does not match the ABox chunk")
    start, end = evidence["line_start"], evidence["line_end"]
    if start < primary["line_start"] or end > primary["line_end"] or end < start:
        raise PipelineError("evidence line range is outside primary")
    matching_segments = [
        segment
        for segment in primary["segments"]
        if segment["line_start"] <= start <= segment["line_end"]
        and segment["heading_path"] == evidence["heading_path"]
    ]
    if not matching_segments:
        raise PipelineError("evidence heading_path is not valid for the primary line")
    lines = primary["text"].splitlines(keepends=True)
    relative_start = start - primary["line_start"]
    relative_end = end - primary["line_start"] + 1
    excerpt = "".join(lines[relative_start:relative_end])
    quote_offset = excerpt.find(evidence["quote"])
    if quote_offset < 0:
        raise PipelineError("evidence.quote is not a verbatim primary substring")
    return len("".join(lines[:relative_start])) + quote_offset


def _validate_entity_result(result: dict, payload: dict) -> None:
    _validate_contract(result, "entity-pass-output.schema.json")
    chunk = payload["chunk"]
    card = payload["schema_card"]
    prefix = payload["candidate_id_prefix"]
    if result["chunk_id"] != chunk["chunk_id"]:
        raise PipelineError("Entity result chunk_id mismatch")
    if result["status"] == "retryable_failure":
        return
    index = validate_schema_card(card)
    entity_ids: set[str] = set()
    last_offset = -1
    for ordinal, entity in enumerate(result["entities"], start=1):
        expected_id = f"{prefix}.entity.{ordinal:03d}"
        if entity["candidate_id"] != expected_id:
            raise PipelineError("Candidate Entity IDs must be sequential and use the supplied prefix")
        if entity["class_iri"] not in index["classes"]:
            raise PipelineError("Candidate Entity class_iri is outside the locked Schema Card")
        offset = _primary_evidence_offset(entity["evidence"], chunk)
        if entity["name"] not in entity["evidence"]["quote"]:
            raise PipelineError("Candidate Entity name is not verbatim in its evidence quote")
        offset += entity["evidence"]["quote"].find(entity["name"])
        if offset < last_offset:
            raise PipelineError("Candidate Entities are not in primary textual order")
        last_offset = offset
        identifier = entity["business_identifier"]
        if identifier is not None:
            prop = index["datatype_properties"].get(identifier["property_iri"])
            if (
                prop is None
                or prop["identity"] is not True
                or not _is_subclass(entity["class_iri"], prop["domain"], index["parents"])
                or not _literal_is_valid(identifier["value"], prop["range"])
                or identifier["value"] not in entity["evidence"]["quote"]
            ):
                raise PipelineError("business_identifier is not grounded in one valid identity-property quote")
        entity_ids.add(entity["candidate_id"])

    last_offset = -1
    for ordinal, ambiguity in enumerate(result["ambiguities"], start=1):
        if ambiguity["ambiguity_id"] != f"{prefix}.ambiguity.{ordinal:03d}":
            raise PipelineError("ambiguity IDs must be sequential and use the supplied prefix")
        offset = _primary_evidence_offset(ambiguity["evidence"], chunk)
        if ambiguity["mention"] not in ambiguity["evidence"]["quote"]:
            raise PipelineError("ambiguity mention is not verbatim in its evidence quote")
        offset += ambiguity["evidence"]["quote"].find(ambiguity["mention"])
        if offset < last_offset:
            raise PipelineError("ambiguities are not in primary textual order")
        last_offset = offset
        if ambiguity["field"] in {"name", "class_iri"} and ambiguity["candidate_id"] is not None:
            raise PipelineError("name/class ambiguity cannot reference a Candidate Entity")
        if ambiguity["field"] == "business_identifier" and ambiguity["candidate_id"] not in entity_ids:
            raise PipelineError("business_identifier ambiguity must reference a Candidate Entity")
        if ambiguity["field"] == "class_iri" and any(value not in index["classes"] for value in ambiguity["alternatives"]):
            raise PipelineError("class ambiguity alternatives must come from the locked Schema Card")


def _semantic_result_paths(run_root: Path, state: dict, result: dict) -> tuple[Path, Path]:
    work_root = run_root / "work_items" / state["work_item_id"]
    reported = work_root / "reported_result.json"
    filename = {
        "ENTITY": "entity_result.json",
        "ASSERTION": "assertion_result.json",
        "CANDIDATE_CRITIC": "critic_result.json",
    }.get(state["stage"])
    if filename is None:
        raise RunLifecycleError("ledger_corrupt", "semantic result stage 无效", 5)
    payload = _read_json_strict(work_root / "input.json", "semantic work item input")
    schema_digest = payload.get("schema_card_sha256")
    if not isinstance(schema_digest, str) or re.fullmatch(r"[0-9a-f]{64}", schema_digest) is None:
        raise RunLifecycleError("ledger_corrupt", "semantic result schema generation 无效", 5)
    if state["stage"] == "CANDIDATE_CRITIC":
        chunk_id = payload.get("chunk", {}).get("chunk_id")
        if not isinstance(chunk_id, str):
            raise RunLifecycleError("ledger_corrupt", "Candidate Critic chunk identity 无效", 5)
    else:
        chunk_id = result["chunk_id"]
    chunk_digest = _digest_text(chunk_id)
    final = run_root / "artifacts" / "schema_generations" / schema_digest / "chunks" / chunk_digest / filename
    return reported, final


def _validate_assertion_result(result: dict, payload: dict) -> None:
    _validate_contract(result, "assertion-pass-output.schema.json")
    chunk = payload["chunk"]
    card = payload["schema_card"]
    prefix = payload["candidate_id_prefix"]
    if result["chunk_id"] != chunk["chunk_id"]:
        raise PipelineError("Assertion result chunk_id mismatch")
    if result["status"] == "retryable_failure":
        return

    index = validate_schema_card(card)
    entities = {entity["candidate_id"]: entity for entity in payload["entity_result"]["entities"]}
    last_offset = -1
    for ordinal, assertion in enumerate(result["assertions"], start=1):
        if assertion["candidate_id"] != f"{prefix}.assertion.{ordinal:03d}":
            raise PipelineError("Candidate Assertion IDs must be sequential and use the supplied prefix")
        subject = entities.get(assertion["subject_candidate_id"])
        if subject is None:
            raise PipelineError("Candidate Assertion subject must reference one locked Candidate Entity")
        offset = _primary_evidence_offset(assertion["evidence"], chunk)
        quote = assertion["evidence"]["quote"]
        if subject["name"] not in quote:
            raise PipelineError("Candidate Assertion evidence does not identify its locked subject")
        if offset < last_offset:
            raise PipelineError("Candidate Assertions are not in primary textual order")
        last_offset = offset

        if assertion["kind"] == "object":
            prop = index["object_properties"].get(assertion["property_iri"])
            target = entities.get(assertion["object_candidate_id"])
            if prop is None:
                raise PipelineError("Object Candidate Assertion property kind is outside the locked Schema Card")
            if target is None:
                raise PipelineError("Object Candidate Assertion target must reference one locked Candidate Entity")
            if not _is_subclass(subject["class_iri"], prop["domain"], index["parents"]):
                raise PipelineError("Object Candidate Assertion subject is outside the property domain")
            if not _is_subclass(target["class_iri"], prop["range"], index["parents"]):
                raise PipelineError("Object Candidate Assertion target is outside the property range")
            if target["name"] not in quote:
                raise PipelineError("Object Candidate Assertion evidence does not identify its locked target")
        else:
            prop = index["datatype_properties"].get(assertion["property_iri"])
            if prop is None:
                raise PipelineError("Data Candidate Assertion property kind is outside the locked Schema Card")
            if not _is_subclass(subject["class_iri"], prop["domain"], index["parents"]):
                raise PipelineError("Data Candidate Assertion subject is outside the property domain")
            if prop["identity"]:
                raise PipelineError("Assertion pass cannot extract or rescue identity property values")
            if assertion["datatype"] != prop["range"]:
                raise PipelineError("Data Candidate Assertion datatype must equal the Schema Card range")
            if assertion["value"] not in quote or not _literal_is_valid(assertion["value"], prop["range"]):
                raise PipelineError("Data Candidate Assertion value must be a verbatim valid XSD lexical form")

    all_properties = {**index["object_properties"], **index["datatype_properties"]}
    last_offset = -1
    for ordinal, exclusion in enumerate(result["exclusions"], start=1):
        if exclusion["exclusion_id"] != f"{prefix}.exclusion.{ordinal:03d}":
            raise PipelineError("Assertion exclusion IDs must be sequential and use the supplied prefix")
        offset = _primary_evidence_offset(exclusion["evidence"], chunk)
        if offset < last_offset:
            raise PipelineError("Assertion exclusions are not in primary textual order")
        last_offset = offset
        if any(candidate_id not in entities for candidate_id in exclusion["candidate_ids"]):
            raise PipelineError("Assertion exclusion references an unlocked Candidate Entity")
        property_iri = exclusion["property_iri"]
        if property_iri is not None and property_iri not in all_properties:
            raise PipelineError("Assertion exclusion property is outside the locked Schema Card")
        if exclusion["reason"] == "PROPERTY_AMBIGUOUS" and property_iri is not None:
            raise PipelineError("PROPERTY_AMBIGUOUS exclusion cannot select a property")
        if exclusion["reason"] == "LITERAL_NOT_SCHEMA_TYPED" and property_iri not in index["datatype_properties"]:
            raise PipelineError("LITERAL_NOT_SCHEMA_TYPED exclusion must identify a DatatypeProperty")


def _validate_critic_result(result: dict, payload: dict) -> None:
    _validate_contract(result, "candidate-critic-output.schema.json")
    if result["review_id"] != payload["review_id"]:
        raise PipelineError("Candidate Critic review_id mismatch")
    chunk = payload["chunk"]
    expected = sorted(
        [
            ("entity", candidate["candidate_id"])
            for candidate in payload["entity_result"]["entities"]
        ]
        + [
            ("assertion", candidate["candidate_id"])
            for candidate in payload["assertion_result"]["assertions"]
        ],
        key=lambda item: (0 if item[0] == "entity" else 1, item[1]),
    )
    actual = [(review["candidate_kind"], review["candidate_id"]) for review in result["reviews"]]
    if actual != expected:
        raise PipelineError("Candidate Critic must review every candidate exactly once in fixed order")

    has_request = False
    for review in result["reviews"]:
        disposition = review["disposition"]
        if disposition == "retain":
            continue
        _primary_evidence_offset(review["evidence"], chunk)
        reason = review["reason_code"]
        kind = review["candidate_kind"]
        if disposition == "reject":
            if reason == "NEGATED_ASSERTION" and kind != "assertion":
                raise PipelineError("NEGATED_ASSERTION cannot reject a Candidate Entity")
            continue
        has_request = True
        if reason.startswith("ENTITY_") and kind != "entity":
            raise PipelineError("Entity re-extraction reason must target a Candidate Entity")
        if reason.startswith("ASSERTION_") and kind != "assertion":
            raise PipelineError("Assertion re-extraction reason must target a Candidate Assertion")

    batch_sort_keys = []
    for request in result["batch_reextraction_requests"]:
        offset = _primary_evidence_offset(request["evidence"], chunk)
        expected_reason = "ENTITY_OMITTED" if request["target_pass"] == "entity" else "ASSERTION_OMITTED"
        if request["reason_code"] != expected_reason:
            raise PipelineError("batch omission reason does not match target_pass")
        batch_sort_keys.append((0 if request["target_pass"] == "entity" else 1, offset))
        has_request = True
    if batch_sort_keys != sorted(batch_sort_keys):
        raise PipelineError("batch re-extraction requests are not in fixed order")
    expected_status = "request_reextraction" if has_request else "complete"
    if result["status"] != expected_status:
        raise PipelineError("Candidate Critic status does not match its dispositions")


def _work_item_results(run_root: Path) -> list[dict]:
    rows = []
    work_root = run_root / "work_items"
    if not work_root.exists():
        return rows
    for item_root in sorted(path for path in work_root.iterdir() if path.is_dir()):
        reported = item_root / "reported_result.json"
        if not reported.exists():
            continue
        state, _ = _read_work_item(run_root, item_root.name)
        result = _read_json_strict(reported, "reported work result")
        rows.append(
            {
                "work_item_id": state["work_item_id"],
                "stage": state["stage"],
                "work_status": state["status"],
                "result": result,
            }
        )
    return rows


def _attempt_count(run_root: Path, state: dict) -> int:
    attempts_root = run_root / "work_items" / state["work_item_id"] / "attempts"
    return len([path for path in attempts_root.iterdir() if path.is_dir()]) if attempts_root.exists() else 0


def _discard_reported_result(run_root: Path, state: dict) -> None:
    try:
        (run_root / "work_items" / state["work_item_id"] / "reported_result.json").unlink()
    except FileNotFoundError:
        pass


def _execution_failure_code(value: object) -> tuple[str, str] | None:
    if not isinstance(value, dict) or set(value) != {"version", "status", "code", "detail"}:
        return None
    if (
        value.get("version") != RUN_CONTRACT_VERSION
        or value.get("status") != "execution_failure"
        or value.get("code") not in {"NO_RESPONSE", "TIMEOUT", "TOOL_INTERRUPTED"}
        or not isinstance(value.get("detail"), str)
        or not value["detail"]
    ):
        return None
    return value["code"], value["detail"]


def _chunk_attempt_audit(
    run_root: Path, chunk_id: str, schema_digest: str
) -> tuple[list[str], dict, dict]:
    attempts = []
    execution = {"ENTITY": 0, "ASSERTION": 0, "CRITIC": 0}
    semantic = {"ENTITY": 0, "ASSERTION": 0}
    stage_names = {"ENTITY": "ENTITY", "ASSERTION": "ASSERTION", "CANDIDATE_CRITIC": "CRITIC"}
    for item_root in (run_root / "work_items").iterdir():
        if not item_root.is_dir():
            continue
        state, payload = _read_work_item(run_root, item_root.name)
        if (
            payload.get("chunk", {}).get("chunk_id") != chunk_id
            or payload.get("schema_card_sha256") != schema_digest
            or state["stage"] not in stage_names
        ):
            continue
        stage = stage_names[state["stage"]]
        if payload.get("invocation_kind") == "CRITIC_REEXTRACTION" and stage in semantic:
            semantic[stage] = 1
        attempts_root = item_root / "attempts"
        if not attempts_root.exists():
            continue
        for attempt_root in attempts_root.iterdir():
            if not attempt_root.is_dir():
                continue
            attempt = _read_json_strict(attempt_root / "attempt.json", "work item attempt")
            attempts.append(
                (
                    attempt["invocation_sequence"],
                    {"ENTITY": 0, "ASSERTION": 1, "CRITIC": 2}[stage],
                    attempt["execution_attempt"],
                    attempt["attempt_id"],
                )
            )
            execution[stage] += 1
    attempts.sort()
    return [item[3] for item in attempts], execution, semantic


def _record_failed_chunk(
    run_root: Path,
    state: dict,
    payload: dict,
    reason_code: str,
    reported_code: str | None,
) -> dict:
    chunk = payload["chunk"]
    chunk_id = chunk["chunk_id"]
    schema_digest = payload["schema_card_sha256"]
    attempt_ids, execution, semantic = _chunk_attempt_audit(run_root, chunk_id, schema_digest)
    record = {
        "version": RUN_CONTRACT_VERSION,
        "chunk_id": chunk_id,
        "source_path": chunk["source_path"],
        "chunk_ordinal": chunk["ordinal"],
        "failed_stage": "CRITIC" if state["stage"] == "CANDIDATE_CRITIC" else state["stage"],
        "reason_code": reason_code,
        "reported_code": reported_code,
        "counters": {
            "execution_attempts": execution,
            "semantic_reextractions": semantic,
        },
        "attempt_ids": attempt_ids,
    }
    _validate_contract(record, "failed-chunk.schema.json")
    failure_path = (
        run_root / "artifacts" / "schema_generations" / schema_digest
        / "chunks" / _digest_text(chunk_id) / "failure.json"
    )
    if failure_path.exists():
        raise RunLifecycleError("ledger_corrupt", "Failed Chunk 已经是不可变终态", 5)
    _atomic_text(failure_path, _canonical_json(record) + "\n")
    return record


def _failed_chunk_records(run_root: Path, expected_ids: list[str], schema_digest: str) -> list[dict]:
    records = []
    for chunk_id in expected_ids:
        path = (
            run_root / "artifacts" / "schema_generations" / schema_digest
            / "chunks" / _digest_text(chunk_id) / "failure.json"
        )
        if not path.exists():
            continue
        record = _read_json_strict(path, "Failed Chunk")
        _validate_contract(record, "failed-chunk.schema.json")
        if record["chunk_id"] != chunk_id:
            raise RunLifecycleError("ledger_corrupt", "Failed Chunk identity 不一致", 5)
        records.append(record)
    return records


def _prior_complete_output_digest(run_root: Path, state: dict, payload: dict) -> str | None:
    candidates = []
    chunk_id = payload["chunk"]["chunk_id"]
    for item_root in (run_root / "work_items").iterdir():
        if not item_root.is_dir() or item_root.name == state["work_item_id"]:
            continue
        other_state, other_payload = _read_work_item(run_root, item_root.name)
        if (
            other_state["stage"] == state["stage"]
            and other_state["status"] == "COMPLETE"
            and other_payload.get("chunk", {}).get("chunk_id") == chunk_id
            and other_payload.get("schema_card_sha256") == payload.get("schema_card_sha256")
            and other_payload.get("invocation_sequence", 1) < payload.get("invocation_sequence", 1)
        ):
            reported = item_root / "reported_result.json"
            if reported.exists() and _read_json_strict(reported, "prior result").get("status") == "complete":
                candidates.append((other_payload.get("invocation_sequence", 1), other_state["output_digest"]))
    return max(candidates)[1] if candidates else None


def _semantic_reextraction_used(
    run_root: Path, chunk_id: str, target: str, schema_digest: str
) -> bool:
    expected_stage = "ENTITY" if target == "entity" else "ASSERTION"
    for item_root in (run_root / "work_items").iterdir():
        if not item_root.is_dir():
            continue
        state, payload = _read_work_item(run_root, item_root.name)
        if (
            state["stage"] == expected_stage
            and payload.get("chunk", {}).get("chunk_id") == chunk_id
            and payload.get("schema_card_sha256") == schema_digest
            and payload.get("invocation_kind") == "CRITIC_REEXTRACTION"
        ):
            return True
    return False


def _initial_assertion_work_items(run_root: Path, schema_digest: str) -> list[str]:
    expected = _read_json_strict(
        run_root / "artifacts" / "expected_abox_chunks.json", "expected ABox chunks"
    )
    failed = {
        record["chunk_id"]
        for record in _failed_chunk_records(run_root, expected["chunk_ids"], schema_digest)
    }
    completed: dict[str, tuple[dict, dict]] = {}
    for item_root in (run_root / "work_items").iterdir():
        if not item_root.is_dir():
            continue
        state, payload = _read_work_item(run_root, item_root.name)
        if (
            state["stage"] != "ENTITY"
            or state["status"] != "COMPLETE"
            or payload.get("invocation_kind") != "INITIAL"
            or payload.get("schema_card_sha256") != schema_digest
        ):
            continue
        chunk_id = payload.get("chunk", {}).get("chunk_id")
        if not isinstance(chunk_id, str) or chunk_id in completed:
            raise RunLifecycleError(
                "ledger_corrupt", "Initial Entity pass terminal state 不唯一", 5
            )
        result = _read_json_strict(
            item_root / "reported_result.json", "Initial Entity pass result"
        )
        _validate_entity_result(result, payload)
        if result["status"] != "complete":
            raise RunLifecycleError(
                "ledger_corrupt", "Initial Entity pass terminal result 不完整", 5
            )
        completed[chunk_id] = (payload, result)
    if set(completed) | failed != set(expected["chunk_ids"]) or set(completed) & failed:
        raise RunLifecycleError(
            "ledger_corrupt",
            "Assertion pass 不能在全部 Initial Entity chunks 终态前开始",
            5,
        )

    assertion_ids = []
    for chunk_id in expected["chunk_ids"]:
        if chunk_id in failed:
            continue
        payload, result = completed[chunk_id]
        assertion_payload = {
            "version": RUN_CONTRACT_VERSION,
            "stage": "ASSERTION",
            "logical_sequence": payload["logical_sequence"],
            "invocation_kind": "INITIAL",
            "invocation_sequence": payload.get("invocation_sequence", 1),
            "schema_card_sha256": payload["schema_card_sha256"],
            "schema_card": payload["schema_card"],
            "chunk": payload["chunk"],
            "candidate_id_prefix": payload["candidate_id_prefix"],
            "entity_result": result,
        }
        assertion_state = _create_work_item(
            run_root, "ASSERTION", payload["logical_sequence"], assertion_payload
        )
        assertion_ids.append(assertion_state["work_item_id"])
    return assertion_ids


def _critic_request_target(result: dict) -> str | None:
    targets = {
        review["candidate_kind"]
        for review in result["reviews"]
        if review["disposition"] == "request_reextraction"
    }
    targets.update(request["target_pass"] for request in result["batch_reextraction_requests"])
    if "entity" in targets:
        return "entity"
    return "assertion" if "assertion" in targets else None


def _terminal_chunk_failure(
    paths: dict[str, Path],
    ledger: dict,
    active: dict,
    state: dict,
    payload: dict,
    output_digest: str,
    reason_code: str,
    reported_code: str | None = None,
) -> dict:
    run_root = paths["staging"] / active["run_id"]
    _fail_work_item(run_root, state, output_digest)
    _record_failed_chunk(run_root, state, payload, reason_code, reported_code)
    next_ids = [work_id for work_id in active["pending_work"] if work_id != state["work_item_id"]]
    if (
        not next_ids
        and state["stage"] == "ENTITY"
        and payload.get("invocation_kind") == "INITIAL"
    ):
        next_ids = _initial_assertion_work_items(
            run_root, payload["schema_card_sha256"]
        )
    if not next_ids:
        return _complete_nonempty_pass(paths, ledger, active, payload["schema_card"])
    active["current_stage"] = "ABOX_WORK"
    active["pending_work"] = next_ids
    ledger["active_run"] = active
    _integrity_write(paths["ledger"], ledger)
    return {
        "status": "accepted",
        "run_id": active["run_id"],
        "run_state": "ACTIVE",
        "current_stage": "ABOX_WORK",
        "delivery_status": None,
        "pending_work_items": next_ids,
        "pending_work_details": _work_details(run_root, next_ids),
        "failed_chunk_id": payload["chunk"]["chunk_id"],
        "failure_code": reason_code,
    }


def _accept_gate1_submission(
    paths: dict[str, Path],
    ledger: dict,
    active: dict,
    state: dict,
    payload: dict,
    result: dict,
    normalized: str,
    output_digest: str,
) -> dict:
    run_root = paths["staging"] / active["run_id"]
    try:
        artifacts_match = _qa_artifact_digests(
            run_root
        ) == _expected_qa_artifact_digests(payload)
    except (OSError, UnicodeError):
        artifacts_match = False
    if not artifacts_match:
        _atomic_text(run_root / "work_items" / state["work_item_id"] / "reported_result.json", normalized)
        _complete_work_item(run_root, state, output_digest)
        card = _read_json_strict(run_root / "artifacts" / "schema_card.json", "Schema Card")
        return _finalize_qa_and_publish(
            paths, ledger, active, card, round_number=payload["round"],
            gates={"gate_1": "NOT_RUN", "gate_2": "NOT_RUN", "gate_3": "NOT_RUN"},
            findings=[], qa_reasons=["STATE_LEDGER_INVALID"],
            failed_chunk_count=len(payload["coverage"]["failed_chunk_ids"]),
        )
    try:
        _validate_gate1_result(result, payload)
    except PipelineError as exc:
        raise RunLifecycleError("invalid_submission", str(exc), 4) from exc
    _atomic_text(run_root / "work_items" / state["work_item_id"] / "reported_result.json", normalized)
    _atomic_text(
        run_root / "artifacts" / "qa_rounds" / f"{result['round']:02d}" / "gate1.json",
        normalized,
    )
    _complete_work_item(run_root, state, output_digest)
    failed_chunk_count = len(payload["coverage"]["failed_chunk_ids"])
    card = _read_json_strict(run_root / "artifacts" / "schema_card.json", "Schema Card")
    gates = {"gate_1": result["status"], **payload["deterministic_gates"]}
    if result["status"] == "PASS":
        return _finalize_qa_and_publish(
            paths, ledger, active, card, round_number=result["round"], gates=gates,
            findings=[], qa_reasons=[], failed_chunk_count=failed_chunk_count,
        )

    qa_state = _read_qa_state(run_root)
    findings_digest = _findings_digest(result["findings"])
    target = _repair_target(result["findings"])
    stop_reason = None
    if failed_chunk_count:
        stop_reason = "INCOMPLETE_COVERAGE_NO_REPAIR"
    elif target is None:
        stop_reason = "NO_VALID_REPAIR_TARGET"
    elif findings_digest in qa_state["seen_findings"]:
        stop_reason = "REPEATED_FINDINGS"
    elif result["round"] >= 20:
        stop_reason = "ROUND_LIMIT"
    if stop_reason is not None:
        return _finalize_qa_and_publish(
            paths, ledger, active, card, round_number=result["round"], gates=gates,
            findings=result["findings"], qa_reasons=[stop_reason],
            failed_chunk_count=failed_chunk_count,
        )

    qa_state["seen_findings"].append(findings_digest)
    qa_state["seen_findings"].sort()
    _write_qa_state(run_root, qa_state)
    current, current_digest = _current_target(run_root, target)
    fixer_payload = {
        "version": RUN_CONTRACT_VERSION,
        "stage": "FIXER",
        "logical_sequence": 200 + result["round"],
        "round": result["round"],
        "target": target,
        "findings": result["findings"],
        "findings_digest": findings_digest,
        "current": current,
        "current_sha256": current_digest,
        "schema_card_sha256": payload["schema_card_sha256"],
        "review_bundle": _canonical_qa_review_bundle(payload),
    }
    fixer_state = _create_work_item(run_root, "FIXER", 200 + result["round"], fixer_payload)
    active["current_stage"] = "FIXER"
    active["pending_work"] = [fixer_state["work_item_id"]]
    ledger["active_run"] = active
    _integrity_write(paths["ledger"], ledger)
    return {
        "status": "accepted",
        "run_id": active["run_id"],
        "run_state": "ACTIVE",
        "current_stage": "FIXER",
        "delivery_status": None,
        "pending_work_items": active["pending_work"],
        "pending_work_details": _work_details(run_root, active["pending_work"]),
        "work_result": result,
        "output_digest": output_digest,
    }


def _accept_fixer_submission(
    paths: dict[str, Path],
    project: dict,
    ledger: dict,
    active: dict,
    state: dict,
    payload: dict,
    result: dict,
    normalized: str,
    output_digest: str,
) -> dict:
    run_root = paths["staging"] / active["run_id"]
    try:
        _validate_contract(result, "fixer-output.schema.json")
        if result["round"] != payload["round"] or result["target"] != payload["target"]:
            raise PipelineError("Fixer round/target does not match its work item")
        target = result["target"]
        replacement = result["replacement"]
        if target == "SCHEMA_CARD":
            validate_schema_card(replacement)
            if (
                replacement["ontology_iri"] != project["ontology_iri"]
                or replacement["entity_namespace"] != project["entity_namespace"]
            ):
                raise PipelineError("Fixer cannot change Ontology Project identity")
            replacement_normalized = _canonical_json(replacement) + "\n"
        else:
            replacement_normalized = replacement.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
            if not replacement_normalized.strip():
                raise PipelineError("Fixer Markdown replacement cannot be empty")
    except PipelineError as exc:
        raise RunLifecycleError("invalid_submission", str(exc), 4) from exc

    _atomic_text(run_root / "work_items" / state["work_item_id"] / "reported_result.json", normalized)
    _complete_work_item(run_root, state, output_digest)
    replacement_digest = _digest_text(replacement_normalized)
    card = _read_json_strict(run_root / "artifacts" / "schema_card.json", "Schema Card")
    coverage = _read_json_strict(run_root / "artifacts" / "coverage.json", "Coverage")
    gate1 = _read_json_strict(
        run_root / "artifacts" / "qa_rounds" / f"{result['round']:02d}" / "gate1.json",
        "QA Gate 1 result",
    )
    if replacement_digest == payload["current_sha256"]:
        return _finalize_qa_and_publish(
            paths, ledger, active, card, round_number=result["round"],
            gates={"gate_1": "FAIL", "gate_2": "PASS", "gate_3": "PASS"},
            findings=gate1["findings"], qa_reasons=["FIXER_NO_CHANGE"],
            failed_chunk_count=len(coverage["failed_chunk_ids"]),
        )

    qa_state = _read_qa_state(run_root)
    qa_state["round"] = result["round"] + 1
    _write_qa_state(run_root, qa_state)
    if target == "CQ":
        _atomic_text(run_root / "artifacts" / "cqs.md", replacement_normalized)
        next_state = _create_work_item(
            run_root, "SRD", 2,
            _global_input(run_root, active["config"], "SRD", {"cq_sha256": replacement_digest}),
        )
        next_ids, next_stage = [next_state["work_item_id"]], "SRD"
    elif target == "SRD":
        _atomic_text(run_root / "artifacts" / "srd.md", replacement_normalized)
        cq_digest = _digest_text((run_root / "artifacts" / "cqs.md").read_text(encoding="utf-8"))
        schema_input = _global_input(
            run_root, active["config"], "SCHEMA_CARD",
            {"cq_sha256": cq_digest, "srd_sha256": replacement_digest},
        )
        schema_input["term_identity_registry"] = _captured_term_registry(
            run_root, project
        )
        next_state = _create_work_item(run_root, "SCHEMA_CARD", 3, schema_input)
        next_ids, next_stage = [next_state["work_item_id"]], "SCHEMA_CARD"
    else:
        next_ids, _ = _lock_schema(run_root, active, project, replacement)
        next_stage = "SCHEMA_LOCKED"

    active["current_stage"] = next_stage
    active["pending_work"] = next_ids
    ledger["active_run"] = active
    _integrity_write(paths["ledger"], ledger)
    if target == "SCHEMA_CARD" and not next_ids:
        return _complete_empty_pass(paths, ledger, active, replacement)
    return {
        "status": "accepted",
        "run_id": active["run_id"],
        "run_state": "ACTIVE",
        "current_stage": next_stage,
        "delivery_status": None,
        "pending_work_items": next_ids,
        "pending_work_details": _work_details(run_root, next_ids),
        "work_result": result,
        "output_digest": output_digest,
    }


def _run_submit(args: argparse.Namespace) -> dict:
    output_dir = _ensure_directory(args.output_dir, "output-dir", writable=True)
    paths = _project_paths(output_dir)
    project, project_digest = _read_project(paths)
    ledger, _ = _read_ledger(paths, project_digest)
    _validate_release_pointers(paths, ledger)
    active = _active_run(ledger)
    _migrate_term_identity_registry(paths, ledger, project, active)
    _validate_staging(paths, active, project_digest)
    _validate_resume_config(active, None, None)
    _recover_active_lock(paths, active["run_id"])
    run_root = paths["staging"] / active["run_id"]
    state, payload = _read_work_item(run_root, args.work_item_id)
    if state["status"] == "COMPLETE":
        raise RunLifecycleError("duplicate_submission", "work item 已完成，拒绝重复提交", 4)
    if args.work_item_id not in active["pending_work"]:
        raise RunLifecycleError("late_submission", "work item 已不在当前 pending set", 4)
    if args.input_digest != state["input_digest"]:
        raise RunLifecycleError("input_mismatch", "提交的 input digest 不匹配", 4)
    if state["stage"] not in GLOBAL_STAGES | QA_STAGES | {"ENTITY", "ASSERTION", "CANDIDATE_CRITIC"}:
        raise RunLifecycleError("late_submission", "当前 slice 不接受该 work item stage", 4)
    _migrate_abox_candidates_artifact(run_root)
    try:
        raw = args.result.read_bytes().decode("utf-8", errors="strict")
    except (OSError, UnicodeError) as exc:
        raise RunLifecycleError("invalid_submission", "提交结果无法按 UTF-8 读取", 4) from exc

    try:
        normalized, parsed, suffix = _normalize_submitted_output(raw, state["stage"])
    except RunLifecycleError:
        _record_attempt(run_root, state, payload, raw, raw, "json")
        _discard_reported_result(run_root, state)
        if state["stage"] not in {"ENTITY", "ASSERTION", "CANDIDATE_CRITIC"}:
            raise
        if _attempt_count(run_root, state) >= 2:
            return _terminal_chunk_failure(
                paths,
                ledger,
                active,
                state,
                payload,
                _digest_text(raw),
                "NO_RESPONSE" if not raw.strip() else "INVALID_JSON",
            )
        raise
    _record_attempt(run_root, state, payload, raw, normalized, suffix)
    output_digest = _digest_text(normalized)

    execution_failure = _execution_failure_code(parsed)
    if execution_failure is not None:
        code, detail = execution_failure
        _discard_reported_result(run_root, state)
        if state["stage"] not in {"ENTITY", "ASSERTION", "CANDIDATE_CRITIC"}:
            raise RunLifecycleError("invalid_submission", detail, 4)
        if _attempt_count(run_root, state) >= 2:
            return _terminal_chunk_failure(paths, ledger, active, state, payload, output_digest, code, code)
        raise RunLifecycleError("invalid_submission", detail, 4)

    if state["stage"] == "QA_GATE_1":
        assert isinstance(parsed, dict)
        return _accept_gate1_submission(
            paths, ledger, active, state, payload, parsed, normalized, output_digest
        )
    if state["stage"] == "FIXER":
        assert isinstance(parsed, dict)
        return _accept_fixer_submission(
            paths, project, ledger, active, state, payload, parsed, normalized, output_digest
        )

    if state["stage"] == "ENTITY":
        assert isinstance(parsed, dict)
        try:
            _validate_entity_result(parsed, payload)
        except PipelineError as exc:
            if _attempt_count(run_root, state) >= 2:
                _discard_reported_result(run_root, state)
                return _terminal_chunk_failure(
                    paths, ledger, active, state, payload, output_digest, "SCHEMA_VIOLATION"
                )
            raise RunLifecycleError("invalid_submission", str(exc), 4) from exc
        if payload.get("invocation_kind") == "CRITIC_REEXTRACTION":
            prior_digest = _prior_complete_output_digest(run_root, state, payload)
            if prior_digest == output_digest:
                return _terminal_chunk_failure(
                    paths, ledger, active, state, payload, output_digest, "REEXTRACTION_NO_CHANGE"
                )
        reported_path, final_path = _semantic_result_paths(run_root, state, parsed)
        _atomic_text(reported_path, normalized)
        if parsed["status"] == "retryable_failure":
            if _attempt_count(run_root, state) >= 2:
                return _terminal_chunk_failure(
                    paths, ledger, active, state, payload, output_digest,
                    "PASS_REPORTED_RETRYABLE_FAILURE", parsed["failure"]["code"],
                )
            return {
                "status": "accepted",
                "run_id": active["run_id"],
                "run_state": "ACTIVE",
                "current_stage": active["current_stage"],
                "delivery_status": None,
                "pending_work_items": active["pending_work"],
                "pending_work_details": _work_details(run_root, active["pending_work"]),
                "work_result": parsed,
                "output_digest": output_digest,
            }
        _atomic_text(final_path, normalized)
        if payload.get("invocation_kind") == "INITIAL":
            next_ids = [
                work_id
                for work_id in active["pending_work"]
                if work_id != state["work_item_id"]
            ]
        else:
            assertion_payload = {
                "version": RUN_CONTRACT_VERSION,
                "stage": "ASSERTION",
                "logical_sequence": state["logical_sequence"],
                "invocation_kind": "ENTITY_DEPENDENCY_RERUN",
                "invocation_sequence": payload.get("invocation_sequence", 1),
                "schema_card_sha256": payload["schema_card_sha256"],
                "schema_card": payload["schema_card"],
                "chunk": payload["chunk"],
                "candidate_id_prefix": payload["candidate_id_prefix"],
                "entity_result": parsed,
            }
            assertion_state = _create_work_item(
                run_root, "ASSERTION", state["logical_sequence"], assertion_payload
            )
            next_ids = [
                assertion_state["work_item_id"]
                if work_id == state["work_item_id"]
                else work_id
                for work_id in active["pending_work"]
            ]
        next_stage = "ABOX_WORK"
    elif state["stage"] == "ASSERTION":
        assert isinstance(parsed, dict)
        try:
            _validate_assertion_result(parsed, payload)
        except PipelineError as exc:
            if _attempt_count(run_root, state) >= 2:
                _discard_reported_result(run_root, state)
                return _terminal_chunk_failure(
                    paths, ledger, active, state, payload, output_digest, "SCHEMA_VIOLATION"
                )
            raise RunLifecycleError("invalid_submission", str(exc), 4) from exc
        if payload.get("invocation_kind") == "CRITIC_REEXTRACTION":
            prior_digest = _prior_complete_output_digest(run_root, state, payload)
            if prior_digest == output_digest:
                return _terminal_chunk_failure(
                    paths, ledger, active, state, payload, output_digest, "REEXTRACTION_NO_CHANGE"
                )
        reported_path, final_path = _semantic_result_paths(run_root, state, parsed)
        _atomic_text(reported_path, normalized)
        if parsed["status"] == "retryable_failure":
            if _attempt_count(run_root, state) >= 2:
                return _terminal_chunk_failure(
                    paths, ledger, active, state, payload, output_digest,
                    "PASS_REPORTED_RETRYABLE_FAILURE", parsed["failure"]["code"],
                )
            return {
                "status": "accepted",
                "run_id": active["run_id"],
                "run_state": "ACTIVE",
                "current_stage": active["current_stage"],
                "delivery_status": None,
                "pending_work_items": active["pending_work"],
                "pending_work_details": _work_details(run_root, active["pending_work"]),
                "work_result": parsed,
                "output_digest": output_digest,
            }
        _atomic_text(final_path, normalized)
        critic_payload = {
            "version": RUN_CONTRACT_VERSION,
            "stage": "CANDIDATE_CRITIC",
            "logical_sequence": state["logical_sequence"],
            "invocation_kind": payload.get("invocation_kind", "INITIAL"),
            "invocation_sequence": payload.get("invocation_sequence", 1),
            "schema_card_sha256": payload["schema_card_sha256"],
            "schema_card": payload["schema_card"],
            "chunk": payload["chunk"],
            "candidate_id_prefix": payload["candidate_id_prefix"],
            "entity_result": payload["entity_result"],
            "assertion_result": parsed,
        }
        critic_payload["review_id"] = "review-v1-" + _digest_text(
            _canonical_json([critic_payload, {"invocation": 1}])
        )
        critic_state = _create_work_item(
            run_root, "CANDIDATE_CRITIC", state["logical_sequence"], critic_payload
        )
        next_ids = [
            critic_state["work_item_id"] if work_id == state["work_item_id"] else work_id
            for work_id in active["pending_work"]
        ]
        next_stage = "ABOX_WORK"
    elif state["stage"] == "CANDIDATE_CRITIC":
        assert isinstance(parsed, dict)
        try:
            _validate_critic_result(parsed, payload)
        except PipelineError as exc:
            if _attempt_count(run_root, state) >= 2:
                _discard_reported_result(run_root, state)
                reason = "CRITIC_REVIEW_INCOMPLETE" if "review every candidate" in str(exc) else "SCHEMA_VIOLATION"
                return _terminal_chunk_failure(paths, ledger, active, state, payload, output_digest, reason)
            raise RunLifecycleError("invalid_submission", str(exc), 4) from exc
        reported_path, final_path = _semantic_result_paths(run_root, state, parsed)
        _atomic_text(reported_path, normalized)
        if parsed["status"] == "request_reextraction":
            target = _critic_request_target(parsed)
            chunk_id = payload["chunk"]["chunk_id"]
            if target is None:
                raise RunLifecycleError("invalid_submission", "Critic request has no target pass", 4)
            if _semantic_reextraction_used(run_root, chunk_id, target, payload["schema_card_sha256"]):
                return _terminal_chunk_failure(
                    paths, ledger, active, state, payload, output_digest,
                    "REEXTRACTION_BUDGET_EXHAUSTED",
                )
            _complete_work_item(run_root, state, output_digest)
            next_sequence = payload.get("invocation_sequence", 1) + 1
            if target == "entity":
                next_payload = {
                    "version": RUN_CONTRACT_VERSION,
                    "stage": "ENTITY",
                    "logical_sequence": state["logical_sequence"],
                    "invocation_kind": "CRITIC_REEXTRACTION",
                    "invocation_sequence": next_sequence,
                    "schema_card_sha256": payload["schema_card_sha256"],
                    "schema_card": payload["schema_card"],
                    "chunk": payload["chunk"],
                    "candidate_id_prefix": payload["candidate_id_prefix"],
                }
                next_state = _create_work_item(run_root, "ENTITY", state["logical_sequence"], next_payload)
            else:
                next_payload = {
                    "version": RUN_CONTRACT_VERSION,
                    "stage": "ASSERTION",
                    "logical_sequence": state["logical_sequence"],
                    "invocation_kind": "CRITIC_REEXTRACTION",
                    "invocation_sequence": next_sequence,
                    "schema_card_sha256": payload["schema_card_sha256"],
                    "schema_card": payload["schema_card"],
                    "chunk": payload["chunk"],
                    "candidate_id_prefix": payload["candidate_id_prefix"],
                    "entity_result": payload["entity_result"],
                }
                next_state = _create_work_item(run_root, "ASSERTION", state["logical_sequence"], next_payload)
            next_ids = [
                next_state["work_item_id"] if work_id == state["work_item_id"] else work_id
                for work_id in active["pending_work"]
            ]
            active["current_stage"] = "ABOX_WORK"
            active["pending_work"] = next_ids
            ledger["active_run"] = active
            _integrity_write(paths["ledger"], ledger)
            return {
                "status": "accepted",
                "run_id": active["run_id"],
                "run_state": "ACTIVE",
                "current_stage": active["current_stage"],
                "delivery_status": None,
                "pending_work_items": active["pending_work"],
                "pending_work_details": _work_details(run_root, active["pending_work"]),
                "work_result": parsed,
                "output_digest": output_digest,
            }
        _atomic_text(final_path, normalized)
        next_ids = [work_id for work_id in active["pending_work"] if work_id != state["work_item_id"]]
        next_stage = "ABOX_WORK"
    elif state["stage"] == "CQ":
        _atomic_text(run_root / "artifacts" / "cqs.md", normalized)
        next_state = _create_work_item(
            run_root,
            "SRD",
            2,
            _global_input(run_root, active["config"], "SRD", {"cq_sha256": output_digest}),
        )
        next_ids = [next_state["work_item_id"]]
        next_stage = "SRD"
    elif state["stage"] == "SRD":
        _atomic_text(run_root / "artifacts" / "srd.md", normalized)
        cq_digest = _digest_text((run_root / "artifacts" / "cqs.md").read_text(encoding="utf-8"))
        schema_input = _global_input(
            run_root,
            active["config"],
            "SCHEMA_CARD",
            {"cq_sha256": cq_digest, "srd_sha256": output_digest},
        )
        schema_input["term_identity_registry"] = _captured_term_registry(
            run_root, project
        )
        next_state = _create_work_item(
            run_root,
            "SCHEMA_CARD",
            3,
            schema_input,
        )
        next_ids = [next_state["work_item_id"]]
        next_stage = "SCHEMA_CARD"
    else:
        assert isinstance(parsed, dict)
        try:
            next_ids, _ = _lock_schema(run_root, active, project, parsed)
        except PipelineError as exc:
            raise RunLifecycleError("SCHEMA_CARD_INVALID", str(exc), 4) from exc
        next_stage = "SCHEMA_LOCKED"

    _complete_work_item(run_root, state, output_digest)
    if (
        state["stage"] == "ENTITY"
        and payload.get("invocation_kind") == "INITIAL"
        and not next_ids
    ):
        next_ids = _initial_assertion_work_items(
            run_root, payload["schema_card_sha256"]
        )
        if not next_ids:
            return _complete_nonempty_pass(paths, ledger, active, payload["schema_card"])
    if state["stage"] == "CANDIDATE_CRITIC" and not next_ids:
        return _complete_nonempty_pass(paths, ledger, active, payload["schema_card"])
    active["current_stage"] = next_stage
    active["pending_work"] = next_ids
    ledger["active_run"] = active
    _integrity_write(paths["ledger"], ledger)
    if state["stage"] == "SCHEMA_CARD" and not next_ids:
        assert isinstance(parsed, dict)
        return _complete_empty_pass(paths, ledger, active, parsed)
    return {
        "status": "accepted",
        "run_id": active["run_id"],
        "run_state": "ACTIVE",
        "current_stage": next_stage,
        "delivery_status": None,
        "pending_work_items": next_ids,
        "pending_work_details": _work_details(run_root, next_ids),
        "completed_work_item_id": state["work_item_id"],
        "output_digest": output_digest,
        **(
            {"work_result": parsed}
            if state["stage"] in {"ENTITY", "ASSERTION", "CANDIDATE_CRITIC"}
            else {}
        ),
    }


def _terminal_run(active: dict) -> dict:
    return {
        **active,
        "run_state": "FAILED",
        "current_stage": "RELEASE_SNAPSHOT",
        "delivery_status": "FAILED",
        "pending_work": [],
        "recent_errors": [{"code": "ORCHESTRATION_ABORTED"}],
    }


def _abort_snapshot_payloads(active: dict) -> dict[str, str]:
    delivery_status = {
        "version": RUN_CONTRACT_VERSION,
        "delivery_status": "FAILED",
        "failure_code": "ORCHESTRATION_ABORTED",
    }
    run_state = {
        "version": RUN_CONTRACT_VERSION,
        "run_id": active["run_id"],
        "run_state": "FAILED",
        "current_stage": "RELEASE_SNAPSHOT",
        "failure_code": "ORCHESTRATION_ABORTED",
        "project_digest": active["project_digest"],
        "config_digest": active["config_digest"],
        "config": active["config"],
    }
    return {
        "artifacts/run_state.json": _canonical_json(run_state) + "\n",
        "delivery_status.json": _canonical_json(delivery_status) + "\n",
    }


def _verify_release_snapshot(paths: dict[str, Path], snapshot_id: str) -> None:
    release_dir = paths["releases"] / snapshot_id
    try:
        manifest = json.loads((release_dir / "release_manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RunLifecycleError("ledger_corrupt", "Release Snapshot manifest 不可信", 5) from exc
    if not isinstance(manifest, dict):
        raise RunLifecycleError("ledger_corrupt", "Release Snapshot manifest 顶层必须是对象", 5)
    _require_exact_keys(manifest, {"version", "snapshot_id", "artifacts"}, "Release Snapshot manifest")
    if manifest["version"] != RUN_CONTRACT_VERSION or manifest["snapshot_id"] != snapshot_id:
        raise RunLifecycleError("ledger_corrupt", "Release Snapshot identity 不一致", 5)
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise RunLifecycleError("ledger_corrupt", "Release Snapshot artifacts 无效", 5)
    expected_files = {"release_manifest.json"}
    artifact_paths: list[str] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise RunLifecycleError("ledger_corrupt", "Release Snapshot artifact 无效", 5)
        _require_exact_keys(artifact, {"path", "sha256"}, "Release Snapshot artifact")
        if (
            not isinstance(artifact["path"], str)
            or not isinstance(artifact["sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"]) is None
        ):
            raise RunLifecycleError("ledger_corrupt", "Release Snapshot artifact 字段无效", 5)
        relative = PurePosixPath(artifact["path"])
        if relative.is_absolute() or ".." in relative.parts or relative.as_posix() != artifact["path"]:
            raise RunLifecycleError("ledger_corrupt", "Release Snapshot artifact path 无效", 5)
        artifact_path = release_dir.joinpath(*relative.parts)
        try:
            content = artifact_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise RunLifecycleError("ledger_corrupt", "Release Snapshot artifact 无法读取", 5) from exc
        if _digest_text(content) != artifact["sha256"]:
            raise RunLifecycleError("ledger_corrupt", "Release Snapshot artifact 摘要不一致", 5)
        artifact_paths.append(relative.as_posix())
        expected_files.add(relative.as_posix())
    if artifact_paths != sorted(artifact_paths) or len(artifact_paths) != len(set(artifact_paths)):
        raise RunLifecycleError("ledger_corrupt", "Release Snapshot artifacts 必须按路径排序且无重复", 5)
    if _digest_text(_canonical_json(artifacts)) != snapshot_id:
        raise RunLifecycleError("ledger_corrupt", "Release Snapshot content address 不一致", 5)
    actual_files = {path.relative_to(release_dir).as_posix() for path in release_dir.rglob("*") if path.is_file()}
    if actual_files != expected_files:
        raise RunLifecycleError("ledger_corrupt", "Release Snapshot 文件集合无效", 5)


def _publish_abort_snapshot(paths: dict[str, Path], active: dict) -> str:
    payloads = _abort_snapshot_payloads(active)
    artifacts = [
        {"path": relative, "sha256": _digest_text(content)} for relative, content in sorted(payloads.items())
    ]
    snapshot_id = _digest_text(_canonical_json(artifacts))
    release_manifest = {
        "version": RUN_CONTRACT_VERSION,
        "snapshot_id": snapshot_id,
        "artifacts": artifacts,
    }
    release_dir = paths["releases"] / snapshot_id
    if release_dir.exists():
        _verify_release_snapshot(paths, snapshot_id)
        existing_manifest = json.loads((release_dir / "release_manifest.json").read_text(encoding="utf-8"))
        if existing_manifest != release_manifest:
            raise RunLifecycleError("ledger_corrupt", "content-addressed Release Snapshot 冲突", 5)
    else:
        paths["releases"].mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(dir=paths["releases"], prefix=".snapshot."))
        try:
            for relative, content in payloads.items():
                _atomic_text(temporary.joinpath(*PurePosixPath(relative).parts), content)
            _atomic_text(temporary / "release_manifest.json", _canonical_json(release_manifest) + "\n")
            os.replace(temporary, release_dir)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
    return snapshot_id


def _complete_abort(paths: dict[str, Path], ledger: dict, active: dict, snapshot_id: str) -> dict:
    pointer = {
        "version": RUN_CONTRACT_VERSION,
        "snapshot_id": snapshot_id,
        "run_id": active["run_id"],
        "delivery_status": "FAILED",
    }
    _atomic_text(paths["latest_attempt"], _canonical_json(pointer) + "\n")
    ledger["active_run"] = _terminal_run(active)
    ledger["latest_attempt"] = snapshot_id
    _integrity_write(paths["ledger"], ledger)
    _release_lock(paths)
    return {
        "status": "accepted",
        "run_id": active["run_id"],
        "snapshot_id": snapshot_id,
        "run_state": "FAILED",
        "delivery_status": "FAILED",
        "pending_work_items": [],
    }


def _pending_abort_snapshot(
    paths: dict[str, Path], ledger: dict, project_digest: str
) -> tuple[dict, str] | None:
    active = ledger.get("active_run")
    if not isinstance(active, dict) or not isinstance(active.get("run_id"), str):
        return None
    marker_path = paths["staging"] / active["run_id"] / "abort_commit.json"
    if not marker_path.exists():
        return None
    marker, _ = _integrity_read(marker_path, "abort commit")
    expected_keys = {"version", "run_id", "snapshot_id", "project_digest"}
    _require_exact_keys(marker, expected_keys, "abort commit")
    if (
        marker["version"] != RUN_CONTRACT_VERSION
        or marker["run_id"] != active["run_id"]
        or marker["project_digest"] != project_digest
        or not isinstance(marker["snapshot_id"], str)
    ):
        raise RunLifecycleError("ledger_corrupt", "abort commit 与运行账本不一致", 5)
    _verify_release_snapshot(paths, marker["snapshot_id"])
    return active, marker["snapshot_id"]


def _recover_abort_commit(paths: dict[str, Path], ledger: dict, project_digest: str) -> dict | None:
    pending = _pending_abort_snapshot(paths, ledger, project_digest)
    if pending is None:
        return None
    _validate_latest_delivery_pointer(paths, ledger)
    active, snapshot_id = pending
    return _complete_abort(paths, ledger, active, snapshot_id)


def _run_abort(args: argparse.Namespace) -> dict:
    output_dir = _ensure_directory(args.output_dir, "output-dir", writable=True)
    paths = _project_paths(output_dir)
    project, project_digest = _read_project(paths)
    ledger, _ = _read_ledger(paths, project_digest)
    active = _active_run(ledger)
    _migrate_term_identity_registry(paths, ledger, project, active)
    _validate_staging(paths, active, project_digest)
    _recover_active_lock(paths, active["run_id"])
    snapshot_id = _publish_abort_snapshot(paths, active)
    marker = {
        "version": RUN_CONTRACT_VERSION,
        "run_id": active["run_id"],
        "snapshot_id": snapshot_id,
        "project_digest": project_digest,
    }
    _integrity_write(paths["staging"] / active["run_id"] / "abort_commit.json", marker)
    return _complete_abort(paths, ledger, active, snapshot_id)


def _validate_contract(value: dict, filename: str) -> None:
    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:
        requirements = Path(__file__).resolve().parent.parent / "requirements.txt"
        raise PipelineError(f"缺少 jsonschema，请先执行: pip install -r {requirements}") from exc

    contract_path = Path(__file__).resolve().parent.parent / "references" / filename
    contract = _read_json(contract_path)
    try:
        Draft202012Validator.check_schema(contract)
    except Exception as exc:
        raise PipelineError(f"JSON Schema 本身无效 {contract_path}: {exc}") from exc
    errors = sorted(Draft202012Validator(contract).iter_errors(value), key=lambda item: list(item.path))
    if errors:
        details = []
        for error in errors[:10]:
            location = ".".join(str(part) for part in error.absolute_path) or "$"
            details.append(f"{location}: {error.message}")
        if len(errors) > 10:
            details.append(f"另有 {len(errors) - 10} 个错误")
        raise PipelineError(f"不符合 {filename}: " + "; ".join(details))


def _contract_item_error(value: object, filename: str, definition: str) -> str | None:
    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:
        requirements = Path(__file__).resolve().parent.parent / "requirements.txt"
        raise PipelineError(f"缺少 jsonschema，请先执行: pip install -r {requirements}") from exc

    contract_path = Path(__file__).resolve().parent.parent / "references" / filename
    contract = _read_json(contract_path)
    item_contract = {
        "$schema": contract["$schema"],
        "$ref": f"#/$defs/{definition}",
        "$defs": contract["$defs"],
    }
    errors = sorted(
        Draft202012Validator(item_contract).iter_errors(value),
        key=lambda item: (list(item.path), item.message),
    )
    if not errors:
        return None
    return "; ".join(error.message for error in errors[:3])


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PipelineError(f"无法读取 JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PipelineError(f"JSON 顶层必须是对象: {path}")
    return value


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    content = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    _atomic_text(path, content)


def _local_name(iri: str) -> str:
    return iri.rsplit("#", 1)[-1] if "#" in iri else iri.rsplit("/", 1)[-1]


def _valid_absolute_iri(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    parsed = urlparse(value)
    return bool(parsed.scheme and (parsed.netloc or parsed.scheme == "urn")) and " " not in value


def _required(record: dict, fields: tuple[str, ...], context: str) -> None:
    missing = [field for field in fields if field not in record]
    if missing:
        raise PipelineError(f"{context} 缺少字段: {', '.join(missing)}")


def validate_schema_card(card: dict) -> dict:
    """Validate the Schema Card and return indexed terms plus subclass closure data."""
    _validate_contract(card, "schema-card.schema.json")
    _required(
        card,
        ("version", "ontology_iri", "entity_namespace", "classes", "object_properties", "datatype_properties"),
        "Schema Card",
    )
    if card["version"] != 1:
        raise PipelineError("Schema Card version 必须为 1")
    ontology_iri = card["ontology_iri"]
    namespace = card["entity_namespace"]
    if not _valid_absolute_iri(ontology_iri):
        raise PipelineError("ontology_iri 必须是绝对 IRI")
    if namespace != ontology_iri.rstrip("#") + "#":
        raise PipelineError("entity_namespace 必须等于 ontology_iri 加 '#' 后缀")

    collections = ("classes", "object_properties", "datatype_properties")
    for name in collections:
        if not isinstance(card[name], list):
            raise PipelineError(f"{name} 必须是数组")

    classes: dict[str, dict] = {}
    object_properties: dict[str, dict] = {}
    datatype_properties: dict[str, dict] = {}
    all_locals: dict[str, str] = {}

    def register(term: dict, target: dict[str, dict], context: str) -> None:
        if not isinstance(term, dict):
            raise PipelineError(f"{context} 必须是对象")
        _required(term, ("iri", "label", "comment"), context)
        iri = term["iri"]
        if not _valid_absolute_iri(iri) or not iri.startswith(namespace):
            raise PipelineError(f"{context}.iri 必须位于 entity_namespace 下: {iri}")
        local = _local_name(iri)
        if not LOCAL_NAME_RE.fullmatch(local):
            raise PipelineError(f"{context}.iri 本地名不合法: {local}")
        if re.fullmatch(r"I_[0-9a-f]{64}", local) is not None:
            raise PipelineError(f"{context}.iri 占用 Canonical Entity 保留本地名: {local}")
        if not isinstance(term["label"], str) or not term["label"].strip():
            raise PipelineError(f"{context}.label 不能为空")
        if not isinstance(term["comment"], str) or not term["comment"].strip():
            raise PipelineError(f"{context}.comment 不能为空")
        if iri in target:
            raise PipelineError(f"重复 IRI: {iri}")
        if local in all_locals:
            raise PipelineError(f"同一本地名映射到多个术语: {local}")
        target[iri] = term
        all_locals[local] = iri

    for index, term in enumerate(card["classes"]):
        register(term, classes, f"classes[{index}]")
        _required(term, ("superclasses", "equivalent_classes", "disjoint_with"), f"classes[{index}]")
    for index, term in enumerate(card["object_properties"]):
        register(term, object_properties, f"object_properties[{index}]")
        _required(
            term,
            ("domain", "range", "subproperty_of", "equivalent_properties", "inverse_of"),
            f"object_properties[{index}]",
        )
    for index, term in enumerate(card["datatype_properties"]):
        register(term, datatype_properties, f"datatype_properties[{index}]")
        _required(
            term,
            ("domain", "range", "subproperty_of", "equivalent_properties", "identity"),
            f"datatype_properties[{index}]",
        )

    def iri_list(term: dict, field: str, allowed: dict[str, dict], context: str) -> None:
        values = term[field]
        if not isinstance(values, list) or len(values) != len(set(values)):
            raise PipelineError(f"{context}.{field} 必须是无重复 IRI 数组")
        unknown = [value for value in values if value not in allowed]
        if unknown:
            raise PipelineError(f"{context}.{field} 引用了未声明术语: {unknown}")

    parents: dict[str, set[str]] = {iri: set() for iri in classes}
    for iri, term in classes.items():
        context = f"class {_local_name(iri)}"
        iri_list(term, "superclasses", classes, context)
        iri_list(term, "equivalent_classes", classes, context)
        iri_list(term, "disjoint_with", classes, context)
        parents[iri].update(term["superclasses"])

    def validate_property(iri: str, term: dict, same_kind: dict[str, dict], is_object: bool) -> None:
        context = f"property {_local_name(iri)}"
        if term["domain"] not in classes:
            raise PipelineError(f"{context}.domain 未声明为 Class")
        if is_object:
            if term["range"] not in classes:
                raise PipelineError(f"{context}.range 未声明为 Class")
            iri_list(term, "inverse_of", object_properties, context)
        elif not isinstance(term["range"], str) or not re.fullmatch(
            r"http://www\.w3\.org/2001/XMLSchema#[A-Za-z][A-Za-z0-9_]*", term["range"]
        ):
            raise PipelineError(f"{context}.range 必须是 XSD IRI")
        iri_list(term, "subproperty_of", same_kind, context)
        iri_list(term, "equivalent_properties", same_kind, context)
        if "max_count" in term and (not isinstance(term["max_count"], int) or term["max_count"] < 1):
            raise PipelineError(f"{context}.max_count 必须是正整数")
        if not is_object and not isinstance(term["identity"], bool):
            raise PipelineError(f"{context}.identity 必须是布尔值")

    for iri, term in object_properties.items():
        validate_property(iri, term, object_properties, True)
    for iri, term in datatype_properties.items():
        validate_property(iri, term, datatype_properties, False)

    result = {
        "classes": classes,
        "object_properties": object_properties,
        "datatype_properties": datatype_properties,
        "parents": parents,
        "all_locals": all_locals,
    }
    result["closure"] = _semantic_schema_closure(card, result)
    for iri, term in classes.items():
        parents[iri].update(term["equivalent_classes"])
        for equivalent in term["equivalent_classes"]:
            parents[equivalent].add(iri)
    return result


def _semantic_schema_closure(card: dict, index: dict) -> dict:
    """Validate folded class/property semantics and return deterministic closure metadata."""

    def groups(nodes: list[str], links: list[tuple[str, str]]) -> dict[str, str]:
        parent = {node: node for node in nodes}

        def find(node: str) -> str:
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        def union(left: str, right: str) -> None:
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                parent[max(left_root, right_root)] = min(left_root, right_root)

        for left, right in links:
            union(left, right)
        return {node: find(node) for node in nodes}

    classes = index["classes"]
    object_properties = index["object_properties"]
    datatype_properties = index["datatype_properties"]
    class_rep = groups(
        list(classes),
        [(iri, equivalent) for iri, term in classes.items() for equivalent in term["equivalent_classes"]],
    )
    if any(iri in term["superclasses"] for iri, term in classes.items()):
        raise PipelineError("SCHEMA_CARD_INVALID: class cannot be its own superclass")
    disjoint: set[tuple[str, str]] = set()
    for iri, term in classes.items():
        for other in term["disjoint_with"]:
            pair = tuple(sorted((class_rep[iri], class_rep[other])))
            if pair[0] == pair[1]:
                raise PipelineError("SCHEMA_CARD_INVALID: equivalent classes cannot be disjoint")
            disjoint.add(pair)

    def folded_edges(nodes: dict[str, dict], field: str, reps: dict[str, str]) -> set[tuple[str, str]]:
        return {(reps[child], reps[parent]) for child, term in nodes.items() for parent in term[field] if reps[child] != reps[parent]}

    class_edges = folded_edges(classes, "superclasses", class_rep)
    property_groups = {}
    property_edges = {}
    for kind, properties in (("object", object_properties), ("datatype", datatype_properties)):
        reps = groups(
            list(properties),
            [(iri, equivalent) for iri, term in properties.items() for equivalent in term["equivalent_properties"]],
        )
        property_groups[kind] = reps
        if any(iri in term["subproperty_of"] for iri, term in properties.items()):
            raise PipelineError("SCHEMA_CARD_INVALID: property cannot be its own superproperty")
        property_edges[kind] = folded_edges(properties, "subproperty_of", reps)

    def assert_acyclic(edges: set[tuple[str, str]], label: str) -> None:
        outgoing: dict[str, set[str]] = defaultdict(set)
        for child, parent in edges:
            outgoing[child].add(parent)
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> None:
            if node in visiting:
                raise PipelineError(f"SCHEMA_CARD_INVALID: {label} 形成环")
            if node in visited:
                return
            visiting.add(node)
            for parent in outgoing[node]:
                visit(parent)
            visiting.remove(node)
            visited.add(node)

        for node in list(outgoing):
            visit(node)

    assert_acyclic(class_edges, "superclass closure")
    assert_acyclic(property_edges["object"], "object subproperty closure")
    assert_acyclic(property_edges["datatype"], "datatype subproperty closure")

    def reaches(edges: set[tuple[str, str]], start: str, target: str) -> bool:
        pending = [start]
        seen: set[str] = set()
        while pending:
            current = pending.pop()
            if current == target:
                return True
            if current in seen:
                continue
            seen.add(current)
            pending.extend(parent for child, parent in edges if child == current)
        return False

    for left, right in disjoint:
        if reaches(class_edges, left, right) or reaches(class_edges, right, left):
            raise PipelineError("SCHEMA_CARD_INVALID: disjoint classes have an incompatible superclass closure")

    def class_compatible(left: str, right: str) -> bool:
        return class_rep[left] == class_rep[right]

    for kind, properties in (("object", object_properties), ("datatype", datatype_properties)):
        reps = property_groups[kind]
        members: dict[str, list[str]] = defaultdict(list)
        for iri in properties:
            members[reps[iri]].append(iri)
        for representative, member_iris in members.items():
            domains = {class_rep[properties[iri]["domain"]] for iri in member_iris}
            ranges = {class_rep[properties[iri]["range"]] if kind == "object" else properties[iri]["range"] for iri in member_iris}
            if len(domains) != 1 or len(ranges) != 1:
                raise PipelineError("SCHEMA_CARD_INVALID: equivalent properties have incompatible domain/range")
            max_counts = {properties[iri].get("max_count") for iri in member_iris}
            if len(max_counts) > 1:
                raise PipelineError("SCHEMA_CARD_INVALID: equivalent properties have incompatible max_count")
            if kind == "datatype":
                identities = {properties[iri]["identity"] for iri in member_iris}
                if len(identities) != 1:
                    raise PipelineError("SCHEMA_CARD_INVALID: equivalent identity properties must share identity=true")

        edges = property_edges[kind]
        for child, parent in edges:
            child_members = members[child]
            parent_members = members[parent]
            child_domain = properties[child_members[0]]["domain"]
            parent_domain = properties[parent_members[0]]["domain"]
            if not (class_compatible(child_domain, parent_domain) or reaches(class_edges, class_rep[child_domain], class_rep[parent_domain])):
                raise PipelineError("SCHEMA_CARD_INVALID: subproperty domain closure is incompatible")
            if kind == "object":
                child_range = properties[child_members[0]]["range"]
                parent_range = properties[parent_members[0]]["range"]
                if not (class_compatible(child_range, parent_range) or reaches(class_edges, class_rep[child_range], class_rep[parent_range])):
                    raise PipelineError("SCHEMA_CARD_INVALID: subproperty range closure is incompatible")
            elif properties[child_members[0]]["range"] != properties[parent_members[0]]["range"]:
                raise PipelineError("SCHEMA_CARD_INVALID: datatype subproperty range closure is incompatible")

    inverse_pairs: set[tuple[str, str]] = set()
    for iri, term in object_properties.items():
        for inverse in term["inverse_of"]:
            left, right = property_groups["object"][iri], property_groups["object"][inverse]
            inverse_pairs.add(tuple(sorted((left, right))))
            left_term = object_properties[next(member for member in object_properties if property_groups["object"][member] == left)]
            right_term = object_properties[next(member for member in object_properties if property_groups["object"][member] == right)]
            if class_rep[left_term["domain"]] != class_rep[right_term["range"]] or class_rep[left_term["range"]] != class_rep[right_term["domain"]]:
                raise PipelineError("SCHEMA_CARD_INVALID: inverse domain/range closure is incompatible")

    for iri, term in datatype_properties.items():
        if term["range"] not in ALLOWED_XSD_DATATYPES:
            raise PipelineError(f"SCHEMA_CARD_INVALID: datatype 不在允许 profile: {term['range']}")

    return {
        "class_representatives": dict(sorted(class_rep.items())),
        "object_property_representatives": dict(sorted(property_groups["object"].items())),
        "datatype_property_representatives": dict(sorted(property_groups["datatype"].items())),
        "class_super_edges": sorted(class_edges),
        "object_subproperty_edges": sorted(property_edges["object"]),
        "datatype_subproperty_edges": sorted(property_edges["datatype"]),
        "inverse_pairs": sorted(inverse_pairs),
        "disjoint_pairs": sorted(disjoint),
    }


def _is_subclass(actual: str, expected: str, parents: dict[str, set[str]]) -> bool:
    pending = [actual]
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if current == expected:
            return True
        if current in seen:
            continue
        seen.add(current)
        pending.extend(parents.get(current, ()))
    return False


def _normalize(value: object) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value)).casefold().split())


def _slug(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    words = re.findall(r"[A-Za-z0-9]+", ascii_value)
    return "_".join(words[:5])[:48] or "Instance"


def _digest(value: str, length: int = 12) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _canonical_sources(workspace: Path, sources: list[Path]) -> set[str]:
    root = workspace.resolve()
    result: set[str] = set()
    for source in sources:
        absolute = source.resolve() if source.is_absolute() else (root / source).resolve()
        try:
            relative = absolute.relative_to(root)
        except ValueError as exc:
            raise PipelineError(f"Source Document 必须位于 workspace 内: {source}") from exc
        if not absolute.is_file() or absolute.suffix.lower() not in (".md", ".markdown"):
            raise PipelineError(f"Source Document 必须是存在的 Markdown 文件: {source}")
        result.add(relative.as_posix())
    if not result:
        raise PipelineError("至少指定一个 --source")
    return result


def _validate_evidence(evidence: object, workspace: Path, sources: set[str]) -> tuple[dict | None, str | None]:
    if not isinstance(evidence, dict):
        return None, "evidence 必须是对象"
    required = ("source", "heading_path", "line_start", "line_end", "quote")
    if any(field not in evidence for field in required):
        return None, "evidence 字段不完整"
    source = evidence["source"]
    if not isinstance(source, str) or source not in sources:
        return None, "evidence.source 不在用户明确选择的 Source Documents 中"
    pure = PurePosixPath(source)
    if pure.is_absolute() or ".." in pure.parts:
        return None, "evidence.source 必须是 workspace-relative POSIX path"
    start, end = evidence["line_start"], evidence["line_end"]
    if not isinstance(start, int) or not isinstance(end, int) or start < 1 or end < start:
        return None, "evidence 行号范围无效"
    quote = evidence["quote"]
    if not isinstance(quote, str) or not quote.strip():
        return None, "evidence.quote 不能为空"
    if not isinstance(evidence["heading_path"], list) or not all(
        isinstance(item, str) for item in evidence["heading_path"]
    ):
        return None, "evidence.heading_path 必须是字符串数组"
    lines = (workspace / source).read_text(encoding="utf-8").splitlines()
    if end > len(lines):
        return None, "evidence 行号超出 Source Document"
    excerpt = _normalize("\n".join(lines[start - 1 : end]))
    if _normalize(quote) not in excerpt:
        return None, "evidence.quote 未出现在指定行范围"
    return {
        "source": source,
        "heading_path": evidence["heading_path"],
        "line_start": start,
        "line_end": end,
        "quote": quote,
    }, None


def _literal_is_valid(value: str, datatype: str) -> bool:
    return literal_is_valid(value, datatype)


def resolve_candidates(card: dict, candidates: dict, workspace: Path, sources: list[Path]) -> tuple[dict, list[dict], list[dict]]:
    """Resolve candidates into canonical individuals and admitted assertions."""
    index = validate_schema_card(card)
    selected_sources = _canonical_sources(workspace, sources)
    _required(candidates, ("version", "entities", "assertions"), "ABox candidates")
    if set(candidates) != {"version", "entities", "assertions"}:
        raise PipelineError("ABox candidates 顶层字段集合无效")
    if candidates["version"] != 1:
        raise PipelineError("ABox candidates version 必须为 1")
    if not isinstance(candidates["entities"], list) or not isinstance(candidates["assertions"], list):
        raise PipelineError("entities 和 assertions 必须是数组")

    rejections: list[dict] = []
    evidence_rows: list[dict] = []

    def reject(
        candidate_id: str,
        kind: str,
        reason: str,
        evidence: object = None,
        context: dict | None = None,
    ) -> None:
        row = {"candidate_id": candidate_id, "candidate_kind": kind, "reasons": [reason]}
        if (
            isinstance(evidence, dict)
            and _contract_item_error(evidence, "abox-candidates.schema.json", "evidence") is None
        ):
            row["evidence"] = evidence
        if context is not None:
            row["context"] = context
        rejections.append(row)

    entity_id_counts = Counter(
        item["candidate_id"]
        for item in candidates["entities"]
        if isinstance(item, dict) and isinstance(item.get("candidate_id"), str) and item["candidate_id"]
    )
    assertion_id_counts = Counter(
        item["candidate_id"]
        for item in candidates["assertions"]
        if isinstance(item, dict) and isinstance(item.get("candidate_id"), str) and item["candidate_id"]
    )
    candidate_to_iri: dict[str, str] = {}
    canonical: dict[str, dict] = {}
    entity_evidence: list[tuple[str, str, str, dict]] = []
    synthetic_assertions: list[dict] = []
    entity_failure_reasons: dict[str, list[str]] = {}
    entity_failure_contexts: dict[str, dict] = {}
    identity_groups: dict[str, list[dict]] = defaultdict(list)

    def reject_entity(
        candidate_id: str, reason: str, evidence: object = None, context: dict | None = None
    ) -> None:
        reject(candidate_id, "entity", reason, evidence, context)
        entity_failure_reasons[candidate_id] = [reason]
        if context is not None:
            entity_failure_contexts[candidate_id] = context

    for position, item in enumerate(candidates["entities"]):
        fallback_id = f"entity[{position}]"
        if not isinstance(item, dict):
            reject_entity(fallback_id, "ENTITY_CONTRACT_INVALID")
            continue
        candidate_id = item.get("candidate_id", fallback_id)
        if not isinstance(candidate_id, str) or not candidate_id:
            reject_entity(fallback_id, "ENTITY_CANDIDATE_ID_INVALID", item.get("evidence"))
            continue
        contract_error = _contract_item_error(item, "abox-candidates.schema.json", "entity")
        if contract_error is not None:
            reject_entity(candidate_id, "ENTITY_CONTRACT_INVALID", item.get("evidence"))
            continue
        if entity_id_counts[candidate_id] != 1:
            reject_entity(candidate_id, "ENTITY_CANDIDATE_ID_DUPLICATE", item.get("evidence"))
            continue
        class_iri = item.get("class_iri")
        name = item.get("name")
        if class_iri not in index["classes"]:
            reject_entity(candidate_id, "ENTITY_CLASS_UNDECLARED", item.get("evidence"))
            continue
        if not isinstance(name, str) or not name.strip():
            reject_entity(candidate_id, "ENTITY_NAME_INVALID", item.get("evidence"))
            continue
        grounded, error = _validate_evidence(item.get("evidence"), workspace, selected_sources)
        if error:
            reject_entity(candidate_id, "ENTITY_EVIDENCE_INVALID", item.get("evidence"))
            continue

        business_identifier = item.get("business_identifier")
        if business_identifier is not None:
            if not isinstance(business_identifier, dict):
                reject_entity(candidate_id, "ENTITY_BUSINESS_IDENTIFIER_INVALID", grounded)
                continue
            property_iri = business_identifier.get("property_iri")
            value = business_identifier.get("value")
            prop = index["datatype_properties"].get(property_iri)
            if (
                prop is None
                or prop.get("identity") is not True
                or not _is_subclass(class_iri, prop["domain"], index["parents"])
                or not isinstance(value, str)
                or not value.strip()
                or not _literal_is_valid(value, prop["range"])
            ):
                reject_entity(candidate_id, "ENTITY_BUSINESS_IDENTIFIER_INVALID", grounded)
                continue
            representative = index["closure"]["datatype_property_representatives"][property_iri]
            normalized_value = unicodedata.normalize("NFC", value)
            identity_material = ["business", representative, normalized_value]
        else:
            property_iri = None
            value = None
            representative = None
            normalized_value = None
            identity_material = [
                "source", grounded["source"], class_iri, unicodedata.normalize("NFC", name.strip())
            ]
        identity_key = _canonical_json(identity_material)
        identity_groups[identity_key].append(
            {
                "candidate_id": candidate_id,
                "class_iri": class_iri,
                "name": name,
                "evidence": grounded,
                "business_property_iri": property_iri,
                "business_property_representative": representative,
                "business_value": value,
                "business_identity_value": normalized_value,
                "identity_key": identity_key,
            }
        )

    class_representatives = index["closure"]["class_representatives"]
    class_edges = {tuple(edge) for edge in index["closure"]["class_super_edges"]}

    def reaches_class(actual: str, expected: str) -> bool:
        pending = [actual]
        seen: set[str] = set()
        while pending:
            current = pending.pop()
            if current == expected:
                return True
            if current in seen:
                continue
            seen.add(current)
            pending.extend(parent for child, parent in class_edges if child == current)
        return False

    local_identities: dict[str, str] = {}
    for identity_key in sorted(identity_groups):
        members = sorted(
            identity_groups[identity_key],
            key=lambda member: (
                member["evidence"]["source"], member["evidence"]["line_start"], member["candidate_id"]
            ),
        )
        business_group = members[0]["business_property_representative"] is not None
        if business_group:
            class_groups = {class_representatives[member["class_iri"]] for member in members}
            specific = sorted(
                candidate
                for candidate in class_groups
                if all(reaches_class(candidate, other) for other in class_groups)
            )
            if len(specific) != 1:
                conflict_id = "conflict-v1-" + _digest_text(
                    _canonical_json(["identity_class", identity_key, sorted(class_groups)])
                )
                context = {
                    "conflict_id": conflict_id,
                    "class_iris": sorted(class_groups),
                    "candidate_ids": sorted(member["candidate_id"] for member in members),
                }
                for member in members:
                    reject_entity(
                        member["candidate_id"], "IDENTITY_CLASS_CONFLICT", member["evidence"], context
                    )
                continue
            canonical_class = specific[0]
        else:
            canonical_class = members[0]["class_iri"]

        local = "I_" + _digest_text(identity_key)
        prior_identity = local_identities.get(local)
        if prior_identity is not None and prior_identity != identity_key:
            raise PipelineError("CANONICAL_IDENTITY_HASH_COLLISION")
        if local in index["all_locals"]:
            raise PipelineError("CANONICAL_IDENTITY_LOCAL_NAME_CONFLICT")
        local_identities[local] = identity_key
        iri = card["entity_namespace"] + local

        canonical_mention = members[0]
        canonical_name_key = canonical_mention["name"]
        aliases: dict[str, dict] = {}
        for member in members[1:]:
            alias_key = member["name"]
            if alias_key == canonical_name_key:
                continue
            alias = aliases.setdefault(
                alias_key,
                {
                    "alias_id": "alias-v1-"
                    + _digest_text(_canonical_json(["alias", iri, member["name"]])),
                    "name": member["name"],
                    "candidate_ids": [],
                    "evidence_records": [],
                },
            )
            alias["candidate_ids"].append(member["candidate_id"])
            alias["evidence_records"].append(member["evidence"])
        individual = {
            "iri": iri,
            "class_iri": canonical_class,
            "label": canonical_mention["name"],
            "identity": (
                {"kind": "business"}
                if business_group
                else {"kind": "source", "source": canonical_mention["evidence"]["source"]}
            ),
            "candidate_ids": sorted(member["candidate_id"] for member in members),
            "observed_aliases": [aliases[key] for key in sorted(aliases)],
        }
        if business_group:
            individual["business_identifier"] = {
                "property_iri": members[0]["business_property_representative"],
                "value": members[0]["business_identity_value"],
            }
        canonical[iri] = individual
        for member in members:
            candidate_id = member["candidate_id"]
            candidate_to_iri[candidate_id] = iri
            entity_evidence.append((candidate_id, iri, member["class_iri"], member["evidence"]))
            if member["business_property_iri"] is not None:
                property_iri = member["business_property_iri"]
                synthetic_assertions.append(
                    {
                        "candidate_id": f"{candidate_id}:business-identifier",
                        "kind": "data",
                        "subject_candidate_id": candidate_id,
                        "property_iri": property_iri,
                        "value": member["business_value"],
                        "datatype": index["datatype_properties"][property_iri]["range"],
                        "evidence": member["evidence"],
                        "_evidence_validated": True,
                    }
                )

    raw_assertions: list[dict] = []
    assertions_to_process = list(candidates["assertions"]) + synthetic_assertions
    for position, item in enumerate(assertions_to_process):
        fallback_id = f"assertion[{position}]"
        if not isinstance(item, dict):
            reject(fallback_id, "assertion", "ASSERTION_CONTRACT_INVALID")
            continue
        candidate_id = item.get("candidate_id", fallback_id)
        synthetic = item.get("_evidence_validated") is True
        if not isinstance(candidate_id, str) or not candidate_id:
            reject(fallback_id, "assertion", "ASSERTION_CANDIDATE_ID_INVALID", item.get("evidence"))
            continue
        if not synthetic:
            contract_error = _contract_item_error(item, "abox-candidates.schema.json", "assertion")
            if contract_error is not None:
                reject(candidate_id, "assertion", "ASSERTION_CONTRACT_INVALID", item.get("evidence"))
                continue
        if not synthetic and assertion_id_counts[candidate_id] != 1:
            reject(candidate_id, "assertion", "ASSERTION_CANDIDATE_ID_DUPLICATE", item.get("evidence"))
            continue
        subject_id = item.get("subject_candidate_id")
        subject_iri = candidate_to_iri.get(subject_id)
        if subject_iri is None:
            reject(
                candidate_id,
                "assertion",
                "ASSERTION_SUBJECT_UNRESOLVED",
                item.get("evidence"),
                {
                    "subject_candidate_id": subject_id,
                    "subject_reasons": entity_failure_reasons.get(subject_id, ["ENTITY_NOT_RESOLVED"]),
                    "subject_context": entity_failure_contexts.get(subject_id),
                },
            )
            continue
        kind = item.get("kind")
        property_iri = item.get("property_iri")
        properties = index["object_properties"] if kind == "object" else index["datatype_properties"] if kind == "data" else {}
        prop = properties.get(property_iri)
        if prop is None:
            reject(candidate_id, "assertion", "ASSERTION_PROPERTY_INVALID", item.get("evidence"))
            continue
        subject_class = canonical[subject_iri]["class_iri"]
        if not _is_subclass(subject_class, prop["domain"], index["parents"]):
            reject(candidate_id, "assertion", "ASSERTION_DOMAIN_MISMATCH", item.get("evidence"))
            continue
        if synthetic:
            grounded = item["evidence"]
        else:
            grounded, error = _validate_evidence(item.get("evidence"), workspace, selected_sources)
            if error:
                reject(candidate_id, "assertion", "ASSERTION_EVIDENCE_INVALID", item.get("evidence"))
                continue

        row = {
            "candidate_id": candidate_id,
            "kind": kind,
            "subject_iri": subject_iri,
            "property_iri": property_iri,
            "evidence": grounded,
        }
        if kind == "object":
            object_iri = candidate_to_iri.get(item.get("object_candidate_id"))
            if object_iri is None:
                object_id = item.get("object_candidate_id")
                reject(
                    candidate_id,
                    "assertion",
                    "ASSERTION_OBJECT_UNRESOLVED",
                    grounded,
                    {
                        "object_candidate_id": object_id,
                        "object_reasons": entity_failure_reasons.get(object_id, ["ENTITY_NOT_RESOLVED"]),
                        "object_context": entity_failure_contexts.get(object_id),
                    },
                )
                continue
            object_class = canonical[object_iri]["class_iri"]
            if not _is_subclass(object_class, prop["range"], index["parents"]):
                reject(candidate_id, "assertion", "ASSERTION_RANGE_MISMATCH", grounded)
                continue
            row["object_iri"] = object_iri
            row["key"] = (kind, subject_iri, property_iri, object_iri)
        else:
            value = item.get("value")
            datatype = item.get("datatype")
            if not isinstance(value, str) or datatype != prop["range"] or not _literal_is_valid(value, datatype):
                reject(candidate_id, "assertion", "ASSERTION_LITERAL_INVALID", grounded)
                continue
            row["value"] = value
            row["datatype"] = datatype
            row["key"] = (kind, subject_iri, property_iri, value, datatype)
        raw_assertions.append(row)

    object_representatives = index["closure"]["object_property_representatives"]
    datatype_representatives = index["closure"]["datatype_property_representatives"]
    object_super_edges = {tuple(edge) for edge in index["closure"]["object_subproperty_edges"]}
    datatype_super_edges = {tuple(edge) for edge in index["closure"]["datatype_subproperty_edges"]}
    inverse_adjacency: dict[str, set[str]] = defaultdict(set)
    for left, right in index["closure"]["inverse_pairs"]:
        inverse_adjacency[left].add(right)
        inverse_adjacency[right].add(left)

    object_members: dict[str, list[dict]] = defaultdict(list)
    datatype_members: dict[str, list[dict]] = defaultdict(list)
    for iri, prop in index["object_properties"].items():
        object_members[object_representatives[iri]].append(prop)
    for iri, prop in index["datatype_properties"].items():
        datatype_members[datatype_representatives[iri]].append(prop)

    def semantic_states(row: dict) -> set[tuple[str, str, str]]:
        if row["kind"] == "object":
            start = (object_representatives[row["property_iri"]], row["subject_iri"], row["object_iri"])
            super_edges = object_super_edges
        else:
            start = (datatype_representatives[row["property_iri"]], row["subject_iri"], row["value"])
            super_edges = datatype_super_edges
        pending = [start]
        states: set[tuple[str, str, str]] = set()
        while pending:
            property_rep, subject, value = pending.pop()
            state = (property_rep, subject, value)
            if state in states:
                continue
            states.add(state)
            pending.extend((parent, subject, value) for child, parent in super_edges if child == property_rep)
            if row["kind"] == "object":
                pending.extend((inverse, value, subject) for inverse in inverse_adjacency[property_rep])
        return states

    constraint_slots: dict[tuple[str, str, str], dict[tuple[str, ...], list[dict]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in raw_assertions:
        for property_rep, subject, value in semantic_states(row):
            if row["kind"] == "object":
                value_key = (value,)
            else:
                value_key = (row["datatype"], value)
            constraint_slots[(row["kind"], subject, property_rep)][value_key].append(row)

    candidate_conflicts: dict[str, list[dict]] = defaultdict(list)
    for slot in sorted(constraint_slots):
        kind, subject, property_rep = slot
        members = object_members[property_rep] if kind == "object" else datatype_members[property_rep]
        max_count = members[0].get("max_count")
        distinct = constraint_slots[slot]
        if max_count is None or len(distinct) <= max_count:
            continue
        values = [list(value) for value in sorted(distinct)]
        conflict_id = "conflict-v1-" + _digest_text(
            _canonical_json(["max_count", kind, subject, property_rep, max_count, values])
        )
        peer_ids = sorted({row["candidate_id"] for rows in distinct.values() for row in rows})
        context = {
            "conflict_id": conflict_id,
            "constraint_kind": kind,
            "constraint_subject": subject,
            "constraint_property_iri": property_rep,
            "max_count": max_count,
            "values": values,
            "candidate_ids": peer_ids,
        }
        for rows in distinct.values():
            for row in rows:
                candidate_conflicts[row["candidate_id"]].append(context)

    conflict_ids = set(candidate_conflicts)
    for candidate_id in sorted(candidate_conflicts):
        contexts = sorted(candidate_conflicts[candidate_id], key=lambda item: item["conflict_id"])
        row = next(row for row in raw_assertions if row["candidate_id"] == candidate_id)
        primary = {**contexts[0], "conflict_ids": [item["conflict_id"] for item in contexts]}
        reject(candidate_id, "assertion", "MAX_COUNT_CONFLICT", row["evidence"], primary)

    admitted: list[dict] = []
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for row in raw_assertions:
        if row["candidate_id"] not in conflict_ids:
            grouped[row["key"]].append(row)
    for key in sorted(grouped, key=lambda value: tuple(str(part) for part in value)):
        rows = grouped[key]
        first = rows[0]
        assertion = {
            field: first[field]
            for field in ("kind", "subject_iri", "property_iri", "object_iri", "value", "datatype")
            if field in first
        }
        assertion["candidate_ids"] = sorted(row["candidate_id"] for row in rows)
        fact_id = "fact-v1-" + _digest_text(_canonical_json(["fact", *key]))
        assertion["fact_id"] = fact_id
        admitted.append(assertion)
        for row in rows:
            evidence_rows.append(
                {
                    "fact_id": fact_id,
                    "candidate_id": row["candidate_id"],
                    "candidate_kind": "assertion",
                    "status": "admitted",
                    "subject": row["subject_iri"],
                    "predicate": row["property_iri"],
                    "object": row.get("object_iri", row.get("value")),
                    "datatype": row.get("datatype"),
                    "evidence": row["evidence"],
                }
            )

    members_by_iri: dict[str, list[tuple[str, str, dict]]] = defaultdict(list)
    for candidate_id, iri, member_class_iri, grounded in entity_evidence:
        if candidate_id in candidate_to_iri:
            members_by_iri[iri].append((candidate_id, member_class_iri, grounded))

    for iri in sorted(members_by_iri):
        individual = canonical[iri]
        class_iri = individual["class_iri"]
        canonical_candidate_id, _, canonical_grounded = min(
            members_by_iri[iri], key=lambda item: (item[2]["source"], item[2]["line_start"], item[0])
        )
        type_candidate_id, _, type_grounded = min(
            (
                item for item in members_by_iri[iri]
                if (
                    class_representatives[item[1]] == class_iri
                    if "business_identifier" in individual
                    else item[1] == class_iri
                )
            ),
            key=lambda item: (item[2]["source"], item[2]["line_start"], item[0]),
        )
        type_fact_id = "fact-v1-" + _digest_text(
            _canonical_json(["fact", "type", iri, class_iri])
        )
        label_fact_id = "fact-v1-" + _digest_text(
            _canonical_json(["fact", "label", iri, individual["label"]])
        )
        individual["type_fact_id"] = type_fact_id
        individual["label_fact_id"] = label_fact_id
        evidence_rows.append(
            {
                "fact_id": type_fact_id,
                "candidate_id": type_candidate_id,
                "candidate_kind": "entity",
                "status": "admitted",
                "subject": iri,
                "predicate": "http://www.w3.org/1999/02/22-rdf-syntax-ns#type",
                "object": class_iri,
                "datatype": None,
                "evidence": type_grounded,
            }
        )
        evidence_rows.append(
            {
                "fact_id": label_fact_id,
                "candidate_id": canonical_candidate_id,
                "candidate_kind": "entity",
                "status": "admitted",
                "subject": iri,
                "predicate": "http://www.w3.org/2000/01/rdf-schema#label",
                "object": individual["label"],
                "datatype": None,
                "evidence": canonical_grounded,
            }
        )
        for alias in individual["observed_aliases"]:
            for candidate_id, grounded in zip(alias["candidate_ids"], alias["evidence_records"]):
                evidence_rows.append(
                    {
                        "fact_id": alias["alias_id"],
                        "candidate_id": candidate_id,
                        "candidate_kind": "entity",
                        "status": "observed_alias",
                        "subject": iri,
                        "predicate": "urn:ontology-auto-generation:observedAlias",
                        "object": alias["name"],
                        "datatype": None,
                        "evidence": grounded,
                    }
                )

    resolved = {
        "version": 1,
        "ontology_iri": card["ontology_iri"],
        "entity_namespace": card["entity_namespace"],
        "schema_card_sha256": _digest_text(_canonical_json(card) + "\n"),
        "individuals": sorted(canonical.values(), key=lambda item: item["iri"]),
        "assertions": admitted,
    }
    evidence_rows.sort(key=lambda row: (row["fact_id"], row["candidate_id"]))
    rejections.sort(
        key=lambda row: (row["candidate_kind"], row["candidate_id"], row["reasons"][0], _canonical_json(row))
    )
    _validate_contract(resolved, "resolved-instances.schema.json")
    for row in evidence_rows:
        _validate_contract(row, "evidence-record.schema.json")
    for row in rejections:
        _validate_contract(row, "rejection-record.schema.json")
    return resolved, evidence_rows, rejections


def _restricted_rdf_xml_bytes(graph: object, entity_namespace: str) -> bytes:
    from rdflib import BNode, Literal, RDF, RDFS, OWL, URIRef

    namespace_prefixes = (
        ("rdf", str(RDF)),
        ("rdfs", str(RDFS)),
        ("owl", str(OWL)),
        ("xsd", XSD_PREFIX),
        ("ont", entity_namespace),
    )
    for prefix, namespace in namespace_prefixes:
        ET.register_namespace(prefix, namespace)

    root = ET.Element(ET.QName(str(RDF), "RDF"))
    triples = list(graph)  # type: ignore[arg-type]
    if any(isinstance(node, BNode) for triple in triples for node in triple):
        raise PipelineError("restricted RDF/XML forbids blank nodes")

    predicates = {str(predicate) for _, predicate, _ in triples}
    allowed_predicate_namespaces = {str(RDF), str(RDFS), str(OWL), entity_namespace}
    for predicate in predicates:
        if not any(predicate.startswith(namespace) for namespace in allowed_predicate_namespaces):
            raise PipelineError(f"restricted RDF/XML predicate namespace is not allowed: {predicate}")

    by_subject: dict[URIRef, list[tuple[URIRef, object]]] = defaultdict(list)

    def xml10_valid(value: str) -> bool:
        return all(
            character in "\t\n\r"
            or "\u0020" <= character <= "\ud7ff"
            or "\ue000" <= character <= "\ufffd"
            or "\U00010000" <= character <= "\U0010ffff"
            for character in value
        )

    for subject, predicate, value in triples:
        if not isinstance(subject, URIRef) or not isinstance(predicate, URIRef):
            raise PipelineError("restricted RDF/XML requires IRI subjects and predicates")
        if not xml10_valid(str(subject)) or not xml10_valid(str(predicate)):
            raise PipelineError("restricted RDF/XML encountered an XML 1.0-invalid IRI")
        if isinstance(value, (URIRef, Literal)) and not xml10_valid(str(value)):
            raise PipelineError("restricted RDF/XML encountered an XML 1.0-invalid value")
        by_subject[subject].append((predicate, value))

    def object_key(value: object) -> tuple[str, str, str, str]:
        if isinstance(value, URIRef):
            return ("0", str(value), "", "")
        if isinstance(value, Literal):
            return ("1", str(value.datatype or ""), value.language or "", str(value))
        raise PipelineError("restricted RDF/XML object must be an IRI or literal")

    def predicate_qname(predicate: URIRef) -> ET.QName:
        value = str(predicate)
        for _, namespace in namespace_prefixes:
            if value.startswith(namespace):
                local = value[len(namespace) :]
                if local and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", local):
                    return ET.QName(namespace, local)
        raise PipelineError(f"restricted RDF/XML predicate cannot be represented as QName: {value}")

    for subject in sorted(by_subject, key=str):
        description = ET.SubElement(root, ET.QName(str(RDF), "Description"))
        description.set(ET.QName(str(RDF), "about"), str(subject))
        for predicate, value in sorted(by_subject[subject], key=lambda row: (str(row[0]), object_key(row[1]))):
            element = ET.SubElement(description, predicate_qname(predicate))
            if isinstance(value, URIRef):
                element.set(ET.QName(str(RDF), "resource"), str(value))
            elif isinstance(value, Literal):
                if value.language is not None:
                    raise PipelineError("restricted RDF/XML forbids language-tagged literals")
                if value.datatype is not None:
                    element.set(ET.QName(str(RDF), "datatype"), str(value.datatype))
                element.text = str(value)
            else:
                raise PipelineError("restricted RDF/XML object must be an IRI or literal")

    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode", short_empty_elements=True)
    root_open = body.partition(">")[0]
    declarations = "".join(
        f' xmlns:{prefix}="{namespace.replace("&", "&amp;").replace(chr(34), "&quot;")}"'
        for prefix, namespace in namespace_prefixes
        if f"xmlns:{prefix}=" not in root_open
    )
    body = body.replace("<rdf:RDF", "<rdf:RDF" + declarations, 1)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n").encode("utf-8")


def build_owl_files(card: dict, resolved: dict, output_dir: Path) -> dict[str, int]:
    """Build schema.owl, instances.owl, and the canonical combined ontology.owl."""
    from rdflib import Graph, Literal, Namespace, RDF, RDFS, OWL, URIRef

    index = validate_schema_card(card)
    _validate_contract(resolved, "resolved-instances.schema.json")
    _required(
        resolved,
        ("version", "ontology_iri", "entity_namespace", "schema_card_sha256", "individuals", "assertions"),
        "resolved instances",
    )
    if resolved["version"] != 1 or resolved["ontology_iri"] != card["ontology_iri"] or resolved["entity_namespace"] != card["entity_namespace"]:
        raise PipelineError("resolved instances 与 Schema Card 的 ontology identity 不一致")
    if not isinstance(resolved["individuals"], list) or not isinstance(resolved["assertions"], list):
        raise PipelineError("resolved individuals/assertions 必须是数组")
    if resolved["schema_card_sha256"] != _digest_text(_canonical_json(card) + "\n"):
        raise PipelineError("resolved instances 的 schema_card_sha256 不匹配")

    schema = Graph()
    instances = Graph()
    namespace = Namespace(card["entity_namespace"])
    for graph in (schema, instances):
        graph.bind("ont", namespace)
        graph.bind("owl", OWL)
        graph.bind("rdf", RDF)
        graph.bind("rdfs", RDFS)

    for iri, term in sorted(index["classes"].items()):
        node = URIRef(iri)
        schema.add((node, RDF.type, OWL.Class))
        schema.add((node, RDFS.label, Literal(term["label"], normalize=False)))
        schema.add((node, RDFS.comment, Literal(term["comment"], normalize=False)))
        for parent in term["superclasses"]:
            schema.add((node, RDFS.subClassOf, URIRef(parent)))
        for equivalent in term["equivalent_classes"]:
            schema.add((node, OWL.equivalentClass, URIRef(equivalent)))
        for disjoint in term["disjoint_with"]:
            schema.add((node, OWL.disjointWith, URIRef(disjoint)))

    for iri, term in sorted(index["object_properties"].items()):
        node = URIRef(iri)
        schema.add((node, RDF.type, OWL.ObjectProperty))
        schema.add((node, RDFS.label, Literal(term["label"], normalize=False)))
        schema.add((node, RDFS.comment, Literal(term["comment"], normalize=False)))
        schema.add((node, RDFS.domain, URIRef(term["domain"])))
        schema.add((node, RDFS.range, URIRef(term["range"])))
        for parent in term["subproperty_of"]:
            schema.add((node, RDFS.subPropertyOf, URIRef(parent)))
        for equivalent in term["equivalent_properties"]:
            schema.add((node, OWL.equivalentProperty, URIRef(equivalent)))
        for inverse in term["inverse_of"]:
            schema.add((node, OWL.inverseOf, URIRef(inverse)))

    for iri, term in sorted(index["datatype_properties"].items()):
        node = URIRef(iri)
        schema.add((node, RDF.type, OWL.DatatypeProperty))
        schema.add((node, RDFS.label, Literal(term["label"], normalize=False)))
        schema.add((node, RDFS.comment, Literal(term["comment"], normalize=False)))
        schema.add((node, RDFS.domain, URIRef(term["domain"])))
        schema.add((node, RDFS.range, URIRef(term["range"])))
        for parent in term["subproperty_of"]:
            schema.add((node, RDFS.subPropertyOf, URIRef(parent)))
        for equivalent in term["equivalent_properties"]:
            schema.add((node, OWL.equivalentProperty, URIRef(equivalent)))

    individual_classes: dict[str, str] = {}
    all_candidate_ids: set[str] = set()
    schema_locals = set(index["all_locals"])
    for position, individual in enumerate(resolved["individuals"]):
        if not isinstance(individual, dict):
            raise PipelineError(f"individuals[{position}] 必须是对象")
        _required(
            individual,
            ("iri", "class_iri", "label", "identity", "type_fact_id", "label_fact_id"),
            f"individuals[{position}]",
        )
        iri, class_iri = individual["iri"], individual["class_iri"]
        if not _valid_absolute_iri(iri) or not iri.startswith(card["entity_namespace"]):
            raise PipelineError(f"individual IRI 不在 entity_namespace 下: {iri}")
        local = _local_name(iri)
        if re.fullmatch(r"I_[0-9a-f]{64}", local) is None or local in schema_locals:
            raise PipelineError(f"individual 本地名无效或与 Ontology Term 冲突: {local}")
        if iri in individual_classes:
            raise PipelineError(f"重复 individual IRI: {iri}")
        if class_iri not in index["classes"]:
            raise PipelineError(f"individual class 未声明: {class_iri}")
        candidate_ids = individual["candidate_ids"]
        if candidate_ids != sorted(set(candidate_ids)) or any(item in all_candidate_ids for item in candidate_ids):
            raise PipelineError(f"individual candidate_ids 不唯一或未按 canonical order: individuals[{position}]")
        all_candidate_ids.update(candidate_ids)
        identity = individual["identity"]
        business_identifier = individual.get("business_identifier")
        if identity["kind"] == "business":
            if not isinstance(business_identifier, dict):
                raise PipelineError(f"business identity 缺少 business_identifier: individuals[{position}]")
            property_iri = business_identifier["property_iri"]
            value = business_identifier["value"]
            prop = index["datatype_properties"].get(property_iri)
            if (
                prop is None
                or prop.get("identity") is not True
                or index["closure"]["datatype_property_representatives"][property_iri] != property_iri
                or not _is_subclass(class_iri, prop["domain"], index["parents"])
                or not _literal_is_valid(value, prop["range"])
                or unicodedata.normalize("NFC", value) != value
            ):
                raise PipelineError(f"business identity 与 Schema Card 不一致: individuals[{position}]")
            identity_material = ["business", property_iri, value]
        elif identity["kind"] == "source":
            source = identity["source"]
            source_path = PurePosixPath(source)
            if (
                business_identifier is not None
                or not source
                or source_path.is_absolute()
                or "\\" in source
                or any(part in {"", ".", ".."} for part in source_path.parts)
                or source_path.as_posix() != source
            ):
                raise PipelineError(f"source identity material 无效: individuals[{position}]")
            identity_material = [
                "source", source, class_iri, unicodedata.normalize("NFC", individual["label"].strip())
            ]
        else:
            raise PipelineError(f"identity kind 无效: individuals[{position}]")
        expected_iri = card["entity_namespace"] + "I_" + _digest_text(_canonical_json(identity_material))
        if iri != expected_iri:
            raise PipelineError(f"Canonical Entity identity hash 不匹配: individuals[{position}]")
        expected_type_fact_id = "fact-v1-" + _digest_text(
            _canonical_json(["fact", "type", iri, class_iri])
        )
        expected_label_fact_id = "fact-v1-" + _digest_text(
            _canonical_json(["fact", "label", iri, individual["label"]])
        )
        if (
            individual["type_fact_id"] != expected_type_fact_id
            or individual["label_fact_id"] != expected_label_fact_id
        ):
            raise PipelineError(f"Canonical Entity fact ID 不匹配: individuals[{position}]")

        aliases = individual["observed_aliases"]
        if aliases != sorted(aliases, key=lambda row: row["name"]):
            raise PipelineError(f"Observed Alias 未按 canonical order: individuals[{position}]")
        alias_names: set[str] = set()
        alias_candidate_ids: set[str] = set()
        for alias in aliases:
            alias_id = "alias-v1-" + _digest_text(_canonical_json(["alias", iri, alias["name"]]))
            if (
                alias["alias_id"] != alias_id
                or alias["name"] == individual["label"]
                or alias["name"] in alias_names
                or alias["candidate_ids"] != sorted(set(alias["candidate_ids"]))
                or len(alias["candidate_ids"]) != len(alias["evidence_records"])
                or not set(alias["candidate_ids"]).issubset(candidate_ids)
                or not alias_candidate_ids.isdisjoint(alias["candidate_ids"])
            ):
                raise PipelineError(f"Observed Alias sidecar 不一致: individuals[{position}]")
            alias_names.add(alias["name"])
            alias_candidate_ids.update(alias["candidate_ids"])
        individual_classes[iri] = class_iri
        node = URIRef(iri)
        instances.add((node, RDF.type, OWL.NamedIndividual))
        instances.add((node, RDF.type, URIRef(class_iri)))
        instances.add((node, RDFS.label, Literal(str(individual["label"]), normalize=False)))

    assertion_keys: set[tuple[str, ...]] = set()
    validated_assertions: list[dict] = []
    for position, assertion in enumerate(resolved["assertions"]):
        if not isinstance(assertion, dict):
            raise PipelineError(f"assertions[{position}] 必须是对象")
        _required(assertion, ("kind", "subject_iri", "property_iri", "fact_id"), f"assertions[{position}]")
        subject = assertion["subject_iri"]
        if subject not in individual_classes:
            raise PipelineError(f"assertion subject 未声明为 individual: {subject}")
        candidate_ids = assertion["candidate_ids"]
        if candidate_ids != sorted(set(candidate_ids)) or any(item in all_candidate_ids for item in candidate_ids):
            raise PipelineError(f"assertion candidate_ids 不唯一或未按 canonical order: assertions[{position}]")
        all_candidate_ids.update(candidate_ids)
        if assertion["kind"] == "object":
            prop = index["object_properties"].get(assertion["property_iri"])
            target = assertion.get("object_iri")
            if prop is None or target not in individual_classes:
                raise PipelineError(f"object assertion 不符合 Schema Card: assertions[{position}]")
            if not _is_subclass(individual_classes[subject], prop["domain"], index["parents"]) or not _is_subclass(
                individual_classes[target], prop["range"], index["parents"]
            ):
                raise PipelineError(f"object assertion 违反 domain/range: assertions[{position}]")
            key = ("object", subject, assertion["property_iri"], target)
            triple = (URIRef(subject), URIRef(assertion["property_iri"]), URIRef(target))
        elif assertion["kind"] == "data":
            prop = index["datatype_properties"].get(assertion["property_iri"])
            value, datatype = assertion.get("value"), assertion.get("datatype")
            if (
                prop is None
                or datatype != prop["range"]
                or not isinstance(value, str)
                or not _literal_is_valid(value, datatype)
                or not _is_subclass(individual_classes[subject], prop["domain"], index["parents"])
            ):
                raise PipelineError(f"data assertion 不符合 Schema Card: assertions[{position}]")
            key = ("data", subject, assertion["property_iri"], value, datatype)
            triple = (
                URIRef(subject), URIRef(assertion["property_iri"]),
                Literal(value, datatype=URIRef(datatype), normalize=False),
            )
        else:
            raise PipelineError(f"assertion kind 无效: {assertion['kind']}")
        if key in assertion_keys:
            raise PipelineError(f"duplicate admitted fact in resolved sidecar: assertions[{position}]")
        assertion_keys.add(key)
        fact_id = "fact-v1-" + _digest_text(_canonical_json(["fact", *key]))
        if assertion["fact_id"] != fact_id:
            raise PipelineError(f"admitted assertion fact ID 不匹配: assertions[{position}]")
        validated_assertions.append(assertion)
        instances.add(triple)

    object_representatives = index["closure"]["object_property_representatives"]
    datatype_representatives = index["closure"]["datatype_property_representatives"]
    object_edges = {tuple(edge) for edge in index["closure"]["object_subproperty_edges"]}
    datatype_edges = {tuple(edge) for edge in index["closure"]["datatype_subproperty_edges"]}
    inverse_adjacency: dict[str, set[str]] = defaultdict(set)
    for left, right in index["closure"]["inverse_pairs"]:
        inverse_adjacency[left].add(right)
        inverse_adjacency[right].add(left)

    slots: dict[tuple[str, str, str], set[tuple[str, ...]]] = defaultdict(set)
    for assertion in validated_assertions:
        kind = assertion["kind"]
        representatives = object_representatives if kind == "object" else datatype_representatives
        edges = object_edges if kind == "object" else datatype_edges
        value_key = (
            (assertion["object_iri"],)
            if kind == "object"
            else (assertion["datatype"], assertion["value"])
        )
        pending = [(representatives[assertion["property_iri"]], assertion["subject_iri"], value_key)]
        seen: set[tuple[str, str, tuple[str, ...]]] = set()
        while pending:
            property_rep, subject, semantic_value = pending.pop()
            state = (property_rep, subject, semantic_value)
            if state in seen:
                continue
            seen.add(state)
            slots[(kind, subject, property_rep)].add(semantic_value)
            pending.extend((parent, subject, semantic_value) for child, parent in edges if child == property_rep)
            if kind == "object":
                target = semantic_value[0]
                pending.extend(
                    (inverse, target, (subject,)) for inverse in inverse_adjacency[property_rep]
                )

    member_properties: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for kind, properties, representatives in (
        ("object", index["object_properties"], object_representatives),
        ("data", index["datatype_properties"], datatype_representatives),
    ):
        for iri, prop in properties.items():
            member_properties[(kind, representatives[iri])].append(prop)
    for (kind, subject, representative), values in sorted(slots.items()):
        max_count = member_properties[(kind, representative)][0].get("max_count")
        if max_count is not None and len(values) > max_count:
            raise PipelineError(
                f"resolved sidecar violates semantic max_count: {kind} {subject} {representative}"
            )

    combined = schema + instances
    combined.bind("ont", namespace)
    combined.bind("owl", OWL)
    combined.bind("rdf", RDF)
    combined.bind("rdfs", RDFS)
    combined.add((URIRef(card["ontology_iri"]), RDF.type, OWL.Ontology))

    serialized = [
        (filename, _restricted_rdf_xml_bytes(graph, card["entity_namespace"]))
        for filename, graph in (("schema.owl", schema), ("instances.owl", instances), ("ontology.owl", combined))
    ]
    output_dir.mkdir(parents=True, exist_ok=True)
    for filename, content in serialized:
        _atomic_bytes(output_dir / filename, content)

    return {
        "classes": len(index["classes"]),
        "object_properties": len(index["object_properties"]),
        "datatype_properties": len(index["datatype_properties"]),
        "individuals": len(individual_classes),
        "assertions": len(resolved["assertions"]),
        "schema_triples": len(schema),
        "instance_triples": len(instances),
        "combined_triples": len(combined),
    }


def main() -> None:
    parser = JsonArgumentParser(description="Schema-constrained OWL TBox/ABox pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True, parser_class=JsonArgumentParser)

    resolve_parser = subparsers.add_parser("resolve", help="resolve and admit ABox candidates")
    resolve_parser.add_argument("schema_card", type=Path)
    resolve_parser.add_argument("candidates", type=Path)
    resolve_parser.add_argument("--workspace", type=Path, required=True)
    resolve_parser.add_argument("--source", type=Path, action="append", required=True)
    resolve_parser.add_argument("--output", type=Path, required=True)
    resolve_parser.add_argument("--evidence", type=Path, required=True)
    resolve_parser.add_argument("--rejections", type=Path, required=True)

    build_parser = subparsers.add_parser("build", help="build RDF/XML schema, instances, and combined ontology")
    build_parser.add_argument("schema_card", type=Path)
    build_parser.add_argument("resolved_instances", type=Path)
    build_parser.add_argument("--output-dir", type=Path, required=True)

    run_parser = subparsers.add_parser("run", help="manage a recoverable Ontology Project run")
    run_subparsers = run_parser.add_subparsers(dest="run_command", required=True, parser_class=JsonArgumentParser)

    start_parser = run_subparsers.add_parser("start", help="create project identity and start one Full Rebuild")
    start_parser.add_argument("--workspace", type=Path, required=True)
    start_parser.add_argument("--output", "--output-dir", dest="output_dir", type=Path, required=True)
    start_parser.add_argument("--source", type=Path, action="append", required=True)

    status_parser = run_subparsers.add_parser("status", help="read persisted run status")
    status_parser.add_argument("--output", "--output-dir", dest="output_dir", type=Path, required=True)
    status_parser.add_argument("--json", action="store_true", help="emit the machine-readable envelope")

    resume_parser = run_subparsers.add_parser("resume", help="resume a trusted staging run")
    resume_parser.add_argument("--output", "--output-dir", dest="output_dir", type=Path, required=True)
    resume_parser.add_argument("--workspace", type=Path)
    resume_parser.add_argument("--source", type=Path, action="append")

    submit_parser = run_subparsers.add_parser("submit", help="submit one Semantic Work Item result")
    submit_parser.add_argument("--output", "--output-dir", dest="output_dir", type=Path, required=True)
    submit_parser.add_argument("--work-item-id", required=True)
    submit_parser.add_argument("--input-digest", required=True)
    submit_parser.add_argument("--result", type=Path, required=True)

    abort_parser = run_subparsers.add_parser("abort", help="publish an orchestration-aborted failed attempt")
    abort_parser.add_argument("--output", "--output-dir", dest="output_dir", type=Path, required=True)

    try:
        args = parser.parse_args()
    except RunLifecycleError as exc:
        command = " ".join(sys.argv[1:3]) if len(sys.argv) >= 3 and sys.argv[1] == "run" else " ".join(sys.argv[1:2])
        print(_canonical_json(_rejected_response(command, exc)))
        raise SystemExit(exc.exit_code) from exc
    try:
        if args.command == "run":
            if args.run_command == "start":
                output_dir = _ensure_directory(
                    args.output_dir, "output-dir", create=True, writable=True
                )
            else:
                output_dir = _ensure_directory(args.output_dir, "output-dir")
            paths = _project_paths(output_dir)
            with _project_transaction(paths):
                if args.run_command == "start":
                    response = _run_start(args)
                elif args.run_command == "status":
                    response = _run_status(args)
                elif args.run_command == "resume":
                    response = _run_resume(args)
                elif args.run_command == "submit":
                    response = _run_submit(args)
                else:
                    response = _run_abort(args)
            response.setdefault("command", f"run {args.run_command}")
            response.setdefault("accepted", response.get("status") in {"accepted", "ok"})
            if "data" in response:
                data = response["data"]
                response.setdefault("run_state", data.get("run_state"))
                response.setdefault("delivery_status", data.get("delivery_status"))
                response.setdefault("pending_work_items", data.get("pending_work", []))
            response.setdefault("error_code", None)
            print(_canonical_json(response))
            return

        card = _read_json(args.schema_card)
        if args.command == "resolve":
            candidates = _read_json(args.candidates)
            resolved, evidence, rejections = resolve_candidates(card, candidates, args.workspace, args.source)
            _write_json(args.output, resolved)
            _write_jsonl(args.evidence, evidence)
            _write_jsonl(args.rejections, rejections)
            print(
                json.dumps(
                    {
                        "status": "success",
                        "individuals": len(resolved["individuals"]),
                        "assertions": len(resolved["assertions"]),
                        "rejections": len(rejections),
                    },
                    ensure_ascii=False,
                )
            )
        else:
            resolved = _read_json(args.resolved_instances)
            stats = build_owl_files(card, resolved, args.output_dir)
            print(json.dumps({"status": "success", **stats}, ensure_ascii=False))
    except RunLifecycleError as exc:
        command = f"run {args.run_command}" if getattr(args, "command", None) == "run" else str(args.command)
        print(_canonical_json(_rejected_response(command, exc)))
        raise SystemExit(exc.exit_code) from exc
    except OSError as exc:
        if getattr(args, "command", None) == "run":
            error = RunLifecycleError("persistence_failed", "无法可靠持久化运行状态", 5)
            print(_canonical_json(_rejected_response(f"run {args.run_command}", error)))
            print(str(exc), file=sys.stderr)
            raise SystemExit(error.exit_code) from exc
        raise
    except PipelineError as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
