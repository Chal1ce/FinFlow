import contextlib
import hashlib
import io
import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.context import PipelineContext
from delivery.publisher import LocalDeliveryPublisher
from storage.state_store import StateStore
from training.cli import main
from training.pretrain import PretrainDatasetBuilder, TrainingDataError


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_checksums(directory: Path, names: tuple[str, ...]) -> None:
    lines = []
    for name in names:
        digest = hashlib.sha256((directory / name).read_bytes()).hexdigest()
        lines.append(f"{digest}  {name}")
    (directory / "checksums.sha256").write_text("\n".join(lines) + "\n", encoding="ascii")


def create_v6_release(
    root: Path,
    *,
    batch_id: str,
    release_id: str,
    text: str,
    asset_uid: str,
    document_uid: str,
    quality_status: str | None = None,
) -> Path:
    context = PipelineContext.create(root, batch_id=batch_id)
    governed_uid = f"governed-{asset_uid}"
    output_dir = root / "processed" / "governed" / "fixture" / asset_uid / "local"
    output_dir.mkdir(parents=True)
    output_path = output_dir / "governed.md"
    output_path.write_text(text, encoding="utf-8")
    (output_dir / ".complete").write_text("complete\n", encoding="utf-8")

    with StateStore(root / "state" / "pipeline.db") as store:
        store.start_run(context, dry_run=False)
        store.upsert_governed_document(
            context,
            {
                "governed_uid": governed_uid,
                "document_uid": document_uid,
                "work_uid": f"work-{asset_uid}",
                "asset_uid": asset_uid,
                "backend": "local",
                "rule_version": "governance-v1",
                "input_path": "parsed/fixture.md",
                "output_path": context.relative_path(output_path),
                "status": "success",
                "block_count": 1,
                "char_count": len(text),
                "document_type": "financial-report",
                "title": f"Fixture title {asset_uid}",
                "source_name": "fixture-source",
                "source_id": f"source-{asset_uid}",
            },
        )
        store.replace_document_chunks(
            context,
            governed_uid,
            [
                {
                    "chunk_uid": f"chunk-{asset_uid}",
                    "document_uid": document_uid,
                    "work_uid": f"work-{asset_uid}",
                    "asset_uid": asset_uid,
                    "backend": "local",
                    "chunk_index": 0,
                    "content_type": "text",
                    "char_start": 0,
                    "char_end": len(text),
                    "text_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "text": text,
                    "status": "success",
                }
            ],
        )
        if quality_status is not None:
            store.upsert_ocr_quality(
                context,
                {
                    "asset_uid": asset_uid,
                    "document_uid": document_uid,
                    "ocr_backend": "local",
                    "ocr_input_hash": f"ocr-{asset_uid}",
                    "metrics": {"char_count": len(text)},
                    "qc_checks": [
                        {
                            "check_name": "suspicious_characters",
                            "status": "warn",
                            "message": "fixture warning",
                        }
                    ],
                    "status": quality_status,
                },
            )
        store.finish_run(context.run_id, "success")

    return Path(LocalDeliveryPublisher(root).publish(batch_id, release_id)["release_path"])


class PretrainDatasetTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        logger = logging.getLogger("fin_doc_governance")
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)
        self.directory.cleanup()

    def builder(self) -> PretrainDatasetBuilder:
        return PretrainDatasetBuilder(self.root)

    def test_v6_release_writes_redacted_governed_text_and_quality_provenance(self):
        source_text = "# Governed body\n\nContact alice@example.com or +86 138 0013 8000."
        create_v6_release(
            self.root,
            batch_id="batch-a",
            release_id="release-a",
            text=source_text,
            asset_uid="asset-a",
            document_uid="document-a",
            quality_status="needs_review",
        )

        result = self.builder().build(dataset_id="corpus-v1", releases=["batch-a/release-a"])

        self.assertEqual(result["status"], "success")
        dataset = self.root / "training" / "pretrain" / "corpus-v1"
        corpus = _jsonl(dataset / "corpus.jsonl")
        provenance = _jsonl(dataset / "provenance.jsonl")
        manifest = json.loads((dataset / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(corpus), 1)
        self.assertEqual(corpus[0]["text"], "# Governed body\n\nContact <EMAIL> or <PHONE>.")
        self.assertNotIn("Fixture title", corpus[0]["text"])
        self.assertEqual(provenance[0]["disposition"], "included")
        self.assertEqual(provenance[0]["ocr_status"], "needs_review")
        self.assertEqual(provenance[0]["ocr_warning_count"], 1)
        self.assertEqual(provenance[0]["redaction"], {"emails": 1, "phones": 1})
        self.assertEqual(manifest["rights_status"], "unknown")
        self.assertEqual(manifest["usage_scope"], "internal-only")
        self.assertEqual(manifest["source_fidelity_counts"], {"full_governed_document": 1})
        self.assertEqual(manifest["ocr_quality_status_counts"], {"needs_review": 1})
        self.assertEqual(len((dataset / "checksums.sha256").read_text(encoding="ascii").splitlines()), 4)

    def test_duplicate_redacted_documents_keep_all_origins(self):
        create_v6_release(
            self.root,
            batch_id="batch-a",
            release_id="release-a",
            text="Call first@example.com for the governed record.",
            asset_uid="asset-a",
            document_uid="document-a",
        )
        create_v6_release(
            self.root,
            batch_id="batch-b",
            release_id="release-b",
            text="Call second@example.com for the governed record.",
            asset_uid="asset-b",
            document_uid="document-b",
        )

        self.builder().build(
            dataset_id="deduplicated",
            releases=["batch-a/release-a", "batch-b/release-b"],
        )

        dataset = self.root / "training" / "pretrain" / "deduplicated"
        corpus = _jsonl(dataset / "corpus.jsonl")
        provenance = _jsonl(dataset / "provenance.jsonl")
        excluded = _jsonl(dataset / "excluded.jsonl")
        self.assertEqual(len(corpus), 1)
        self.assertEqual(len(provenance), 2)
        self.assertEqual([item["disposition"] for item in provenance], ["included", "duplicate"])
        self.assertEqual(excluded[0]["reason"], "duplicate_training_text")
        self.assertEqual({item["release_batch_id"] for item in provenance}, {"batch-a", "batch-b"})

    def test_v5_release_is_reconstructed_and_explicitly_marked_lossy(self):
        release = self.root / "published" / "legacy-batch" / "legacy-release"
        release.mkdir(parents=True)
        manifest = {
            "format_version": "financial-document-delivery-v5",
            "batch_id": "legacy-batch",
            "release_id": "legacy-release",
        }
        (release / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (release / "metadata.jsonl").write_text(
            json.dumps(
                {
                    "governed_uid": "legacy-governed",
                    "document_uid": "legacy-document",
                    "work_uid": "legacy-work",
                    "asset_uid": "legacy-asset",
                    "backend": "local",
                    "rule_version": "governance-v0",
                    "metadata": {"source_name": "legacy", "source_id": "legacy-source"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        chunks = [
            {"governed_uid": "legacy-governed", "chunk_index": 1, "metadata": {"text": "Second section."}},
            {"governed_uid": "legacy-governed", "chunk_index": 0, "metadata": {"text": "First section."}},
        ]
        (release / "chunks.jsonl").write_text(
            "".join(json.dumps(item) + "\n" for item in chunks), encoding="utf-8"
        )
        _write_checksums(release, ("metadata.jsonl", "chunks.jsonl", "manifest.json"))

        self.builder().build(dataset_id="legacy-corpus", releases=["legacy-batch/legacy-release"])

        dataset = self.root / "training" / "pretrain" / "legacy-corpus"
        corpus = _jsonl(dataset / "corpus.jsonl")
        provenance = _jsonl(dataset / "provenance.jsonl")
        manifest = json.loads((dataset / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(corpus[0]["text"], "First section.\n\nSecond section.")
        self.assertEqual(provenance[0]["source_fidelity"], "lossy_chunk_reconstruction")
        self.assertEqual(manifest["source_fidelity_counts"], {"lossy_chunk_reconstruction": 1})

    def test_empty_governed_text_is_excluded(self):
        release = self.root / "published" / "batch-empty" / "release-empty"
        release.mkdir(parents=True)
        (release / "manifest.json").write_text(
            json.dumps(
                {
                    "format_version": "financial-document-delivery-v6",
                    "batch_id": "batch-empty",
                    "release_id": "release-empty",
                }
            ),
            encoding="utf-8",
        )
        (release / "governed-documents.jsonl").write_text(
            json.dumps(
                {
                    "governed_uid": "empty-governed",
                    "document_uid": "empty-document",
                    "work_uid": "empty-work",
                    "asset_uid": "empty-asset",
                    "text": "   \n",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        _write_checksums(release, ("governed-documents.jsonl", "manifest.json"))

        self.builder().build(dataset_id="empty-corpus", releases=["batch-empty/release-empty"])

        dataset = self.root / "training" / "pretrain" / "empty-corpus"
        self.assertEqual(_jsonl(dataset / "corpus.jsonl"), [])
        self.assertEqual(_jsonl(dataset / "excluded.jsonl")[0]["reason"], "empty_training_text")

    def test_checksum_failure_is_rejected_before_dataset_is_created(self):
        release = create_v6_release(
            self.root,
            batch_id="batch-tampered",
            release_id="release-tampered",
            text="Untampered governed text.",
            asset_uid="asset-tampered",
            document_uid="document-tampered",
        )
        (release / "governed-documents.jsonl").write_text("tampered\n", encoding="utf-8")

        with self.assertRaises(TrainingDataError):
            self.builder().build(
                dataset_id="tampered-corpus", releases=["batch-tampered/release-tampered"]
            )

        self.assertFalse((self.root / "training" / "pretrain" / "tampered-corpus").exists())

    def test_existing_dataset_is_never_overwritten(self):
        create_v6_release(
            self.root,
            batch_id="batch-existing",
            release_id="release-existing",
            text="Stable governed text.",
            asset_uid="asset-existing",
            document_uid="document-existing",
        )
        self.builder().build(dataset_id="protected", releases=["batch-existing/release-existing"])

        with self.assertRaises(FileExistsError):
            self.builder().build(dataset_id="protected", releases=["batch-existing/release-existing"])

    def test_failed_atomic_finalize_leaves_no_partial_dataset(self):
        create_v6_release(
            self.root,
            batch_id="batch-atomic",
            release_id="release-atomic",
            text="Atomic governed text.",
            asset_uid="asset-atomic",
            document_uid="document-atomic",
        )
        destination = self.root / "training" / "pretrain" / "atomic"

        with patch("training.pretrain.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaisesRegex(OSError, "disk failure"):
                self.builder().build(dataset_id="atomic", releases=["batch-atomic/release-atomic"])

        self.assertFalse(destination.exists())
        self.assertEqual(list(destination.parent.glob(".atomic.*")), [])

    def test_cli_builds_dataset_and_prints_result(self):
        create_v6_release(
            self.root,
            batch_id="batch-cli",
            release_id="release-cli",
            text="CLI governed text.",
            asset_uid="asset-cli",
            document_uid="document-cli",
        )
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(
                [
                    "--data-root",
                    str(self.root),
                    "--log-file",
                    str(self.root / "logs" / "training.log"),
                    "--log-color",
                    "never",
                    "build-pretrain",
                    "--dataset-id",
                    "cli-corpus",
                    "--release",
                    "batch-cli/release-cli",
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["dataset_id"], "cli-corpus")
        self.assertTrue((self.root / "training" / "pretrain" / "cli-corpus" / "corpus.jsonl").is_file())


if __name__ == "__main__":
    unittest.main()
