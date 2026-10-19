"""Inventory real OCR media without inferring block mappings or training approval."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import re
import shutil
import tempfile
import warnings
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError, __version__ as PILLOW_VERSION

from core.flywheel_files import sha256, write_json


VERSION = "finflow-ocr-inventory-v1"
VISUAL_LABELS = {"image", "chart", "table", "figure"}
IMAGE_REFERENCES = re.compile(r'!\[[^\]]*\]\(([^)]+)\)|src=["\x27]([^"\x27]+)["\x27]')
HASH = re.compile(r"^[0-9a-fA-F]{64}$")


@dataclass(frozen=True)
class InventoryOptions:
    max_documents: int | None = None
    max_pages: int | None = None
    review_limit: int = 20
    max_result_bytes: int = 268435456
    max_image_bytes: int = 10485760
    max_image_pixels: int = 40000000
    copy_media: bool = False

    def __post_init__(self):
        for key, value in asdict(self).items():
            if key != "copy_media" and value is not None and (type(value) is not int or value < 1):
                raise ValueError(f"{key} must be a positive integer")


def _identity(*values: Any) -> str:
    encoded = json.dumps(values, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _contained(root: Path, value: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("invalid_relative_path")
    value = value.replace("\\", "/")
    if ":" in value or Path(value).is_absolute() or ".." in Path(value).parts:
        raise ValueError("invalid_relative_path")
    target = (root / value).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError("path_outside_ocr_directory")
    return target


def _read_json(path: Path, limit: int):
    with path.open("rb") as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("result_byte_limit")
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def _pages(path: Path, limit: int):
    if path.suffix != ".jsonl":
        value, content_hash = _read_json(path, limit)
        container = value.get("result", value) if isinstance(value, dict) else None
        pages = container.get("layoutParsingResults") if isinstance(container, dict) else None
    else:
        if path.stat().st_size > limit:
            raise ValueError("result_byte_limit")
        pages, digest, total_bytes = [], hashlib.sha256(), 0
        with path.open("rb") as handle:
            for raw in handle:
                total_bytes += len(raw)
                if total_bytes > limit:
                    raise ValueError("result_byte_limit")
                digest.update(raw)
                if raw.strip():
                    value = json.loads(raw)
                    container = value.get("result", value) if isinstance(value, dict) else None
                    batch = container.get("layoutParsingResults") if isinstance(container, dict) else None
                    if not isinstance(batch, list):
                        raise ValueError("invalid_layout_pages")
                    pages.extend(batch)
        content_hash = digest.hexdigest()
    if not isinstance(pages, list) or not pages or any(not isinstance(page, dict) for page in pages):
        raise ValueError("invalid_layout_pages")
    return pages, content_hash


def discover_results(input_path: Path) -> tuple[Path, list[Path]]:
    """Prefer current assembled results; explicit input can still select a history version."""
    input_path = input_path.resolve()
    if input_path.is_file():
        if input_path.name not in {"result.json", "result.jsonl"}:
            raise ValueError("input must be an OCR directory or result.json/result.jsonl")
        return input_path.parent, [input_path]
    if not input_path.is_dir():
        raise ValueError("input directory does not exist")
    direct = [input_path / name for name in ("result.json", "result.jsonl") if (input_path / name).is_file()]
    if direct:
        return input_path, direct[:1]
    by_directory = {}
    for name in ("result.jsonl", "result.json"):
        for path in sorted(input_path.rglob(name)):
            relative = path.relative_to(input_path)
            archived = any(part.endswith("_history") for part in relative.parts)
            if "segments" not in relative.parts and not archived and path.resolve().is_relative_to(input_path):
                by_directory[path.parent] = path
    return input_path, sorted(by_directory.values())


class OCRInventory:
    def __init__(self, staging: Path, options: InventoryOptions):
        self.staging, self.options = staging, options
        self.sources, self.media, self.occurrences, self.unmapped = [], [], [], []
        self._source_roots: dict[str, Path] = {}
        self._referenced_paths: set[tuple[str, str]] = set()
        self._review_candidates: list[tuple[str, dict]] = []

    def _inspect(self, raw: bytes, expected: str | None = None) -> dict:
        actual = hashlib.sha256(raw).hexdigest()
        result = {
            "sha256": actual,
            "bytes": len(raw),
            "expected_sha256": expected,
            "checksum_status": "unregistered" if expected is None else "match" if actual == expected else "mismatch",
            "pixel_status": "invalid",
            "width": None,
            "height": None,
            "format": None,
            "mime": None,
        }
        if expected is not None and actual != expected:
            return {**result, "issue": "checksum_mismatch"}
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(raw)) as image:
                    width, height = image.size
                    result.update(width=width, height=height, format=image.format, mime=Image.MIME.get(image.format))
                    if width < 1 or height < 1 or width * height > self.options.max_image_pixels:
                        return {**result, "issue": "image_pixel_limit"}
                    if getattr(image, "n_frames", 1) != 1:
                        return {**result, "issue": "expected_single_frame_image"}
                    image.verify()
                with Image.open(io.BytesIO(raw)) as image:
                    image.load()
        except (OSError, ValueError, SyntaxError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            return {**result, "issue": "image_decode_failed"}
        if result["format"] not in {"PNG", "JPEG", "WEBP", "TIFF", "BMP"}:
            return {**result, "issue": "unsupported_image_format"}
        return {**result, "pixel_status": "available", "issue": None}

    def _register(self, source_id, page, source_key, purpose, *, relative=None, inline=None, expected=None, origin):
        record = {
            "media_id": "media-" + _identity(source_id, page, source_key, relative, expected, origin),
            "source_id": source_id,
            "page": page,
            "source_key": source_key,
            "purpose": purpose,
            "origin": origin,
            "source_path": relative,
            "pixel_status": "unavailable",
            "checksum_status": "unregistered",
            "expected_sha256": None,
            "sha256": None,
            "bytes": None,
            "width": None,
            "height": None,
            "format": None,
            "mime": None,
            "package_path": None,
            "localization_status": "unverified",
            "training_eligible": False,
            "issue": None,
        }
        raw = None
        try:
            if expected is not None:
                if not isinstance(expected, str) or not HASH.fullmatch(expected):
                    raise ValueError("invalid_declared_checksum")
                expected = expected.lower()
                record["expected_sha256"] = expected
            if relative is not None:
                path = _contained(self._source_roots[source_id], relative)
                self._referenced_paths.add((source_id, path.relative_to(self._source_roots[source_id]).as_posix()))
                if not path.is_file():
                    raise ValueError("image_file_missing")
                with path.open("rb") as handle:
                    raw = handle.read(self.options.max_image_bytes + 1)
            elif isinstance(inline, str):
                if inline.startswith(("https://", "http://")):
                    raise ValueError("remote_image_not_fetched")
                encoded = inline.split(",", 1)[1] if inline.startswith("data:image/") and ";base64," in inline else inline
                if len(encoded) > 4 * ((self.options.max_image_bytes + 2) // 3) + 4:
                    raise ValueError("image_byte_limit")
                try:
                    raw = base64.b64decode(encoded, validate=True)
                except ValueError as exc:
                    raise ValueError("invalid_inline_image") from exc
            else:
                raise ValueError("image_payload_missing")
            if len(raw) > self.options.max_image_bytes:
                raise ValueError("image_byte_limit")
            record.update(self._inspect(raw, expected))
            if self.options.copy_media and record["pixel_status"] == "available":
                extension = {"JPEG": "jpg", "TIFF": "tiff"}.get(record["format"], record["format"].lower())
                target = self.staging / "media" / f"{record['sha256']}.{extension}"
                target.parent.mkdir(exist_ok=True)
                if not target.exists():
                    target.write_bytes(raw)
                record["package_path"] = target.relative_to(self.staging).as_posix()
        except (ValueError, OSError) as exc:
            record["issue"] = str(exc) if isinstance(exc, ValueError) else "image_io_error"
            record["pixel_status"] = "missing" if record["issue"] == "image_file_missing" else "unavailable"
        self.media.append(record)
        return record

    def _page_media(self, source_id, page_number, page):
        resources, keys = [], set()
        mapping = page.get("image_assets") or []
        if not isinstance(mapping, list):
            mapping = []
            self.sources[-1]["issues"].append("invalid_image_assets")
        for index, item in enumerate(mapping):
            if not isinstance(item, dict):
                self.sources[-1]["issues"].append("invalid_image_asset_entry")
                continue
            key = item.get("source_key")
            if not isinstance(key, str):
                key = f"invalid-source-key-{index}"
            purpose = item.get("purpose")
            if not isinstance(purpose, str) or purpose not in {"region", "original_page", "page_or_debug"}:
                purpose = "unknown"
            if item.get("page") != page_number:
                self.sources[-1]["issues"].append("image_asset_page_mismatch")
                resources.append(self._register(source_id, page_number, key, purpose, origin="invalid_page_mapping"))
                continue
            resources.append(
                self._register(
                    source_id, page_number, key, purpose, relative=item.get("path"), expected=item.get("sha256"), origin="image_assets"
                )
            )
            keys.add(key)
        if page.get("inputImage") and "inputImage" not in keys:
            resources.append(
                self._register(
                    source_id, page_number, "inputImage", "original_page", inline=page["inputImage"], origin="inline_input_image"
                )
            )
        markdown = page.get("markdown") or {}
        images = markdown.get("images") or {} if isinstance(markdown, dict) else {}
        if isinstance(images, dict):
            for key in sorted(images):
                if key in keys:
                    continue
                # Only a page-qualified exact resource key may recover a legacy disk reference.
                relative = f"images/markdown/page_{page_number:04d}/{key}"
                try:
                    disk = _contained(self._source_roots[source_id], relative)
                except ValueError:
                    disk = None
                if disk is not None and disk.is_file():
                    resources.append(
                        self._register(source_id, page_number, key, "region", relative=relative, origin="legacy_page_qualified_key")
                    )
                else:
                    resources.append(
                        self._register(source_id, page_number, key, "region", inline=images[key], origin="inline_markdown_key")
                    )
        return resources

    @staticmethod
    def _bbox(block, pruned):
        bbox = block.get("block_bbox")
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            return None, "missing"
        if any(type(v) not in {int, float} or not math.isfinite(v) for v in bbox):
            return None, "invalid"
        left, top, right, bottom = bbox
        if not (0 <= left < right and 0 <= top < bottom):
            return list(bbox), "invalid"
        width, height = pruned.get("width"), pruned.get("height")
        if type(width) in {int, float} and type(height) in {int, float} and width > 0 and height > 0:
            return list(bbox), "within_declared_bounds" if right <= width and bottom <= height else "out_of_bounds"
        return list(bbox), "bounds_unverified"

    def _page_blocks(self, source_id, page_number, page, media):
        pruned = page.get("prunedResult") or page
        blocks = pruned.get("parsing_res_list") or [] if isinstance(pruned, dict) else []
        if not isinstance(blocks, list):
            self.sources[-1]["issues"].append("invalid_layout_blocks")
            return
        parent_ids = [m["media_id"] for m in media if m["purpose"] == "original_page" and m["pixel_status"] == "available"]
        for index, block in enumerate(blocks):
            if not isinstance(block, dict) or str(block.get("block_label", "")).lower() not in VISUAL_LABELS:
                continue
            refs = sorted({a or b for a, b in IMAGE_REFERENCES.findall(str(block.get("block_content") or ""))})
            matches = [m for m in media if m["purpose"] == "region" and m["source_key"] in refs]
            mapping_status = "exact_reference" if len(matches) == 1 else "ambiguous" if len(matches) > 1 else "unresolved"
            bbox, bbox_status = self._bbox(block, pruned)
            record = {
                "occurrence_id": "occ-" + _identity(source_id, page_number, index),
                "source_id": source_id,
                "page": page_number,
                "block_index": index,
                "ocr_label": str(block["block_label"]).lower(),
                "bbox": bbox,
                "bbox_status": bbox_status,
                "localization_status": "unverified",
                "mapping_status": mapping_status,
                "referenced_keys": refs,
                "media_ids": [m["media_id"] for m in matches],
                "parent_page_media_ids": parent_ids,
                "training_eligible": False,
                "review_status": "pending",
            }
            self.occurrences.append(record)
            self._review_candidates.append(
                ("block:" + record["ocr_label"] + ":" + mapping_status, {"candidate_kind": "visual_block", "target": record})
            )

    def scan_source(self, path: Path, anchor: Path):
        relative = path.relative_to(anchor).as_posix()
        source_id = "source-" + _identity(relative)
        self._source_roots[source_id] = path.parent.resolve()
        source = {
            "source_id": source_id,
            "result_path": relative,
            "result_sha256": None,
            "pages_total": 0,
            "pages_scanned": 0,
            "page_limit_applied": False,
            "source_pdf_sha256": None,
            "segment_manifest_sha256": None,
            "source_pdf_hash_status": "unknown",
            "grouping_key": None,
            "split_status": "unassigned",
            "training_approval": "unknown",
            "status": "complete",
            "issues": [],
        }
        self.sources.append(source)
        try:
            pages, source["result_sha256"] = _pages(path, self.options.max_result_bytes)
            old_id, source_id = source_id, "source-" + _identity(relative, source["result_sha256"])
            source["source_id"] = source_id
            self._source_roots[source_id] = self._source_roots.pop(old_id)
            source["grouping_key"] = "ocr:" + source["result_sha256"]
            sidecar = _contained(path.parent, "ocr_segments.json")
            if sidecar.is_file():
                try:
                    metadata, sidecar_hash = _read_json(sidecar, self.options.max_result_bytes)
                    source["segment_manifest_sha256"] = sidecar_hash
                    value = metadata.get("source_sha256") if isinstance(metadata, dict) else None
                    if isinstance(value, str) and HASH.fullmatch(value):
                        source.update(
                            source_pdf_sha256=value.lower(),
                            source_pdf_hash_status="declared_not_verified",
                            grouping_key="pdf:" + value.lower(),
                        )
                except (ValueError, OSError):
                    source["issues"].append("invalid_segment_manifest")
            source["pages_total"] = len(pages)
            selected = pages[: self.options.max_pages] if self.options.max_pages else pages
            source["page_limit_applied"] = len(selected) < len(pages)
            for page_number, page in enumerate(selected, 1):
                media = self._page_media(source_id, page_number, page)
                self._page_blocks(source_id, page_number, page, media)
                source["pages_scanned"] += 1
            image_root = _contained(path.parent, "images")
            if image_root.is_dir():
                for image in sorted(image_root.rglob("*")):
                    if not image.is_file():
                        continue
                    image_relative = image.relative_to(path.parent).as_posix()
                    if (source_id, image_relative) in self._referenced_paths:
                        continue
                    kind = "unmapped_region_file" if image_relative.startswith("images/markdown/") else "unmapped_page_or_debug"
                    record = {
                        "unmapped_id": "unmapped-" + _identity(source_id, image_relative),
                        "source_id": source_id,
                        "source_path": image_relative,
                        "classification_hint": kind,
                        "mapping_status": "unregistered",
                        "training_eligible": False,
                    }
                    self.unmapped.append(record)
                    if kind == "unmapped_region_file":
                        self._review_candidates.append((kind, {"candidate_kind": kind, "target": record}))
        except (ValueError, OSError) as exc:
            source["status"] = "failed"
            source["issues"].append(
                str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError) else "result_read_or_parse_failed"
            )

    def review_queue(self):
        buckets = defaultdict(list)
        for stratum, value in self._review_candidates:
            buckets[stratum].append(value)
        for values in buckets.values():
            values.sort(key=lambda v: _identity(v["target"]))
        result, index = [], 0
        while len(result) < self.options.review_limit:
            added = False
            for stratum in sorted(buckets):
                values = buckets[stratum]
                if index >= len(values):
                    continue
                value = values[index]
                target = value["target"]
                record = {
                    "review_id": "review-" + _identity(value),
                    "stratum": stratum,
                    **value,
                    "media_ids": target.get("media_ids", []),
                    "parent_page_media_ids": target.get("parent_page_media_ids", []),
                    "decision": {
                        "status": "pending",
                        "confirmed_page": None,
                        "confirmed_block_index": None,
                        "financial_relevance": None,
                        "readable": None,
                        "header_unit_complete": None,
                        "source_training_approved": None,
                        "notes": None,
                    },
                    "training_eligible": False,
                }
                if value["candidate_kind"] == "unmapped_region_file":
                    media = self._register(
                        target["source_id"],
                        None,
                        target["source_path"],
                        "unmapped_region_candidate",
                        relative=target["source_path"],
                        origin="manual_review_candidate",
                    )
                    record["media_ids"] = [media["media_id"]]
                result.append(record)
                added = True
                if len(result) == self.options.review_limit:
                    break
            if not added:
                break
            index += 1
        return result

    def write(self, review, discovered_count):
        for source in self.sources:
            source["issues"] = sorted(set(source["issues"]))
        documents = {s["source_id"] for s in self.sources if s["status"] == "complete"}
        summary = {
            "documents_discovered": discovered_count,
            "documents_selected": len(self.sources),
            "documents_completed": len(documents),
            "documents_failed": len(self.sources) - len(documents),
            "document_limit_applied": len(self.sources) < discovered_count,
            "pages_scanned": sum(s["pages_scanned"] for s in self.sources),
            "pages_in_selected_documents": sum(s["pages_total"] for s in self.sources),
            "media_records": len(self.media),
            "visual_occurrences": len(self.occurrences),
            "pixel_status": dict(sorted(Counter(m["pixel_status"] for m in self.media).items())),
            "media_purpose": dict(sorted(Counter(m["purpose"] for m in self.media).items())),
            "occurrence_mapping": dict(sorted(Counter(o["mapping_status"] for o in self.occurrences).items())),
            "ocr_labels": dict(sorted(Counter(o["ocr_label"] for o in self.occurrences).items())),
            "unmapped_image_files": len(self.unmapped),
            "review_items": len(review),
            "training_eligible": 0,
        }
        products = {
            "sources.jsonl": self.sources,
            "media.jsonl": self.media,
            "occurrences.jsonl": self.occurrences,
            "unmapped-files.jsonl": self.unmapped,
            "review-queue.jsonl": review,
        }
        for name, records in products.items():
            with (self.staging / name).open("w", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n")
        markdown = (
            "# OCR 图像资产摸底\n\n"
            "本报告只读检查 OCR 来源。图片已解码不代表财务内容可读、定位可信或允许训练。\n\n"
            f"- 已处理文档：{summary['documents_completed']} / {summary['documents_selected']}\n"
            f"- 已扫描页面：{summary['pages_scanned']} / {summary['pages_in_selected_documents']}\n"
            f"- 视觉块：{summary['visual_occurrences']}\n"
            f"- 图片资源记录：{summary['media_records']}\n"
            f"- 未登记关联文件：{summary['unmapped_image_files']}\n"
            f"- 待人工复核：{summary['review_items']}\n\n"
            "## 关联状态\n\n```json\n"
            + json.dumps(summary["occurrence_mapping"], ensure_ascii=False, indent=2)
            + "\n```\n\n## 后续工作\n\n"
            "1. 在 review-queue.jsonl 的副本中补人工核对，另存标签文件并保留本报告。\n"
            "2. 核实来源准入、页/block 关联、符号/单位和图像上下文；不按文件名猜测金标。\n"
            "3. 补齐分组账本与资产审核后，再构建视觉 SFT 和 Agent 任务。\n\n"
            "本阶段无问答生成、模型调用、自动批准、训练或在线调度。\n"
        )
        (self.staging / "report.md").write_text(markdown, encoding="utf-8")
        files = {p.relative_to(self.staging).as_posix(): sha256(p) for p in sorted(self.staging.rglob("*")) if p.is_file()}
        implementation = {"module_sha256": sha256(Path(__file__)), "pillow_version": PILLOW_VERSION}
        manifest = {
            "schema_version": VERSION,
            "inventory_id": _identity(VERSION, implementation, asdict(self.options), self.sources, files),
            "implementation": implementation,
            "status": "partial" if summary["documents_failed"] else "complete",
            "product_kind": "ocr_media_audit",
            "options": asdict(self.options),
            "summary": summary,
            "files": files,
            "source_paths_relative_to": "caller_supplied_input_root",
            "training_eligible": False,
        }
        write_json(self.staging / "manifest.json", manifest)
        return manifest


def build_inventory(input_path: Path, output: Path, options: InventoryOptions | None = None):
    """Write a new immutable report directory; never modify OCR files or source DBs."""
    options = options or InventoryOptions()
    anchor, discovered = discover_results(input_path)
    if not discovered:
        raise ValueError("no assembled OCR result.json/result.jsonl found")
    selected = discovered[: options.max_documents] if options.max_documents else discovered
    output = output.resolve()
    if output.exists():
        raise ValueError("output already exists; use a new inventory directory")
    if any(output.is_relative_to(path.parent.resolve()) for path in selected):
        raise ValueError("output must be outside every OCR source directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".ocr-inventory-", dir=output.parent))
    try:
        inventory = OCRInventory(staging, options)
        for path in selected:
            inventory.scan_source(path, anchor)
        review = inventory.review_queue()
        manifest = inventory.write(review, len(discovered))
        if output.exists():
            raise ValueError("output appeared during inventory; refusing to replace it")
        staging.rename(output)
        return manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def verify_inventory(path: Path):
    """Check a produced audit's bytes and internal references without reading OCR inputs."""
    path = path.resolve()
    manifest, _ = _read_json(_contained(path, "manifest.json"), 33554432)
    if not isinstance(manifest, dict) or manifest.get("schema_version") != VERSION:
        raise ValueError("unsupported_inventory_manifest")
    if manifest.get("product_kind") != "ocr_media_audit" or manifest.get("training_eligible") is not False:
        raise ValueError("inventory_is_not_training_admission")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("invalid_inventory_file_index")
    for relative, expected in files.items():
        if not isinstance(expected, str) or not HASH.fullmatch(expected):
            raise ValueError("invalid_inventory_checksum")
        target = _contained(path, relative)
        if not target.is_file() or sha256(target) != expected:
            raise ValueError("inventory_checksum_mismatch")
    actual = {p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file()}
    if actual != set(files) | {"manifest.json"}:
        raise ValueError("unexpected_or_missing_inventory_files")
    products = {}
    for name in ("sources", "media", "occurrences", "unmapped-files", "review-queue"):
        relative = name + ".jsonl"
        if relative not in files:
            raise ValueError("missing_inventory_product")
        with _contained(path, relative).open(encoding="utf-8") as handle:
            records = [json.loads(line) for line in handle if line.strip()]
        if any(not isinstance(record, dict) or record.get("training_eligible", False) is not False for record in records):
            raise ValueError("invalid_inventory_record")
        products[name] = records
    try:
        options = InventoryOptions(**manifest["options"])
        expected_id = _identity(VERSION, manifest["implementation"], asdict(options), products["sources"], files)
        if expected_id != manifest["inventory_id"]:
            raise ValueError("inventory_identity_mismatch")
        source_ids = [s["source_id"] for s in products["sources"]]
        media_ids = [m["media_id"] for m in products["media"]]
        if len(set(source_ids)) != len(source_ids) or len(set(media_ids)) != len(media_ids):
            raise ValueError("duplicate_inventory_identity")
        for name in ("media", "occurrences", "unmapped-files"):
            if any(record["source_id"] not in source_ids for record in products[name]):
                raise ValueError("unknown_inventory_source")
        for record in products["media"]:
            package_path = record.get("package_path")
            if package_path is not None and (package_path not in files or files[package_path] != record["sha256"]):
                raise ValueError("inventory_media_binding_mismatch")
        for record in products["occurrences"] + products["review-queue"]:
            refs = record.get("media_ids", []) + record.get("parent_page_media_ids", [])
            if any(ref not in media_ids for ref in refs):
                raise ValueError("unknown_inventory_media")
        counts = {
            "documents_selected": "sources",
            "media_records": "media",
            "visual_occurrences": "occurrences",
            "unmapped_image_files": "unmapped-files",
            "review_items": "review-queue",
        }
        if any(manifest["summary"][key] != len(products[name]) for key, name in counts.items()):
            raise ValueError("inventory_summary_mismatch")
        if manifest["summary"]["training_eligible"] != 0:
            raise ValueError("inventory_is_not_training_admission")
    except (KeyError, TypeError) as exc:
        raise ValueError("invalid_inventory_structure") from exc
    return manifest
