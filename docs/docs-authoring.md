---
title: 编写文档与添加图片
parent: 参考与设计
nav_order: 2
---

# 编写文档与添加图片

本项目使用 Just the Docs，文档保存在 `docs/`。Markdown 内容同时用于 GitHub 仓库阅读和 Pages 网站发布。

## 图片放在哪里

统一放在 `docs/assets/images/`，可按用途建立子目录，例如：

```text
docs/
├── assets/images/
│   ├── pipeline-overview.svg
│   ├── screenshots/preflight.png
│   └── examples/table-crop.png
├── index.md
└── data-flywheel.md
```

截图可用 PNG，照片可用 JPG / WebP，矢量示意图可用 SVG。文件名使用英文、小写和短横线，便于引用。

## Markdown 图片写法

对于位于 `docs/` 顶层的文档，使用相对路径：

```markdown
![处理流程概览](assets/images/pipeline-overview.svg)
```

![处理流程概览](assets/images/pipeline-overview.svg)

这样在 GitHub 和 Pages 中都能解析；不要写电脑上的绝对路径，或以 `/assets/` 开头的站点根路径。
如果文档在子目录，例如 `docs/guides/example.md`，对应图片路径为 `../assets/images/pipeline-overview.svg`。

图片旁可以写普通段落作为图注。主题会让宽图适应正文宽度，表格和代码块可横向滚动。

## 控制图片大小

需要指定宽度、懒加载时，可以使用 HTML；下例仍然使用相对路径：

```html
<img src="assets/images/pipeline-overview.svg"
     alt="处理流程概览" width="800" loading="lazy">
```

## 新增页面与左侧导航

每篇 Markdown 顶部写 YAML front matter。`title` 是导航名称，`parent` 必须与父页面的 `title` 完全一致，`nav_order` 决定同级排序：

```yaml
---
title: 表格处理示例
parent: 数据处理
nav_order: 5
---
```

随后写正文。参考[主题导航说明](https://just-the-docs.github.io/just-the-docs/docs/navigation/main/)。

## 页内目录

正文目录使用：

```markdown
## 本页目录

1. TOC
{:toc}
```

其中 `{:toc}` 是 Jekyll / Kramdown 语法，用于 Pages 中自动生成标题目录。

## Mermaid 流程图

网站已经启用 Mermaid，和 GitHub 一样可以使用 `mermaid` 代码块：

````markdown
```mermaid
flowchart LR
    PDF --> OCR --> 治理 --> 预训练数据
```
````

网站从 jsDelivr 加载 Mermaid；无法访问该 CDN 时，图可能不能渲染。需要离线或固定效果时，使用 SVG / PNG 图片。

## 更新与发布

新增图片和 Markdown 后一起提交、推送。Pages 的首次启用和仓库改名后的地址调整见[发布说明](github-pages.md)。
