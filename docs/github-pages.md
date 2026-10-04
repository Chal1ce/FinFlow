---
title: 发布文档网站
nav_order: 2
parent: 运维与排错
---

# 使用 GitHub Pages 发布双语文档

`.github/workflows/docs.yml` 分别构建中文、英文 Just the Docs 站点，合并发布。
两种语言的侧栏和搜索彼此独立，顶部可切换到对应页面。

## 首次启用或从旧方式迁移

1. 将文档和工作流提交并推送到 `main`。
2. 打开[仓库 Pages 设置](https://github.com/Chal1ce/FinFlow/settings/pages)。
3. 在 **Build and deployment → Source** 选择 **GitHub Actions**。
4. 打开 [Actions](https://github.com/Chal1ce/FinFlow/actions)，选择 **Documentation**。
5. 如推送时还未启用 Pages，点击 **Run workflow**，选择 `main`，重新运行。
6. 等待 `build` 和 `deploy` 都成功，再通过 Pages 设置的 **Visit site** 打开网站。

旧的 `Deploy from a branch` / `main` + `/docs` 方式需要切换，无法完成本项目的双语构建。
官方步骤见 [GitHub 自定义 Pages 工作流说明](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)。

## 网站地址

- [默认入口](https://chal1ce.github.io/FinFlow/)：跳转中文首页。
- [中文](https://chal1ce.github.io/FinFlow/zh/)。
- [English](https://chal1ce.github.io/FinFlow/en/)。

旧的 `/FinFlow/quick-start.html` 等页面会跳转到对应中文页，浏览器脚本保留查询参数和锚点。
根目录仅提供兼容跳转；站内搜索使用各语言自身的索引。

## 日常更新

中文源文件在 `docs/*.md`，英文在 `docs/en/*.md`，图片共用 `docs/assets/images/`。
`Chal1ce/FinFlow` 的 `main` 上的文档、构建脚本或工作流改动自动部署。
PR、闭源同步仓库 `fin-doc-governance` 和其他仓库只构建，不上传 Pages 产物、不部署；
这些运行中的 `deploy` 显示 **Skipped** 是预期行为。手动发布也必须选择公开仓库的 `main`。
新增翻译和更新原文时见[文档维护指南](docs-authoring.md)。

Just the Docs 固定为 `v0.12.0`。共同设置在 `docs/_config.yml`；语言配置与编辑链接
由 `scripts/prepare_docs.py` 生成。构建依赖在 `docs/Gemfile`，需要 Ruby 3.3 和 Python 3。

## 本地构建

以下构建命令不调用 OCR 或模型：

```sh
BUNDLE_GEMFILE=docs/Gemfile bundle install
python3 scripts/prepare_docs.py
BUNDLE_GEMFILE=docs/Gemfile bundle exec jekyll build --source _build/docs-source/zh --destination _site/zh --config _build/docs-source/zh/_config.yml,_build/docs-source/zh/_config.language.yml
BUNDLE_GEMFILE=docs/Gemfile bundle exec jekyll build --source _build/docs-source/en --destination _site/en --config _build/docs-source/en/_config.yml,_build/docs-source/en/_config.language.yml
python3 scripts/prepare_docs.py --finalize
```

输出为 `_site/`，生成文件不会提交。主题首次获取、依赖安装和 Mermaid 渲染需要联网。

## 仓库改名或自定义域名

默认地址固定为 `https://chal1ce.github.io/FinFlow/`。改名时同步修改脚本默认 `--baseurl`、
`docs/_config.yml` 的 `url` 和仓库链接，以及 README、文档的 GitHub / Pages 链接。
迁移到其他仓库或使用 fork 发布时，还需修改工作流上传步骤和 `deploy` 的两处
`github.repository == 'Chal1ce/FinFlow'` 条件，使其匹配自己的发布仓库。
自定义域名需修改 `url`，在工作流两处脚本命令传 `--baseurl ''`，并配置 Pages 域名。

## 发布失败排查

- 确认 Pages Source 为 **GitHub Actions**，工作流已推送到 `main`。
- 在 **Documentation** 中区分构建失败与部署失败，查看失败步骤日志。
- 构建失败常见于 front matter、主题下载、依赖安装；新翻译还需登记原文版本。
- 部署失败时检查 Pages 是否启用、Actions 权限及 `github-pages` 环境限制。
- 出现 `No artifacts named "github-pages"` 时，查看同次运行的上传日志和 Artifacts 列表。
  上传成功但产物不可见时，不要仅重跑 `deploy`；在公开仓库选择 **Re-run all jobs**，
  重新构建并上传产物。若仍失败，再检查 GitHub Actions/Pages 服务状态和产物是否被删除。
- 404 时先确认 `deploy` 已成功；路径 `FinFlow` 大小写需要一致。

提交工作流不等于网站已经发布。首次部署结果以 Actions 和 Pages 设置为准。
