import json
import time
from pathlib import Path
from unittest.mock import patch

import fitz
import pytest
from PIL import Image
from tokenizers import Tokenizer, models, pre_tokenizers

from core.context import PipelineContext
from core.ids import stable_uid
from core.flywheel_files import register_file, sha256, write_json
from processing.visual_assets import VisualAssetExtractor
from storage.flywheel_store import FlywheelStore
from storage.flywheel_review import human_review, trace
from training.flywheel_corpus import TokenSplitter, build_snapshot, read_jsonl, verify
from workflow.flywheel import DailyFlywheel, DailyLock
from workflow.flywheel_config import FlywheelConfig


def tokenizer_file(root):
    tokenizer = Tokenizer(models.WordLevel({"[UNK]": 0}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = pre_tokenizers.Split("", behavior="isolated")
    path = root / "tokenizer.json"
    tokenizer.save(str(path))
    return path


def config_file(root, *, policy=None):
    collection = root / "collection.json"
    write_json(collection, {"targets": [], "scholarly": {"enabled": False}})
    path = root / "flywheel.json"
    write_json(
        path,
        {
            "collection_config": str(collection),
            "methods": ["original", "visual", "translate", "rewrite"],
            "source_policy": {"fixture": {"training": "approved", "usage_scope": "internal-only"}},
            "require_ocr_pass": False,
            "max_tokens": 64,
            "max_tasks": 100,
            **(policy or {}),
        },
    )
    environment = {"FIN_DOC_PRETRAIN_TOKENIZER": str(tokenizer_file(root)), "PADDLEOCR_LOCAL_API_URL": "http://localhost/ocr"}
    for prefix in ("FIN_DOC_VISION", "FIN_DOC_PRETRAIN_TRANSLATE", "FIN_DOC_PRETRAIN_REWRITE", "FIN_DOC_PRETRAIN_REVIEW"):
        environment.update(
            {prefix + "_API_URL": "http://localhost/v1", prefix + "_API_KEY": "test-key-not-exportable", prefix + "_MODEL": "fixture"}
        )
    with patch.dict("os.environ", environment):
        return FlywheelConfig(path, data_root=root / "data")


class FakeClient:
    def __init__(self):
        self.requests = 0
        self.roles = []

    def generate(self, role, prompt, **kwargs):
        self.requests += 1
        self.roles.append(role)
        if role == "review":
            text = json.dumps({"status": "accepted", "reasons": ["fixture verified"]})
        elif role == "vision":
            text = "图示营业收入及报告期间，单位为亿元。图中数据仅用于说明文档中的金融信息。"
        else:
            text = prompt.split("SOURCE:\n", 1)[-1]
        return {
            "text": text,
            "finish_reason": "stop",
            "usage": {"total_tokens": 10},
            "model": {"model": "fixture", "role": role},
            "refusal": None,
        }


def parsed_fixture(root):
    raw = root / "raw_pdfs" / "document.pdf"
    raw.parent.mkdir(parents=True)
    with fitz.open() as pdf:
        page = pdf.new_page()
        page.insert_text((50, 50), "Financial data 2025")
        pdf.save(raw)
    parsed = root / "parsed_md" / "financial" / "work" / "asset" / "local"
    image = parsed / "images" / "markdown" / "page_0001" / "chart.png"
    image.parent.mkdir(parents=True)
    Image.new("RGB", (64, 48), color="white").save(image)
    text = "本报告介绍公司在2025年度的经营情况、财务信息和风险管理。" * 8
    blocks = [
        {"block_label": "text", "block_content": text, "block_bbox": [0, 0, 100, 100]},
        {"block_label": "chart", "block_content": "![图1](chart.png)", "block_bbox": [20, 20, 100, 100]},
        {"block_label": "table", "block_content": "<table><tr><td>2025</td><td>10</td></tr></table>", "block_bbox": [20, 120, 100, 200]},
    ]
    write_json(
        parsed / "result.json",
        {
            "layoutParsingResults": [
                {
                    "prunedResult": {"parsing_res_list": blocks},
                    "image_assets": [
                        {"source_key": "chart.png", "purpose": "region", "path": str(image.relative_to(parsed)), "sha256": sha256(image)}
                    ],
                }
            ]
        },
    )
    (parsed / "output.md").write_text(text, encoding="utf-8")
    manifest = {
        "asset_uid": "asset",
        "work_uid": "work",
        "document_uid": "work",
        "source_name": "fixture",
        "raw_path": str(raw.relative_to(root)),
        "raw_file_hash": sha256(raw),
        "page_count": 1,
        "title": "报告",
    }
    directory = root / "manifests"
    directory.mkdir(parents=True)
    (directory / "local_documents.jsonl").write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    return parsed, raw


def test_queue_idempotency_dependency_and_expired_lease(tmp_path):
    with FlywheelStore(tmp_path / "pipeline.db") as store:
        parent = store.enqueue("download", "doc", {}, version="v1")
        child = store.enqueue("ocr", "doc", {}, version="v1", dependency=parent)
        assert parent == store.enqueue("download", "doc", {}, version="v1")
        task = store.claim("owner", lease_seconds=0.01)
        assert task["task_uid"] == parent
        assert store.claim("other") is None
        time.sleep(0.02)
        task = store.claim("other")
        assert task["attempts"] == 2
        store.finish(task, "succeeded")
        task = store.claim("owner")
        assert task["task_uid"] == child
        store.finish(task, "needs_review")
        assert store.claim("owner") is None


def test_schema_backup_and_kernel_lock(tmp_path):
    from storage.state_store import StateStore

    path = tmp_path / "pipeline.db"
    StateStore(path).close()
    with FlywheelStore(path):
        assert path.with_name(path.name + ".before-flywheel.bak").exists()
    with DailyLock(tmp_path):
        with pytest.raises(RuntimeError), DailyLock(tmp_path):
            pass
    with DailyLock(tmp_path):
        pass


def test_artifact_cycle_rejected(tmp_path):
    context = PipelineContext.create(tmp_path)
    with FlywheelStore(tmp_path / "pipeline.db") as store:
        store.start_run(context, dry_run=False)
        a, b = tmp_path / "a.json", tmp_path / "b.json"
        write_json(a, {"a": 1})
        write_json(b, {"b": 2})
        uid_a = register_file(store, context, a, "fixture")
        uid_b = register_file(store, context, b, "fixture", (uid_a,))
        with pytest.raises(ValueError):
            store.edge(uid_a, uid_b)


def test_token_split_exact_text_and_bound(tmp_path):
    splitter = TokenSplitter(tokenizer_file(tmp_path), 32)
    text = "金融报告2025\n" * 24
    segments = list(splitter.split(text))
    assert "".join(x["text"] for x in segments) == text
    assert all(0 < x["token_count"] <= 32 for x in segments)


def test_visual_occurrence_and_unverified_table(tmp_path):
    parsed, raw = parsed_fixture(tmp_path)
    context = PipelineContext.create(tmp_path)
    with FlywheelStore(tmp_path / "pipeline.db") as store:
        store.start_run(context, dry_run=False)
        pdf = register_file(store, context, raw, "pdf")
        ocr = register_file(store, context, parsed / "result.json", "ocr", (pdf,))
        extractor = VisualAssetExtractor(tmp_path, store, context)
        document = {"asset_uid": "asset", "work_uid": "work", "document_uid": "work", "backend": "local", "parsed_dir": parsed}
        records = extractor.extract(document, ocr_artifact=ocr, pdf_path=raw, options={})
        assert [r["status"] for r in records] == ["success", "needs_review"]
        assert {r["asset_kind"] for r in records} == {"image", "table"}
        extractor.extract(document, ocr_artifact=ocr, pdf_path=raw, options={})
        assert store.connection.execute("SELECT count(*) FROM visual_asset").fetchone()[0] == 2


def test_daily_end_to_end_no_model_calls_on_replay(tmp_path):
    config = config_file(tmp_path)
    parsed_fixture(config.root)
    client = FakeClient()
    result = DailyFlywheel(config, client=client).run(discover=False)
    assert result["errors"] == []
    assert result["dataset"]["samples"] > 0
    report = result["quality_reports"]["cpt"]
    assert report["status"] == "success"
    assert report["metrics"]["samples"] == result["dataset"]["samples"]
    assert verify(report["path"])["schema_version"] == "finflow-quality-report-v1"
    assert result["quality_reports"]["sft"]["status"] == "not_available"
    dataset = Path(result["dataset"]["path"])
    manifest = verify(dataset)
    release = config.root / manifest["release"]["path"]
    assert verify(release)["format_version"] == "financial-document-delivery-v7"
    assert (release / "visual-assets.jsonl").is_file()
    assert all(r["token_count"] <= config.max_tokens for r in read_jsonl(dataset / "corpus.jsonl"))
    assert {"review", "vision", "translate", "rewrite"} <= set(client.roles)
    request_count = client.requests
    result2 = DailyFlywheel(config, client=client).run(discover=False)
    assert result2["status"] == "no_change"
    assert client.requests == request_count
    assert "test-key-not-exportable" not in "\n".join(p.read_text(errors="ignore") for p in release.rglob("*.json"))
    build_snapshot(config.root, "combined", [result["dataset"]["dataset_id"]])
    assert verify(config.root / "training" / "snapshots" / "combined")["samples"] > 0
    (dataset / "corpus.jsonl").write_text("corrupted")
    with pytest.raises(ValueError):
        verify(dataset)


def test_unknown_source_is_reviewed_and_not_exported(tmp_path):
    config = config_file(tmp_path, policy={"source_policy": {}, "methods": ["original"]})
    parsed_fixture(config.root)
    client = FakeClient()
    result = DailyFlywheel(config, client=client).run(discover=False)
    assert result["dataset"]["status"] == "no_change"
    assert result["queue"]["needs_review"] > 0
    assert client.requests == 0


def test_failure_retries_without_fabricated_candidate(tmp_path):
    config = config_file(tmp_path, policy={"methods": ["original"], "retry_seconds": 0, "max_attempts": 2})
    parsed_fixture(config.root)

    class BrokenClient(FakeClient):
        def generate(self, *args, **kwargs):
            raise RuntimeError("api-key-should-never-be-recorded")

    result = DailyFlywheel(config, client=BrokenClient()).run(discover=False)
    assert result["queue"].get("failed", 0) > 0
    assert "api-key-should-never-be-recorded" not in json.dumps(result)
    assert result["dataset"]["status"] == "no_change"


def test_preflight_reports_missing_models_without_creating_data(tmp_path):
    config = config_file(tmp_path)
    config.roles["review"] = replace_role = config.roles["review"]
    from dataclasses import replace

    config.roles["review"] = replace(replace_role, key="", model="")
    assert config.preflight()["status"] == "failed"
    assert not config.root.exists()


def test_duplicate_day_retains_origins_without_empty_training_dataset(tmp_path):
    config = config_file(tmp_path, policy={"methods": ["original"]})
    parsed_fixture(config.root)
    first = DailyFlywheel(config, client=FakeClient()).run(discover=False)
    config.version += "-new-review-policy"
    second = DailyFlywheel(config, client=FakeClient()).run(discover=False)
    assert second["dataset"]["status"] == "no_change"
    assert second["dataset"]["samples"] == 0
    assert second["dataset"]["kind"] == "lineage"
    assert len(list((config.root / "training" / "pretrain").glob("*"))) == 1
    assert Path(second["dataset"]["path"]).parent.name == "origin_deltas"
    sample = read_jsonl(Path(first["dataset"]["path"]) / "corpus.jsonl")[0]
    with FlywheelStore(config.root / "state" / "pipeline.db") as store:
        result = trace(store, sample_uid=sample["sample_id"])
        assert len(result["origins"]) >= 2
        assert "flywheel-pdf" in {x["artifact_type"] for x in result["artifacts"]}
    build_snapshot(config.root, "all-origins", [first["dataset"]["dataset_id"]], origin_deltas=[second["dataset"]["dataset_id"]])


def test_model_checkpoint_survives_downstream_failure(tmp_path):
    config = config_file(tmp_path, policy={"methods": ["visual"], "retry_seconds": 0})
    parsed_fixture(config.root)
    client = FakeClient()
    original = DailyFlywheel._candidate
    injected = {"remaining": 1}

    def interrupt_after_model(self, *args, **kwargs):
        if args[0]["method"] == "visual" and injected["remaining"]:
            injected["remaining"] -= 1
            raise RuntimeError("simulated process interruption after model checkpoint")
        return original(self, *args, **kwargs)

    with patch.object(DailyFlywheel, "_candidate", interrupt_after_model):
        result = DailyFlywheel(config, client=client).run(discover=False)
    assert result["dataset"]["samples"] > 0
    assert client.roles.count("vision") == 1


def test_human_review_and_immutable_decision_history(tmp_path):
    config = config_file(tmp_path, policy={"methods": ["original"], "require_ocr_pass": True})
    parsed_fixture(config.root)
    DailyFlywheel(config, client=FakeClient()).run(discover=False)
    with FlywheelStore(config.root / "state" / "pipeline.db") as store:
        row = store.connection.execute("SELECT candidate_uid,decision_json FROM training_candidate LIMIT 1").fetchone()
        old = json.loads(row["decision_json"])["artifact_uid"]
        result = human_review(config, store, row["candidate_uid"], "accepted", reason="checked source", reviewer="fixture reviewer")
        assert result["decision"]["decision_type"] == "human"
        lineage = trace(store, candidate_uid=row["candidate_uid"])
        assert old in {item["artifact_uid"] for item in lineage["artifacts"]}
        config.policy["source_policy"] = {}
        with pytest.raises(ValueError):
            human_review(config, store, row["candidate_uid"], "accepted", reason="x", reviewer="x")


def test_discovery_window_does_not_advance_over_unseen_pages(tmp_path):
    from types import SimpleNamespace

    config = config_file(tmp_path)
    runner = DailyFlywheel(config)

    class Candidate:
        def to_mapping(self):
            return {
                "candidate_uid": "scholar-candidate",
                "work_uid": "work",
                "source_name": "fixture",
                "metadata_hash": "stable",
                "pdf_url": "https://example.test/a.pdf",
                "open_access": True,
            }

    class Adapter:
        name = "fixture"
        next_cursor = None
        scan_complete = False
        calls = []

        def discover(self, target, *, updated_since=None, cursor=None):
            self.calls.append((updated_since, cursor, self.updated_until))
            self.next_cursor = "page-two" if cursor is None else None
            self.scan_complete = cursor is not None
            return [Candidate()]

    adapter = Adapter()
    runner.scholar_runner = SimpleNamespace(adapters=[adapter])
    runner.scholar_targets = [SimpleNamespace(target_uid="target", start_date="2022-01-01", end_date="2026-12-31")]
    config.root.mkdir(parents=True)
    with FlywheelStore(config.root / "state" / "pipeline.db") as store:
        runner.store = store
        assert runner._discover() == []
        assert runner._discover() == []
        assert adapter.calls[0][2] == adapter.calls[1][2]
        assert adapter.calls[1][1] == "page-two"
        assert store.connection.execute("SELECT count(*) FROM processing_task").fetchone()[0] == 1


def test_safe_table_crop_without_coordinate_transforms(tmp_path):
    parsed, raw = parsed_fixture(tmp_path)
    result = json.loads((parsed / "result.json").read_text())
    result["layoutParsingResults"][0]["prunedResult"].update(width=595, height=842)
    write_json(parsed / "result.json", result)
    context = PipelineContext.create(tmp_path)
    with FlywheelStore(tmp_path / "pipeline.db") as store:
        store.start_run(context, dry_run=False)
        ocr = register_file(store, context, parsed / "result.json", "ocr")
        records = VisualAssetExtractor(tmp_path, store, context).extract(
            {"parsed_dir": parsed, "asset_uid": "asset", "work_uid": "work", "document_uid": "work", "backend": "local"},
            ocr_artifact=ocr,
            pdf_path=raw,
            options={"useDocOrientationClassify": False, "useDocUnwarping": False},
        )
        table = records[1]
        assert table["status"] == "success"
        assert table["asset_kind"] == "table"
        assert table["crop_transform"]["render_scale"] == 2
        assert table["table_artifact_uid"]


def test_discovery_download_ocr_through_training_with_injected_services(tmp_path):
    from types import SimpleNamespace

    config = config_file(tmp_path, policy={"methods": ["original"], "require_ocr_pass": False})
    calls = []

    class Downloader:
        def download(self, spec, *, refresh=False):
            calls.append(("download", refresh))
            raw = config.root / "raw_pdfs" / "source.pdf"
            raw.parent.mkdir(parents=True, exist_ok=True)
            if not raw.exists():
                with fitz.open() as pdf:
                    pdf.new_page()
                    pdf.save(raw)
            return {
                "status": "success",
                "asset_uid": "asset",
                "raw_path": str(raw.relative_to(config.root)),
                "raw_file_hash": sha256(raw),
                "page_count": 1,
                "source_url": spec.source_url,
                "report_uid": spec.canonical_uid,
            }

        def associate_batch(self, record, batch_id):
            return {**record, "batch_id": batch_id}

    def processor(pdf_path, output):
        calls.append(("ocr", str(pdf_path)))
        output.mkdir(parents=True, exist_ok=True)
        text = "公司2025年度报告说明营业收入、公司治理与经营风险，并保持原始财务数据口径。" * 6
        write_json(
            output / "result.json",
            {
                "layoutParsingResults": [
                    {
                        "prunedResult": {
                            "width": 595,
                            "height": 842,
                            "parsing_res_list": [{"block_label": "text", "block_content": text, "block_bbox": [0, 0, 100, 100]}],
                        }
                    }
                ]
            },
        )
        (output / "output.md").write_text(text)
        return {"status": "success"}

    class SeededFlywheel(DailyFlywheel):
        def _discover(self):
            self._register_source(
                "financial",
                {
                    "candidate_uid": "candidate",
                    "canonical_uid": "work",
                    "stock_code": "600001",
                    "report_year": 2025,
                    "report_type": "annual",
                    "title": "2025年年度报告",
                    "source_name": "fixture",
                    "source_id": "source",
                    "source_url": "https://example.test/a.pdf",
                },
            )
            return []

    runner = SeededFlywheel(config, client=FakeClient(), processor=processor)
    runner.report_runner = SimpleNamespace(adapters=[], downloader=Downloader())
    result = runner.run()
    assert result["errors"] == []
    assert result["dataset"]["samples"] > 0
    assert [call[0] for call in calls] == ["download", "ocr"]
    with FlywheelStore(config.root / "state" / "pipeline.db") as store:
        assert store.connection.execute("SELECT count(*) FROM report_asset").fetchone()[0] == 1
        assert store.connection.execute("SELECT count(*) FROM document_registry").fetchone()[0] == 1


def test_model_budget_defers_without_consuming_retry_attempt(tmp_path):
    from processing.visual_description import ModelBudgetExceeded

    config = config_file(tmp_path, policy={"methods": ["original"]})
    parsed_fixture(config.root)

    class LimitedClient(FakeClient):
        def generate(self, *args, **kwargs):
            raise ModelBudgetExceeded("budget")

    result = DailyFlywheel(config, client=LimitedClient()).run(discover=False)
    assert result["queue"]["deferred"] == 1
    with FlywheelStore(config.root / "state" / "pipeline.db") as store:
        assert store.connection.execute("SELECT attempts FROM processing_task WHERE status='deferred'").fetchone()[0] == 0


def test_split_conflict_quarantines_new_origin(tmp_path):
    from delivery.flywheel_release import publish_evidence
    from training.flywheel_corpus import FlywheelCorpusBuilder

    config = config_file(tmp_path, policy={"methods": ["original"]})
    parsed_fixture(config.root)
    DailyFlywheel(config, client=FakeClient()).run(discover=False)
    context = PipelineContext.create(config.root)
    with FlywheelStore(config.root / "state" / "pipeline.db") as store:
        store.start_run(context, dry_run=False)
        original = store.connection.execute("SELECT * FROM training_candidate WHERE status='accepted' LIMIT 1").fetchone()
        candidate = json.loads(original["data_json"])
        candidate.update(candidate_uid="work-two-candidate", work_uid="work-two")
        path = config.root / "training" / "candidates" / "work-two.json"
        write_json(path, candidate)
        candidate.update(path=context.relative_path(path), sha256=sha256(path))
        candidate["artifact_uid"] = register_file(store, context, path, "training-candidate", (original["artifact_uid"],))
        with store.connection:
            store.connection.execute(
                "INSERT INTO training_candidate VALUES(?,?,?,?,?,?,?,?)",
                (
                    candidate["candidate_uid"],
                    candidate["work_uid"],
                    candidate["artifact_uid"],
                    "original",
                    "pending",
                    json.dumps(candidate),
                    None,
                    "2026-10-01",
                ),
            )
        human_review(config, store, candidate["candidate_uid"], "accepted", reason="fixture", reviewer="fixture")
        builder = FlywheelCorpusBuilder(config.root, store, TokenSplitter(config.tokenizer, config.max_tokens))
        work_split = store.get("work-split", stable_uid(builder.recipe_uid, "work"))["split"]
        store.put("work-split", stable_uid(builder.recipe_uid, "work-two"), {"split": "validation" if work_split == "train" else "train"})
        release = publish_evidence(config.root, store, "split-conflict-test")
        result = builder.build("split-conflict-delta", release=release)
        assert result["status"] == "no_change"
        assert store.get("split-conflict", "work-two-candidate")["status"] == "needs_review"


def test_recipe_change_exports_existing_content_into_new_namespace(tmp_path):
    from training.flywheel_corpus import FlywheelCorpusBuilder

    config = config_file(tmp_path, policy={"methods": ["original"]})
    parsed_fixture(config.root)
    first = DailyFlywheel(config, client=FakeClient()).run(discover=False)
    with FlywheelStore(config.root / "state" / "pipeline.db") as store:
        changed = FlywheelCorpusBuilder(config.root, store, TokenSplitter(config.tokenizer, config.max_tokens), validation_fraction=0.1)
        result = changed.build("new-recipe", release=first["release"])
        assert result["samples"] > 0
        assert (
            read_jsonl(Path(result["path"]) / "corpus.jsonl")[0]["sample_id"]
            != read_jsonl(Path(first["dataset"]["path"]) / "corpus.jsonl")[0]["sample_id"]
        )
        with pytest.raises(ValueError):
            build_snapshot(config.root, "mixed-recipes", [first["dataset"]["dataset_id"], "new-recipe"])


def test_live_discovery_is_independent_of_unfinished_history(tmp_path):
    from types import SimpleNamespace

    config = config_file(tmp_path, policy={"live_discovery": True})
    runner = DailyFlywheel(config)

    class Candidate:
        def __init__(self, uid):
            self.uid = uid

        def to_mapping(self):
            return {"candidate_uid": self.uid, "work_uid": self.uid, "source_name": "fixture", "metadata_hash": self.uid}

    class Adapter:
        name = "fixture"
        calls = []

        def discover(self, target, *, updated_since=None, cursor=None):
            self.calls.append((updated_since, cursor))
            self.scan_complete = updated_since is not None
            self.next_cursor = None if self.scan_complete else "historical-page-two"
            return [Candidate("live" if updated_since is not None else "historical")]

    adapter = Adapter()
    runner.scholar_runner = SimpleNamespace(adapters=[adapter])
    runner.scholar_targets = [SimpleNamespace(target_uid="target", start_date="2022-01-01", end_date="2026-12-31")]
    with FlywheelStore(config.root / "state" / "pipeline.db") as store:
        runner.store = store
        assert runner._discover() == []
        windows = store.records("discovery-window")
        assert next(w for w in windows if w["lane"] == "live")["complete"]
        assert not next(w for w in windows if w["lane"] == "history")["complete"]
        assert {r["candidate_uid"] for r in store.records("source")} == {"live", "historical"}
        runner._discover()
        assert adapter.calls[-1][1] == "historical-page-two"


def test_crossref_keeps_page_parameters_and_registers_complete_pages():
    from urllib.parse import parse_qs, urlsplit
    from spiders.scholarly_sources import CrossrefAdapter, ScholarlyTarget

    class Http:
        urls = []

        def request_bytes(self, url, **kwargs):
            self.urls.append(url)
            offset = (len(self.urls) - 1) * 2
            items = [
                {
                    "DOI": f"10.1000/example-{offset + i}",
                    "title": [f"Financial example {offset + i}"],
                    "published-online": {"date-parts": [[2024, 1, 1]]},
                }
                for i in range(2)
            ]
            return json.dumps({"message": {"items": items, "next-cursor": "cursor-" + str(len(self.urls))}}).encode()

    http = Http()
    adapter = CrossrefAdapter(http, rows=2)
    adapter.updated_until = "2026-10-01T00:00:00+00:00"
    records = adapter.discover(ScholarlyTarget("query", "finance", 2024, 2024, max_results=3), updated_since="2026-09-01")
    assert len(records) == 4  # retain the complete final page, not a truncated cursor boundary
    queries = [parse_qs(urlsplit(url).query) for url in http.urls]
    assert queries[0]["rows"] == queries[1]["rows"] == ["2"]
    assert queries[0]["filter"] == queries[1]["filter"]
    assert adapter.next_cursor == "cursor-2"
    assert not adapter.scan_complete


def test_openalex_key_is_header_only_and_page_size_is_supported():
    from spiders.scholarly_sources import OpenAlexAdapter, ScholarlyTarget

    class Http:
        def request_bytes(self, url, *, headers=None):
            assert "source-secret-key" not in url
            assert headers["Authorization"] == "Bearer source-secret-key"
            return b'{"results":[],"meta":{"next_cursor":null}}'

    with patch.dict("os.environ", {"OPENALEX_API_KEY": "source-secret-key"}):
        adapter = OpenAlexAdapter(Http(), per_page=200)
        assert adapter.per_page == 100
        assert adapter.discover(ScholarlyTarget("query", "finance", 2024, 2024)) == []
