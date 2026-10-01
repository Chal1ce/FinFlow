"""Deterministic cleaning rules for PaddleOCR block output.

The OCR layer keeps raw parsing results untouched.  This module turns those
blocks into a governed Markdown document and a structured block list that can
be rebuilt whenever the cleaning rules change.
"""

from __future__ import annotations

import html.parser
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable


DEFAULT_DROP_BLOCK_LABELS = frozenset(
    {"header", "footer", "number", "image", "chart", "vision_footnote"}
)

CONTENT_TYPE_BY_LABEL = {
    "header": "noise",
    "footer": "noise",
    "number": "noise",
    "vision_footnote": "noise",
    "doc_title": "heading",
    "abstract": "paragraph",
    "paragraph_title": "heading",
    "table": "table",
    "formula": "formula",
    "image": "figure",
    "chart": "figure",
    "figure_title": "caption",
    "reference": "reference",
    "footnote": "footnote",
    "algorithm": "algorithm",
    "text": "paragraph",
}

# OCR frequently removes the space between a long English word and the
# following short function word.  The pattern only fires when the preceding
# word is long enough that false positives are unlikely.
MERGE_FIX_PATTERN = re.compile(
    r"(?i)(?<![a-z])"
    r"([a-z]{4,}(?:ment|ance|tion|sion|ness|ing|ies|es))"
    r"((?:of|the|and|to|in|on|for|with|by|from|their|our|its|a|an){1,6})"
)

COMMON_ARTICLE_MERGE_FIXES = {
    "theconcept": "the concept",
    "thestrategies": "the strategies",
    "theestablishment": "the establishment",
    "thecompany": "the company",
    "thefinancial": "the financial",
    "theimpact": "the impact",
    "thecurrent": "the current",
    "thefuture": "the future",
    "ofour": "of our",
    "ofthe": "of the",
    "andthe": "and the",
    "inthe": "in the",
    "forthe": "for the",
    "tothe": "to the",
    "bythe": "by the",
    "fromthe": "from the",
    "withthe": "with the",
    "arenot": "are not",
    "isnot": "is not",
}

IMAGE_LINK_PATTERN = re.compile(r"!\[[^\]]*\]\([^)]*\)")
REFERENCE_SECTION_PATTERN = re.compile(
    r"""(?ix)
    ^\s*
    (?:\#{1,6}\s+)?
    (?:
        第\s*[0-9一二三四五六七八九十百零〇]+\s*(?:章|节)?\s* |
        [0-9一二三四五六七八九十百零〇]+(?:\s*\.\s*[0-9]+)*\s*[\.、)）]?\s*
    )?
    (?:参考文献|参考资料|references?|bibliography)
    \s*[:：]?\s*$
    """
)


@dataclass(frozen=True)
class Block:
    """One cleaned OCR block with page and parsing provenance."""

    page: int | None
    block_index: int
    block_label: str
    content_type: str
    text: str
    bbox: tuple[float, ...] | None = None

    def as_mapping(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "block_index": self.block_index,
            "block_label": self.block_label,
            "content_type": self.content_type,
            "text": self.text,
            "bbox": list(self.bbox) if self.bbox else None,
        }


@dataclass(frozen=True)
class CleaningResult:
    """Cleaned blocks plus deterministic statistics for lineage."""

    blocks: list[Block]
    stats: dict[str, int]
    steps: list[str]


@dataclass(frozen=True)
class ReferenceSectionResult:
    """Document-level removal result for a trailing reference section."""

    blocks: list[Block]
    detected: bool
    removed_blocks: int
    removed_chars: int


class _TableParser(html.parser.HTMLParser):
    """Parse PaddleOCR HTML table blocks into row/cell lists."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._current_row: list[str] = []
        self._cell: list[str] = []
        self._in_cell = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"td", "th"}:
            self._cell = []
            self._in_cell = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"}:
            self._current_row.append(" ".join("".join(self._cell).split()))
            self._in_cell = False
        elif tag == "tr":
            if self._current_row:
                self.rows.append(self._current_row)
                self._current_row = []

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell.append(data)


def normalize_text(value: str) -> str:
    """Normalize Unicode and OCR whitespace without destroying Markdown lines."""

    text = unicodedata.normalize("NFKC", value)
    text = text.replace("\u00a0", " ").replace("\u200b", "").replace("\ufeff", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def html_table_to_markdown(value: str) -> str:
    """Convert a PaddleOCR HTML table block to a stable Markdown table."""

    parser = _TableParser()
    parser.feed(value)
    rows = [row for row in parser.rows if any(cell.strip() for cell in row)]
    if not rows:
        return normalize_text(value)

    column_count = max(len(row) for row in rows)
    lines: list[str] = []
    for row_index, row in enumerate(rows):
        cells = [row[index] if index < len(row) else "" for index in range(column_count)]
        line = "| " + " | ".join(cell.replace("|", "\\|") for cell in cells) + " |"
        lines.append(line)
        if row_index == 0:
            lines.append("| " + " | ".join(["---"] * column_count) + " |")
    return "\n".join(lines)


def content_type_for(block_label: str) -> str:
    return CONTENT_TYPE_BY_LABEL.get(block_label, "paragraph")


def heading_level_for(block: Block) -> int:
    """Infer Markdown heading level from the OCR block label and title text."""

    text = block.text.strip()
    match = re.match(r"^(#{1,6})\s+", text)
    if match:
        return len(match.group(1))
    if block.block_label == "doc_title":
        return 1
    if block.block_label == "abstract":
        return 2
    if block.block_label == "paragraph_title" and re.match(
        r"^\d+(\.\d+)*\.?\s", text
    ):
        return 3
    return 2


def apply_ocr_merge_fixes(text: str) -> str:
    """Repair the most common OCR word-join patterns conservatively."""

    text = MERGE_FIX_PATTERN.sub(r"\1 \2", text)
    for source, target in COMMON_ARTICLE_MERGE_FIXES.items():
        text = text.replace(source, target)
    return text


def clean_block_text(text: str, block_label: str) -> str:
    """Normalize one block and convert structured blocks to readable text."""

    text = normalize_text(text)
    if block_label == "table" and "<table" in text.lower():
        return html_table_to_markdown(text)
    if block_label != "formula":
        text = IMAGE_LINK_PATTERN.sub("", text)
        text = apply_ocr_merge_fixes(text)
    return text.strip()


def is_noise_line(text: str) -> bool:
    """Return True for lines that are clearly OCR artifacts, not content."""

    if not text.strip():
        return True
    alnum_count = sum(character.isalnum() for character in text)
    if alnum_count == 0:
        return True
    if alnum_count / len(text) < 0.35:
        return True
    if re.fullmatch(r"[\W_]+", text):
        return True
    words = re.findall(r"[A-Za-z]+", text)
    consonant_clusters = [
        cluster
        for word in words
        for cluster in re.findall(r"[bcdfghjklmnpqrstvwxyz]{4,}", word.lower())
    ]
    max_word = max((len(word) for word in words), default=0)
    if max_word >= 30:
        return True
    if len(consonant_clusters) >= 2 and max_word >= 20:
        return True
    return False


def clean_blocks(
    blocks: Iterable[Block],
    drop_labels: Iterable[str] = DEFAULT_DROP_BLOCK_LABELS,
) -> CleaningResult:
    """Apply the deterministic cleaning rule set and return reproducible stats."""

    blocks = list(blocks)
    drop_set = set(drop_labels)
    kept: list[Block] = []
    dropped_by_label = 0
    empty_or_noise = 0
    for block in blocks:
        if block.block_label in drop_set:
            dropped_by_label += 1
            continue
        text = clean_block_text(block.text, block.block_label)
        if not text:
            empty_or_noise += 1
            continue
        if block.block_label not in {"table", "formula"} and is_noise_line(text):
            empty_or_noise += 1
            continue
        kept.append(
            Block(
                page=block.page,
                block_index=block.block_index,
                block_label=block.block_label,
                content_type=content_type_for(block.block_label),
                text=text,
                bbox=block.bbox,
            )
        )

    deduplicated: list[Block] = []
    seen: set[str] = set()
    for block in kept:
        key = block.text.strip().lower()
        if block.content_type in {"paragraph", "reference", "footnote", "algorithm"}:
            if key in seen:
                continue
            seen.add(key)
        deduplicated.append(block)

    stats = {
        "input_blocks": len(blocks),
        "kept_blocks": len(deduplicated),
        "dropped_by_label": dropped_by_label,
        "empty_or_noise": empty_or_noise,
        "duplicate_removed": len(kept) - len(deduplicated),
        "char_count": sum(len(block.text) for block in deduplicated),
    }
    return CleaningResult(
        blocks=deduplicated,
        stats=stats,
        steps=[
            "normalize_unicode",
            "drop_noise_labels",
            "convert_html_tables",
            "repair_ocr_word_joins",
            "remove_noise_lines",
            "deduplicate_blocks",
        ],
    )


def is_reference_section_heading(text: str) -> bool:
    """Recognize a bibliography section heading after OCR normalization.

    This intentionally matches only a complete heading, so prose that merely
    mentions references is kept.  It does not depend on Paddle's block label:
    OCR commonly emits section titles as ordinary text blocks.
    """

    return bool(REFERENCE_SECTION_PATTERN.fullmatch(normalize_text(text)))


def drop_reference_section(blocks: Iterable[Block]) -> ReferenceSectionResult:
    """Drop a trailing bibliography section while retaining raw OCR elsewhere.

    Bibliographies are document-tail material in this corpus.  Once their
    heading is found, every following governed block belongs to that section
    and is removed before chunking.  The result records what was excluded for
    reproducible governance statistics and lineage.
    """

    block_list = list(blocks)
    for index, block in enumerate(block_list):
        if (
            not is_reference_section_heading(block.text)
            and block.block_label != "reference"
        ):
            continue
        removed = block_list[index:]
        return ReferenceSectionResult(
            blocks=block_list[:index],
            detected=True,
            removed_blocks=len(removed),
            removed_chars=sum(len(item.text) for item in removed),
        )
    return ReferenceSectionResult(
        blocks=block_list,
        detected=False,
        removed_blocks=0,
        removed_chars=0,
    )


def render_governed_markdown(blocks: Iterable[Block]) -> str:
    """Render cleaned blocks into a single governed Markdown document."""

    parts: list[str] = []
    for block in blocks:
        text = block.text.strip()
        if block.content_type == "heading" and not text.startswith("#"):
            level = heading_level_for(block)
            text = f"{'#' * level} {text}"
        parts.append(text)
    return "\n\n".join(parts)


__all__ = [
    "Block",
    "CleaningResult",
    "DEFAULT_DROP_BLOCK_LABELS",
    "ReferenceSectionResult",
    "apply_ocr_merge_fixes",
    "clean_block_text",
    "clean_blocks",
    "content_type_for",
    "drop_reference_section",
    "heading_level_for",
    "html_table_to_markdown",
    "is_reference_section_heading",
    "is_noise_line",
    "normalize_text",
    "render_governed_markdown",
]
