"""Stage independent language sites and create redirects for old documentation URLs.

Uses only the Python standard library. Run from any directory; source files are
never modified. Translation revisions are recorded in docs/translations.json.
"""

import argparse
import hashlib
import html
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
BUILD = ROOT / "_build"


def page_url(name: str) -> str:
    return "/" if name == "index" else f"/{name}.html"


def redirect(target: str) -> str:
    safe = html.escape(target, quote=True)
    encoded = json.dumps(target)
    return (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>FinFlow documentation</title>'
        f'<link rel="canonical" href="{safe}">'
        f'<meta http-equiv="refresh" content="0;url={safe}">'
        f'<p><a href="{safe}">中文文档 / Chinese documentation</a></p>'
        f'<script>location.replace({encoded}+location.search+location.hash);</script>'
        '</html>\n'
    )


def prepare(baseurl: str) -> None:
    revisions = json.loads((DOCS / "translations.json").read_text(encoding="utf-8"))
    originals = {p.stem: p for p in DOCS.glob("*.md")}
    translations = {p.stem: p for p in (DOCS / "en").glob("*.md")}
    unknown = translations.keys() - originals.keys()
    if unknown:
        raise ValueError(f"English pages without a Chinese source: {sorted(unknown)}")
    missing = translations.keys() - revisions.keys()
    if missing:
        raise ValueError(f"Missing translation revisions: {sorted(missing)}")
    source_root = BUILD / "docs-source"
    if source_root.exists():
        shutil.rmtree(source_root)
    for lang, pages in (("zh", originals), ("en", translations)):
        dest = source_root / lang
        dest.mkdir(parents=True)
        for shared in ("assets", "_includes", "_sass"):
            shutil.copytree(DOCS / shared, dest / shared)
        shutil.copyfile(DOCS / "_config.yml", dest / "_config.yml")
        counterparts = {}
        for name, path in pages.items():
            content = path.read_text(encoding="utf-8")
            if not content.startswith("---\n"):
                raise ValueError(f"Missing front matter: {path}")
            content = content.replace("---\n", f"---\ntranslation_key: {name}\n", 1)
            if lang == "en":
                # English sources share the images in ../assets on GitHub.
                content = content.replace("../assets/", "assets/")
            (dest / path.name).write_text(content, encoding="utf-8")
            other = "en" if lang == "zh" else "zh"
            available = name in (translations if lang == "zh" else originals)
            source_hash = hashlib.sha256(originals[name].read_bytes()).hexdigest()
            counterparts[name] = {
                "url": f"{baseurl}/{other}{page_url(name) if available else '/'}",
                "available": available,
                "stale": name in translations and revisions[name]["source_sha256"] != source_hash,
            }
        (dest / "_data").mkdir()
        (dest / "_data" / "translations.json").write_text(
            json.dumps(counterparts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        settings = {
            "lang": "zh-CN" if lang == "zh" else "en",
            "docs_language": lang,
            "baseurl": f"{baseurl}/{lang}",
            "gh_edit_source": "docs" if lang == "zh" else "docs/en",
            "back_to_top_text": "返回顶部" if lang == "zh" else "Back to top",
            "gh_edit_link_text": "在 GitHub 上编辑此页" if lang == "zh" else "Edit this page on GitHub",
            "description": "金融文档治理与每日预训练数据飞轮使用说明" if lang == "zh" else "Financial document governance and daily pretraining data pipeline",
            "aux_links": {"GitHub": ["https://github.com/Chal1ce/FinFlow"]},
        }
        # JSON is also valid YAML and avoids an extra build-script dependency.
        (dest / "_config.language.yml").write_text(
            json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


def finalize(baseurl: str) -> None:
    site = ROOT / "_site"
    if not (site / "zh" / "index.html").is_file() or not (site / "en" / "index.html").is_file():
        raise ValueError("Build both language sites before creating redirects")
    for path in DOCS.glob("*.md"):
        (site / f"{path.stem}.html").write_text(
            redirect(f"{baseurl}/zh{page_url(path.stem)}"), encoding="utf-8"
        )
    (site / ".nojekyll").touch()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseurl", default="/FinFlow", help="Repository URL prefix; empty for a custom domain")
    parser.add_argument("--finalize", action="store_true")
    args = parser.parse_args()
    prefix = args.baseurl.rstrip("/")
    if prefix and (not prefix.startswith("/") or ".." in prefix or "?" in prefix or "#" in prefix):
        parser.error("baseurl must be an absolute URL path without query or fragment")
    if args.finalize:
        finalize(prefix)
    else:
        prepare(prefix)
