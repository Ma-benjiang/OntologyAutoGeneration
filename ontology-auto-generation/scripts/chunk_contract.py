"""Deterministic dual-view Markdown chunk manifests."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from typing import Literal


View = Literal["tbox", "abox"]
CONTRACT_VERSION = 1


@dataclass(frozen=True)
class ChunkPolicy:
    target_chars: int
    hard_max_chars: int
    overlap_chars: int
    hard_heading_levels: tuple[int, ...]

    def __post_init__(self) -> None:
        if not 0 <= self.overlap_chars < self.target_chars <= self.hard_max_chars:
            raise ValueError("expected 0 <= overlap < target <= hard max")


@dataclass(frozen=True)
class Block:
    kind: str
    line_start: int
    line_end: int
    text: str
    heading_path: tuple[str, ...]
    heading_level: int | None = None
    protected: bool = False
    warning_codes: tuple[str, ...] = ()


DEFAULT_POLICIES: dict[View, ChunkPolicy] = {
    "tbox": ChunkPolicy(12_000, 16_000, 800, ()),
    "abox": ChunkPolicy(4_000, 6_000, 400, (1, 2)),
}

ATX_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*(?:\n)?$")
SETEXT_UNDERLINE = re.compile(r"^ {0,3}(=+|-+)[ \t]*(?:\n)?$")
FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})")
TABLE_DIVIDER = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*(?:\n)?$")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_markdown(markdown: str) -> str:
    if markdown.startswith("\ufeff"):
        markdown = markdown[1:]
    return markdown.replace("\r\n", "\n").replace("\r", "\n")


def canonical_source_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value or "\\" in value or path.is_absolute() or ".." in path.parts:
        raise ValueError("source_path must be a workspace-relative POSIX path")
    normalized = path.as_posix()
    if normalized in (".", "") or normalized != value:
        raise ValueError("source_path must already be canonical")
    return normalized


def _is_table_start(lines: list[str], index: int) -> bool:
    return index + 1 < len(lines) and "|" in lines[index] and TABLE_DIVIDER.match(lines[index + 1]) is not None


def parse_blocks(markdown: str) -> list[Block]:
    """Return one complete, ordered and non-overlapping block stream."""
    lines = markdown.splitlines(keepends=True)
    blocks: list[Block] = []
    headings: list[str] = []
    index = 0

    def add(
        kind: str,
        start: int,
        end: int,
        path: tuple[str, ...],
        *,
        protected: bool = False,
        heading_level: int | None = None,
        warning_codes: tuple[str, ...] = (),
    ) -> None:
        blocks.append(
            Block(
                kind,
                start + 1,
                end,
                "".join(lines[start:end]),
                path,
                heading_level,
                protected,
                warning_codes,
            )
        )

    while index < len(lines):
        fence = FENCE_OPEN.match(lines[index])
        if fence:
            marker = fence.group(1)
            close = re.compile(rf"^ {{0,3}}{re.escape(marker[0])}{{{len(marker)},}}[ \t]*(?:\n)?$")
            end = index + 1
            while end < len(lines) and close.match(lines[end]) is None:
                end += 1
            closed = end < len(lines)
            if closed:
                end += 1
            add(
                "fenced_code",
                index,
                end,
                tuple(headings),
                protected=True,
                warning_codes=() if closed else ("UNCLOSED_FENCE",),
            )
            index = end
            continue

        atx = ATX_HEADING.match(lines[index])
        if atx:
            level = len(atx.group(1))
            headings = headings[: level - 1]
            headings.append(atx.group(2).strip().rstrip("#").rstrip())
            add("heading", index, index + 1, tuple(headings), heading_level=level)
            index += 1
            continue

        if index + 1 < len(lines) and lines[index].strip() and SETEXT_UNDERLINE.match(lines[index + 1]):
            level = 1 if lines[index + 1].lstrip().startswith("=") else 2
            headings = headings[: level - 1]
            headings.append(lines[index].strip())
            add("heading", index, index + 2, tuple(headings), heading_level=level)
            index += 2
            continue

        if _is_table_start(lines, index):
            end = index + 2
            while end < len(lines) and "|" in lines[end] and lines[end].strip():
                end += 1
            add("table", index, end, tuple(headings), protected=True)
            index = end
            continue

        if not lines[index].strip():
            end = index + 1
            while end < len(lines) and not lines[end].strip():
                end += 1
            add("blank", index, end, tuple(headings))
            index = end
            continue

        end = index + 1
        while end < len(lines):
            if not lines[end].strip() or FENCE_OPEN.match(lines[end]) or ATX_HEADING.match(lines[end]):
                break
            if _is_table_start(lines, end):
                break
            if end + 1 < len(lines) and SETEXT_UNDERLINE.match(lines[end + 1]):
                break
            end += 1
        add("prose", index, end, tuple(headings))
        index = end

    return blocks


def _split_oversized_prose(blocks: list[Block], hard_max_chars: int) -> list[Block]:
    result: list[Block] = []
    for block in blocks:
        if block.kind != "prose" or len(block.text) <= hard_max_chars:
            result.append(block)
            continue
        current: list[str] = []
        current_chars = 0
        current_start = block.line_start
        line_number = block.line_start

        def flush() -> None:
            nonlocal current, current_chars
            if current:
                result.append(
                    Block("prose", current_start, current_start + len(current) - 1, "".join(current), block.heading_path)
                )
                current = []
                current_chars = 0

        for line in block.text.splitlines(keepends=True):
            if current and current_chars + len(line) > hard_max_chars:
                flush()
                current_start = line_number
            current.append(line)
            current_chars += len(line)
            if len(line) > hard_max_chars:
                flush()
                current_start = line_number + 1
            line_number += 1
        flush()
    return result


def _units(blocks: list[Block]) -> list[list[Block]]:
    result: list[list[Block]] = []
    index = 0
    while index < len(blocks):
        block = blocks[index]
        if block.kind != "heading":
            result.append([block])
            index += 1
            continue
        unit = [block]
        index += 1
        while index < len(blocks) and blocks[index].kind == "blank":
            unit.append(blocks[index])
            index += 1
        if index < len(blocks) and blocks[index].kind != "heading":
            unit.append(blocks[index])
            index += 1
        result.append(unit)
    return result


def _pack_primary(blocks: list[Block], policy: ChunkPolicy) -> list[list[Block]]:
    groups: list[list[Block]] = []
    current: list[Block] = []
    current_chars = 0
    for unit in _units(blocks):
        size = sum(len(block.text) for block in unit)
        heading_level = unit[0].heading_level if unit[0].kind == "heading" else None
        protected_fits = any(block.protected for block in unit) and current_chars + size <= policy.hard_max_chars
        if current and (
            heading_level in policy.hard_heading_levels
            or (current_chars + size > policy.target_chars and not protected_fits)
        ):
            trailing_blanks: list[Block] = []
            if heading_level is not None:
                while current and current[-1].kind == "blank":
                    trailing_blanks.insert(0, current.pop())
            if current:
                groups.append(current)
            current = trailing_blanks
            current_chars = sum(len(block.text) for block in current)
        current.extend(unit)
        current_chars += size
    if current:
        groups.append(current)
    return groups


def _context_suffix(previous: list[Block], budget: int) -> list[Block]:
    selected: list[Block] = []
    size = 0
    for block in reversed(previous):
        if size + len(block.text) > budget:
            break
        selected.append(block)
        size += len(block.text)
    return list(reversed(selected))


def _segment(block: Block) -> dict:
    return {
        "kind": block.kind,
        "heading_level": block.heading_level,
        "heading_path": list(block.heading_path),
        "line_start": block.line_start,
        "line_end": block.line_end,
        "protected": block.protected,
        "text_sha256": _digest(block.text),
    }


def _region(blocks: list[Block]) -> dict | None:
    if not blocks:
        return None
    return {
        "line_start": blocks[0].line_start,
        "line_end": blocks[-1].line_end,
        "text": "".join(block.text for block in blocks),
        "segments": [_segment(block) for block in blocks],
    }


def chunk_document(source_path: str, markdown: str, view: View, policy: ChunkPolicy | None = None) -> dict:
    source_path = canonical_source_path(source_path)
    markdown = normalize_markdown(markdown)
    if view not in DEFAULT_POLICIES:
        raise ValueError("view must be 'tbox' or 'abox'")
    policy = policy or DEFAULT_POLICIES[view]
    warnings = [] if markdown else [{"code": "EMPTY_SOURCE"}]
    blocks = _split_oversized_prose(parse_blocks(markdown), policy.hard_max_chars) if markdown else []
    groups = _pack_primary(blocks, policy)
    identity_counts: dict[str, int] = {}
    chunks: list[dict] = []

    for ordinal, primary_blocks in enumerate(groups, start=1):
        context_blocks = _context_suffix(groups[ordinal - 2], policy.overlap_chars) if ordinal > 1 else []
        primary = _region(primary_blocks)
        context = _region(context_blocks)
        assert primary is not None
        heading_path = next(
            (list(block.heading_path) for block in primary_blocks if block.kind != "blank"),
            list(primary_blocks[0].heading_path),
        )
        identity = _digest(
            json.dumps([CONTRACT_VERSION, source_path, view, heading_path, primary["text"]], ensure_ascii=False, separators=(",", ":"))
        )
        occurrence = identity_counts.get(identity, 0) + 1
        identity_counts[identity] = occurrence
        chunk_warnings: list[dict] = []
        for block in primary_blocks:
            chunk_warnings.extend(
                {"code": code, "line_start": block.line_start, "line_end": block.line_end}
                for code in block.warning_codes
            )
            if len(block.text) > policy.hard_max_chars:
                chunk_warnings.append(
                    {
                        "code": "PROTECTED_BLOCK_OVERSIZE" if block.protected else "SOURCE_LINE_OVERSIZE",
                        "line_start": block.line_start,
                        "line_end": block.line_end,
                        "actual_chars": len(block.text),
                        "hard_max_chars": policy.hard_max_chars,
                    }
                )
        payload = json.dumps(
            {"policy": asdict(policy), "context_before": context, "primary": primary},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        chunks.append(
            {
                "chunk_id": f"chunk-v{CONTRACT_VERSION}-{view}-{identity[:20]}-{occurrence}",
                "payload_sha256": _digest(payload),
                "ordinal": ordinal,
                "source_path": source_path,
                "view": view,
                "heading_path": heading_path,
                "context_before": context,
                "primary": primary,
                "warnings": chunk_warnings,
            }
        )

    return {
        "contract_version": CONTRACT_VERSION,
        "view": view,
        "units": "unicode_code_points",
        "policy": asdict(policy),
        "warnings": warnings,
        "source": {
            "path": source_path,
            "sha256": _digest(markdown),
            "line_count": len(markdown.splitlines()),
            "normalization": ["strip_one_leading_utf8_bom", "crlf_cr_to_lf"],
        },
        "chunks": chunks,
    }


def chunk_source_bytes(source_path: str, source_bytes: bytes, view: View, policy: ChunkPolicy | None = None) -> dict:
    return chunk_document(source_path, source_bytes.decode("utf-8", errors="strict"), view, policy)
