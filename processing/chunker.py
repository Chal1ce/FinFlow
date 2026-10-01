"""Semantic chunking for governed OCR content.

Chunks follow document structure instead of fixed character counts: headings
provide context, tables stay independent, formulas stay close to their
paragraph, and oversized sections are split at sentence boundaries.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Mapping

from core.ids import chunk_version_uid, logical_chunk_uid
from processing.cleaners import Block, heading_level_for


SENTENCE_SPLIT_PATTERN = re.compile(r"(?<=[.;。；!?！？])\s+")


@dataclass(frozen=True)
class Chunk:
    """One self-contained, traceable content chunk."""

    chunk_id: str
    chunk_version_uid: str
    governed_uid: str
    document_uid: str
    work_uid: str
    asset_uid: str
    backend: str
    chunk_index: int
    page: int | None
    pages: tuple[int, ...]
    content_type: str
    title_context: str
    text: str
    bboxes: tuple[tuple[float, ...], ...]
    start_block: int
    end_block: int
    char_start: int
    char_end: int
    block_count: int

    @property
    def chunk_uid(self) -> str:
        """Backward-compatible alias for the materialized chunk version ID."""

        return self.chunk_version_uid

    def as_mapping(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "chunk_version_uid": self.chunk_version_uid,
            "chunk_uid": self.chunk_uid,
            "governed_uid": self.governed_uid,
            "document_uid": self.document_uid,
            "work_uid": self.work_uid,
            "asset_uid": self.asset_uid,
            "backend": self.backend,
            "chunk_index": self.chunk_index,
            "page": self.page,
            "pages": list(self.pages),
            "content_type": self.content_type,
            "title_context": self.title_context,
            "text": self.text,
            "bboxes": [list(bbox) for bbox in self.bboxes],
            "start_block": self.start_block,
            "end_block": self.end_block,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "block_count": self.block_count,
            "text_hash": hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:16],
        }


def _heading_level(block: Block) -> int:
    return heading_level_for(block)


def _heading_text(block: Block) -> str:
    return re.sub(r"^#{1,6}\s+", "", block.text.strip())


def _render_heading(block: Block) -> str:
    text = block.text.strip()
    if text.startswith("#"):
        return text
    return f"{'#' * _heading_level(block)} {text}"


def _positioned_blocks(blocks: list[Block]) -> tuple[list[tuple[Block, int, int]], str]:
    positioned: list[tuple[Block, int, int]] = []
    offset = 0
    for block in blocks:
        start = offset
        offset += len(block.text)
        positioned.append((block, start, offset))
        offset += 2
    document_text = "\n\n".join(block.text for block in blocks)
    return positioned, document_text


def _flush(open_chunk: dict[str, Any] | None, raw_chunks: list[dict[str, Any]]) -> None:
    if open_chunk and any(str(part).strip() for part in open_chunk["parts"]):
        raw_chunks.append(open_chunk)


def _raw_chunk(
    *,
    content_type: str,
    parts: list[str],
    page: int | None,
    start_block: int,
    end_block: int,
    start_char: int,
    end_char: int,
    title_context: str,
) -> dict[str, Any]:
    return {
        "content_type": content_type,
        "parts": parts,
        "page": page,
        "start_block": start_block,
        "end_block": end_block,
        "start_char": start_char,
        "end_char": end_char,
        "title_context": title_context,
    }


def _split_sentences(text: str) -> list[str]:
    parts = SENTENCE_SPLIT_PATTERN.split(text.strip())
    return [part.strip() for part in parts if part.strip()]


def _split_hard(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    pieces: list[str] = []
    cursor = 0
    while cursor < len(text):
        end = min(cursor + max_chars, len(text))
        if end < len(text):
            space = text.rfind(" ", cursor, end)
            if space > cursor + max_chars // 2:
                end = space
        pieces.append(text[cursor:end].strip())
        cursor = max(end, cursor + 1)
    return [piece for piece in pieces if piece]


def _split_paragraphs(text: str, max_chars: int, min_chars: int) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n{2,}", text) if part.strip()]
    if len(paragraphs) <= 1 and len(text) > max_chars:
        sentences = _split_sentences(text)
        if len(sentences) <= 1:
            return _split_hard(text, max_chars)
        pieces: list[str] = []
        current = ""
        for sentence in sentences:
            if current and len(current) + len(sentence) + 1 > max_chars:
                pieces.append(current)
                current = sentence
            else:
                current = f"{current} {sentence}".strip()
        if current:
            pieces.append(current)
        return [piece for piece in pieces if piece]

    pieces: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(paragraph) > max_chars:
            sub_limit = max(min_chars, max_chars - len(current) - 2) if current else max_chars
            sub_pieces = _split_paragraphs(paragraph, sub_limit, min_chars)
            if current and sub_pieces:
                sub_pieces[0] = f"{current}\n\n{sub_pieces[0]}"
                current = ""
            pieces.extend(sub_pieces)
            continue
        if current and len(current) + len(paragraph) + 2 > max_chars:
            pieces.append(current)
            current = paragraph
        else:
            current = f"{current}\n\n{paragraph}".strip()
    if current:
        pieces.append(current)
    if (
        len(pieces) > 1
        and len(pieces[-1]) < min_chars
        and len(pieces[-2]) + len(pieces[-1]) + 2 <= max_chars
    ):
        pieces[-2] = f"{pieces[-2]}\n\n{pieces[-1]}"
        pieces.pop()
    return pieces


def _split_table(text: str, max_chars: int) -> list[str]:
    lines = text.splitlines()
    if len(lines) <= 3:
        return [text]
    header = lines[:2]
    body = lines[2:]
    rows_per_piece = max(5, min(30, max_chars // 120))
    pieces: list[str] = []
    for index in range(0, len(body), rows_per_piece):
        pieces.append("\n".join(header + body[index : index + rows_per_piece]))
    return pieces


def _split_reference(text: str, max_chars: int) -> list[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    pieces: list[str] = []
    current = ""
    for line in lines:
        if current and len(current) + len(line) + 1 > max_chars:
            pieces.append(current)
            current = line
        else:
            current = f"{current}\n{line}".strip()
    if current:
        pieces.append(current)
    return pieces


def _split_raw_chunk(raw: Mapping[str, Any], max_chars: int, min_chars: int) -> list[dict[str, Any]]:
    parts = list(raw["parts"])
    text = "\n\n".join(parts)
    if len(text) <= max_chars:
        return [{"text": text, "raw": raw}]

    content_type = raw["content_type"]
    if content_type == "table":
        pieces = _split_table(text, max_chars)
    elif content_type == "reference":
        pieces = _split_reference(text, max_chars)
    else:
        pieces = _split_paragraphs(text, max_chars, min_chars)
    return [{"text": piece, "raw": raw} for piece in pieces if piece.strip()]


def chunk_blocks(
    blocks: list[Block],
    *,
    governed_uid: str,
    document_uid: str,
    work_uid: str,
    asset_uid: str,
    backend: str,
    max_chars: int = 2000,
    min_chars: int = 80,
) -> list[Chunk]:
    """Build structure-aware chunks from cleaned blocks."""

    positioned, _document_text = _positioned_blocks(blocks)
    heading_stack: list[tuple[int, str]] = []
    raw_chunks: list[dict[str, Any]] = []
    open_chunk: dict[str, Any] | None = None
    open_heading_level: int | None = None

    for block, start_char, end_char in positioned:
        content_type = block.content_type
        if content_type == "heading":
            level = _heading_level(block)
            title = _heading_text(block)
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))

            if (
                open_chunk is not None
                and open_chunk["content_type"] == "section"
                and len(open_chunk["parts"]) == 1
                and open_heading_level is not None
            ):
                if level > open_heading_level:
                    open_chunk["parts"].append(_render_heading(block))
                    open_chunk["end_block"] = block.block_index
                    open_chunk["end_char"] = end_char
                    open_chunk["title_context"] = " > ".join(
                        title for _level, title in heading_stack
                    )
                    open_heading_level = level
                    continue
                open_chunk = None

            _flush(open_chunk, raw_chunks)
            open_chunk = _raw_chunk(
                content_type="section",
                parts=[_render_heading(block)],
                page=block.page,
                start_block=block.block_index,
                end_block=block.block_index,
                start_char=start_char,
                end_char=end_char,
                title_context=" > ".join(title for _level, title in heading_stack),
            )
            open_heading_level = level
            continue

        if content_type == "table":
            _flush(open_chunk, raw_chunks)
            open_chunk = None
            open_heading_level = None
            raw_chunks.append(
                _raw_chunk(
                    content_type="table",
                    parts=[block.text],
                    page=block.page,
                    start_block=block.block_index,
                    end_block=block.block_index,
                    start_char=start_char,
                    end_char=end_char,
                    title_context=" > ".join(title for _level, title in heading_stack),
                )
            )
            continue

        if content_type in {"figure", "caption"}:
            if (
                open_chunk is not None
                and open_chunk["content_type"] == "section"
                and len(open_chunk["parts"]) == 1
            ):
                open_chunk["parts"].append(block.text)
                open_chunk["end_block"] = block.block_index
                open_chunk["end_char"] = end_char
                continue
            _flush(open_chunk, raw_chunks)
            open_chunk = None
            open_heading_level = None
            raw_chunks.append(
                _raw_chunk(
                    content_type=content_type,
                    parts=[block.text],
                    page=block.page,
                    start_block=block.block_index,
                    end_block=block.block_index,
                    start_char=start_char,
                    end_char=end_char,
                    title_context=" > ".join(title for _level, title in heading_stack),
                )
            )
            continue

        if content_type == "formula":
            if (
                open_chunk is not None
                and open_chunk["content_type"] == "section"
                and sum(len(part) for part in open_chunk["parts"]) + len(block.text) <= max_chars
            ):
                open_chunk["parts"].append(block.text)
                open_chunk["end_block"] = block.block_index
                open_chunk["end_char"] = end_char
                continue
            _flush(open_chunk, raw_chunks)
            open_chunk = None
            open_heading_level = None
            raw_chunks.append(
                _raw_chunk(
                    content_type="formula",
                    parts=[block.text],
                    page=block.page,
                    start_block=block.block_index,
                    end_block=block.block_index,
                    start_char=start_char,
                    end_char=end_char,
                    title_context=" > ".join(title for _level, title in heading_stack),
                )
            )
            continue

        if content_type == "reference":
            if open_chunk is None or open_chunk["content_type"] != "reference":
                _flush(open_chunk, raw_chunks)
                open_chunk = _raw_chunk(
                    content_type="reference",
                    parts=[],
                    page=block.page,
                    start_block=block.block_index,
                    end_block=block.block_index,
                    start_char=start_char,
                    end_char=end_char,
                    title_context=" > ".join(title for _level, title in heading_stack),
                )
            open_chunk["parts"].append(block.text)
            open_chunk["end_block"] = block.block_index
            open_chunk["end_char"] = end_char
            continue

        if open_chunk is None:
            open_chunk = _raw_chunk(
                content_type="section",
                parts=[],
                page=block.page,
                start_block=block.block_index,
                end_block=block.block_index,
                start_char=start_char,
                end_char=end_char,
                title_context=" > ".join(title for _level, title in heading_stack),
            )
        elif open_chunk["content_type"] == "reference":
            _flush(open_chunk, raw_chunks)
            open_chunk = _raw_chunk(
                content_type="section",
                parts=[],
                page=block.page,
                start_block=block.block_index,
                end_block=block.block_index,
                start_char=start_char,
                end_char=end_char,
                title_context=" > ".join(title for _level, title in heading_stack),
            )
        open_chunk["parts"].append(block.text)
        open_chunk["end_block"] = block.block_index
        open_chunk["end_char"] = end_char

    _flush(open_chunk, raw_chunks)

    chunks: list[Chunk] = []
    identity_occurrences: dict[tuple[str, str, str], int] = {}
    for raw in raw_chunks:
        raw_text = "\n\n".join(raw["parts"])
        pieces = _split_raw_chunk(raw, max_chars, min_chars)
        cursor = 0
        for piece in pieces:
            piece_text = piece["text"]
            found = raw_text.find(piece_text, cursor)
            if found < 0:
                found = cursor
            char_start = raw["start_char"] + found
            char_end = char_start + len(piece_text)
            cursor = found + len(piece_text)
            chunk_index = len(chunks)
            text_hash = hashlib.sha256(piece_text.encode("utf-8")).hexdigest()[:16]
            identity_key = (raw["content_type"], raw["title_context"], text_hash)
            occurrence = identity_occurrences.get(identity_key, 0)
            identity_occurrences[identity_key] = occurrence + 1
            chunk_id = logical_chunk_uid(
                document_uid,
                raw["content_type"],
                raw["title_context"],
                text_hash,
                occurrence,
            )
            source_blocks = {
                block.block_index: block
                for block in blocks
                if raw["start_block"] <= block.block_index <= raw["end_block"]
            }
            bboxes = tuple(
                block.bbox
                for block_index in sorted(source_blocks)
                if (block := source_blocks[block_index]).bbox
            )
            pages = tuple(
                dict.fromkeys(
                    block.page
                    for block_index in sorted(source_blocks)
                    if (block := source_blocks[block_index]).page is not None
                )
            )
            chunks.append(
                Chunk(
                    chunk_id=chunk_id,
                    chunk_version_uid=chunk_version_uid(governed_uid, chunk_id),
                    governed_uid=governed_uid,
                    document_uid=document_uid,
                    work_uid=work_uid,
                    asset_uid=asset_uid,
                    backend=backend,
                    chunk_index=chunk_index,
                    page=pages[0] if pages else raw["page"],
                    pages=pages or ((raw["page"],) if raw["page"] is not None else ()),
                    content_type=raw["content_type"],
                    title_context=raw["title_context"],
                    text=piece_text,
                    bboxes=bboxes,
                    start_block=raw["start_block"],
                    end_block=raw["end_block"],
                    char_start=char_start,
                    char_end=char_end,
                    block_count=raw["end_block"] - raw["start_block"] + 1,
                )
            )
    return chunks


__all__ = ["Chunk", "chunk_blocks"]
