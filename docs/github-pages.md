---
title: 发布文档网站
nav_order: 2
parent: 运维与排错
---

# 使用 GitHub Pages 发布文档

[文档首页](index.md) · [项目仓库](https://github.com/Chal1ce/FinFlow)

仓库已准备 `docs/index.md`、Just the Docs 主题、侧栏导航、搜索及 Markdown 相对链接转换。
发布来源选择 `main` 分支的 `/docs`，不需要另外复制一份使用说明。

## 首次启用

1. 打开[仓库 Pages 设置](https://github.com/Chal1ce/FinFlow/settings/pages)。
2. 在 **Build and deployment** 下，将 **Source** 设为 **Deploy from a branch**。
3. **Branch** 选择 `main`，目录选择 `/docs`。
4. 点击 **Save**。
5. 在仓库 **Actions** 页面查看 `pages build and deployment` 的构建与部署结果。
6. 成功后，回到 Pages 设置，通过显示的 **Visit site** 打开网站。

默认网站地址为 [https://chal1ce.github.io/FinFlow/](https://chal1ce.github.io/FinFlow/)。
第一次构建完成前，该地址可能返回 404；以 Pages 设置和 Actions 的部署结果为准。
操作对应 [GitHub 官方发布来源说明](https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site)。

## 以后如何更新

编辑 `docs/` 中的 Markdown，然后提交并推送到 `main`，GitHub 会重新构建和发布。
修改主题、网站标题或仓库路径时，编辑 `docs/_config.yml`。

文档内部继续使用 `.md` 相对链接，`jekyll-relative-links` 会转换到网站页面。
指向仓库源码、配置和根目录 README 的链接使用 GitHub 地址，因为这些文件不在 `/docs` 的网站发布范围内。

当前使用 Just the Docs `v0.12.0`，主题版本固定在 `remote_theme` 中。网站提供分组导航、搜索、代码复制和 Mermaid 流程图；图片写法见[编写文档与添加图片](docs-authoring.md)。

## 仓库改名后

在 GitHub 的 Settings → General 修改仓库名后，同步更新：

- `docs/_config.yml` 的 `baseurl`、`repository`、`aux_links` 和 `gh_edit_repository`。
- 文档中指向仓库源码、Pages 设置和网站的 GitHub / `github.io` 链接。
- 本地远程地址：`git remote set-url origin https://github.com/Chal1ce/新仓库名.git`。

本仓库名称为 `FinFlow`，`baseurl` 是 `/FinFlow`，默认网站地址为 `https://chal1ce.github.io/FinFlow/`。仓库名和路径大小写保持一致。
页面内部的相对 Markdown 和图片路径继续有效。

## 发布失败时检查

- 确认发布来源为 `main` + `/docs`，而不是仓库根目录。
- 在 Actions 中打开失败的 `pages build and deployment`，查看出错步骤。
- 若 Pages 设置提示套餐或权限限制，按界面提示处理；GitHub Free 支持公开仓库的 Pages。
- 若刚保存后仍是 404，先确认部署任务已经成功，而不是只看到构建成功。

本仓库提交配置不等于启用 Pages。首次启用需要仓库管理员或维护者在 Settings 中设置发布来源。
