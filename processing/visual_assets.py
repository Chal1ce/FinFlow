"""Preserve OCR visual occurrences before text filtering, without guessing coordinates."""

from __future__ import annotations

import json
import base64
import re
import shutil
from pathlib import Path

from core.flywheel_files import register_file, safe_path, sha256, write_json
from core.ids import stable_uid
from remote.paddle_client import safe_relative_path
from storage.state_store import utc_now


def ocr_pages(parsed_dir):
    parsed_dir = Path(parsed_dir)
    if (parsed_dir / "result.json").exists():
        return json.loads((parsed_dir / "result.json").read_text(encoding="utf-8")).get("layoutParsingResults", [])
    pages = []
    if (parsed_dir / "result.jsonl").exists():
        for line in (parsed_dir / "result.jsonl").read_text(encoding="utf-8").splitlines():
            if line.strip():
                pages.extend(json.loads(line).get("result", {}).get("layoutParsingResults", []))
    return pages


class VisualAssetExtractor:
    version = "visual-occurrence-v1"

    def __init__(self, root, store, context):
        self.root, self.store, self.context = Path(root).resolve(), store, context

    def extract(self, document, *, ocr_artifact, pdf_path, options):
        from PIL import Image

        parsed = Path(document["parsed_dir"])
        result = []
        for page_index, page in enumerate(ocr_pages(parsed)):
            pruned = page.get("prunedResult") or page
            blocks = pruned.get("parsing_res_list") or []
            for index, block in enumerate(blocks):
                label = str(block.get("block_label", "")).lower()
                if label not in {"image", "chart", "table", "figure"}:
                    continue
                bbox = block.get("block_bbox")
                content = str(block.get("block_content") or "")
                uid = stable_uid(self.version, document["asset_uid"], ocr_artifact, page_index, index, label, bbox,
                                 json.dumps(options, sort_keys=True))
                existing = self.store.connection.execute("SELECT * FROM visual_asset WHERE visual_asset_uid=?", (uid,)).fetchone()
                if existing and existing["status"] == "success" and existing["path"]:
                    path = safe_path(self.root, existing["path"])
                    if path.is_file() and sha256(path) == existing["sha256"]:
                        result.append(json.loads(existing["metadata_json"]))
                        continue
                metadata = {
                    "visual_asset_uid": uid,
                    "document_uid": document["document_uid"],
                    "work_uid": document["work_uid"],
                    "source_asset_uid": document["asset_uid"],
                    "asset_kind": "table" if label == "table" else "image",
                    "ocr_label": label,
                    "page": page_index + 1,
                    "block_index": index,
                    "bbox": bbox,
                    "backend": document["backend"],
                    "ocr_artifact_uid": ocr_artifact,
                    "extraction_version": self.version,
                    "ocr_content": content,
                    "context": "\n".join(str(b.get("block_content") or "") for b in blocks[max(0, index - 1) : index + 2]),
                    "options": options,
                    "status": "needs_review",
                    "coordinate_space": "unverified",
                }
                context_path = self.root / "media" / "contexts" / f"{uid}.json"
                write_json(
                    context_path, {"context": metadata["context"], "ocr_content": content, "page": page_index + 1, "block_index": index}
                )
                metadata["context_artifact_uid"] = register_file(
                    self.store, self.context, context_path, "visual-context", (ocr_artifact,), identity=uid
                )
                if label == "table":
                    from processing.cleaners import html_table_to_markdown

                    table_path = self.root / "media" / "tables" / f"{uid}.json"
                    write_json(
                        table_path,
                        {"raw_ocr": content, "markdown": html_table_to_markdown(content), "page": page_index + 1, "block_index": index},
                    )
                    metadata["table_artifact_uid"] = register_file(
                        self.store, self.context, table_path, "table-representation", (ocr_artifact,), identity=uid
                    )
                output = self.root / "media" / "occurrences" / f"{uid}.png"
                try:
                    crop = self._mapped_crop(parsed, page, content, page_index)
                    if crop:
                        output.parent.mkdir(parents=True, exist_ok=True)
                        with Image.open(crop) as image:
                            image.convert("RGB").save(output, format="PNG")
                        metadata.update(coordinate_space="ocr_returned_region", extraction_method="returned_crop")
                    else:
                        metadata["crop_transform"] = self._crop_pdf(pdf_path, output, page_index, bbox, pruned, options)
                        metadata.update(coordinate_space="original_page_pixels", extraction_method="pdf_bbox")
                    content_hash = sha256(output)
                    shared = self.root / "media" / "blobs" / f"{content_hash}.png"
                    shared.parent.mkdir(parents=True, exist_ok=True)
                    if not shared.exists() or sha256(shared) != content_hash:
                        shutil.copyfile(output, shared)
                    output.unlink()
                    with Image.open(shared) as image:
                        width, height = image.size
                    metadata.update(
                        path=self.context.relative_path(shared),
                        sha256=content_hash,
                        width=width,
                        height=height,
                        mime="image/png",
                        file_size=shared.stat().st_size,
                        status="success",
                    )
                    metadata["artifact_uid"] = register_file(
                        self.store,
                        self.context,
                        shared,
                        "visual-image",
                        (ocr_artifact,),
                        identity=uid,
                        document_uid=document["document_uid"],
                    )
                except (ValueError, OSError) as exc:
                    metadata["error_type"] = type(exc).__name__
                    metadata["reason"] = str(exc)
                    # A failed localization still has a persistent evidence record.
                    record_path = self.root / "media" / "unresolved" / f"{uid}.json"
                    write_json(record_path, metadata)
                    metadata["artifact_uid"] = register_file(
                        self.store, self.context, record_path, "visual-unresolved", (ocr_artifact,), identity=uid
                    )
                with self.store.connection:
                    self.store.connection.execute(
                        "INSERT INTO visual_asset VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(visual_asset_uid) "
                        "DO UPDATE SET artifact_uid=excluded.artifact_uid,path=excluded.path,sha256=excluded.sha256,"
                        "status=excluded.status,metadata_json=excluded.metadata_json",
                        (
                            uid,
                            metadata["artifact_uid"],
                            document["document_uid"],
                            document["work_uid"],
                            document["asset_uid"],
                            metadata["asset_kind"],
                            label,
                            page_index + 1,
                            index,
                            metadata.get("path"),
                            metadata.get("sha256"),
                            metadata["status"],
                            json.dumps(metadata, ensure_ascii=False),
                            utc_now(),
                        ),
                    )
                result.append(metadata)
        return result

    def _mapped_crop(self, parsed, page, content, page_index):
        refs = re.findall(r'(?:!\[[^\]]*\]\(([^)]+)\)|src=["\x27]([^"\x27]+))', content)
        keys = [a or b for a, b in refs]
        mapping = page.get("image_assets") or []
        matches = [item for item in mapping if item.get("purpose") == "region" and item.get("source_key") in keys]
        if len(matches) == 1:
            path = safe_path(parsed, matches[0]["path"])
            if sha256(path) != matches[0]["sha256"]:
                raise ValueError("OCR region checksum mismatch")
            return path
        # Legacy outputs are recoverable only with an exact block image reference.
        if len(keys) == 1:
            raw_value = ((page.get("markdown") or {}).get("images") or {}).get(keys[0])
            if isinstance(raw_value, str) and not raw_value.startswith(("http://", "https://")):
                encoded = raw_value.split(",", 1)[1] if raw_value.startswith("data:") else raw_value
                try:
                    value = base64.b64decode(encoded, validate=True)
                except ValueError:
                    value = None
                if value:
                    import hashlib

                    recovered = self.root / "media" / "recovered_ocr" / (hashlib.sha256(value).hexdigest() + ".bin")
                    recovered.parent.mkdir(parents=True, exist_ok=True)
                    recovered.write_bytes(value)
                    return recovered
            name = safe_relative_path(keys[0])
            candidates = [parsed / "images" / "markdown" / f"page_{page_index + 1:04d}" / name]
            for path in candidates:
                if path.is_file():
                    path.resolve().relative_to(parsed.resolve())
                    return path
        return None

    @staticmethod
    def _crop_pdf(pdf_path, output, page_index, bbox, pruned, options):
        import fitz

        if options.get("useDocUnwarping", True) or options.get("useDocOrientationClassify", True):
            raise ValueError("bbox transform after orientation/unwarping is unverified; provide OCR region crop")
        preprocessor = pruned.get("doc_preprocessor_res") or {}
        if (preprocessor.get("model_settings") or {}).get("use_doc_unwarping") or preprocessor.get("angle") not in {None, 0, -1}:
            raise ValueError("OCR response reports a coordinate transform; original-page crop is not valid")
        width, height = pruned.get("width"), pruned.get("height")
        if not width or not height or not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
            raise ValueError("page pixel dimensions and rectangular bbox required")
        width, height = float(width), float(height)
        x1, y1, x2, y2 = map(float, bbox)
        if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
            raise ValueError("bbox lies outside page")
        with fitz.open(pdf_path) as pdf:
            page = pdf[page_index]
            if page.rotation or abs(page.rect.width / page.rect.height - width / height) > 0.01:
                raise ValueError("PDF rotation/aspect does not match OCR page")
            rect = fitz.Rect(
                x1 / width * page.rect.width, y1 / height * page.rect.height, x2 / width * page.rect.width, y2 / height * page.rect.height
            )
            output.parent.mkdir(parents=True, exist_ok=True)
            page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=rect).save(output)
            return {
                "ocr_width": width,
                "ocr_height": height,
                "pdf_width": page.rect.width,
                "pdf_height": page.rect.height,
                "render_scale": 2,
                "pdf_clip": list(rect),
            }
