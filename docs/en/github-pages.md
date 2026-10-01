---
title: Publishing with GitHub Pages
parent: Reference and design
nav_order: 3
---

# Publish the bilingual documentation

`.github/workflows/docs.yml` builds two Just the Docs sites and deploys a combined
Pages artifact. Each language has independent navigation and search.

## Enable or migrate Pages

1. Push the documentation and workflow to `main`.
2. Open [Pages settings](https://github.com/Chal1ce/FinFlow/settings/pages).
3. Set **Build and deployment → Source → GitHub Actions**.
4. In [Actions](https://github.com/Chal1ce/FinFlow/actions), open **Documentation**.
5. If the initial push preceded Pages enablement, use **Run workflow** on `main`.
6. Wait for `build` and `deploy`, then open **Visit site** in Pages settings.

Change the previous branch-based `main` + `/docs` method. See
[GitHub's custom workflow guide](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages).

## URLs and updates

- [Default entry](https://chal1ce.github.io/FinFlow/) redirects to Chinese.
- [Chinese](https://chal1ce.github.io/FinFlow/zh/).
- [English](https://chal1ce.github.io/FinFlow/en/).

Old top-level `.html` URLs redirect to matching Chinese pages; JavaScript preserves
query strings and fragments. Documentation changes on `main` publish automatically;
pull requests build without deploying. See [Writing docs](docs-authoring.md).
The common configuration is `docs/_config.yml`; Just the Docs is fixed at `v0.12.0`.

## Local build

With Ruby 3.3 and Python 3 installed:

```sh
BUNDLE_GEMFILE=docs/Gemfile bundle install
python3 scripts/prepare_docs.py
BUNDLE_GEMFILE=docs/Gemfile bundle exec jekyll build --source _build/docs-source/zh --destination _site/zh --config _build/docs-source/zh/_config.yml,_build/docs-source/zh/_config.language.yml
BUNDLE_GEMFILE=docs/Gemfile bundle exec jekyll build --source _build/docs-source/en --destination _site/en --config _build/docs-source/en/_config.yml,_build/docs-source/en/_config.language.yml
python3 scripts/prepare_docs.py --finalize
```

Generated files are ignored by Git. Builds do not call OCR/models. Dependencies,
theme downloads, and Mermaid need internet. For renaming, change the script's
default base URL and repository links. For a custom domain, update `url`, pass
`--baseurl ''` to both workflow script steps, and configure the domain in Pages.

## Troubleshooting

Confirm **GitHub Actions** is the Pages source and the workflow is on `main`.
Read the failing step: build errors may involve front matter, missing translation
revisions, dependencies, or theme downloads; deployment errors may involve Pages
enablement, permissions, or environment restrictions. Check deployment success
before investigating a 404. URL casing must match `FinFlow`. Committing a workflow
alone does not enable Pages.
