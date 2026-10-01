---
title: OCR 部署与连接
nav_order: 2
parent: 数据处理
---

# 远程 PaddleOCR / PP-StructureV3 部署
{: .no_toc }

## 本页目录
{: .no_toc }

1. TOC
{:toc}


[文档首页](index.md) · [项目首页](https://github.com/Chal1ce/FinFlow/blob/main/README.md)

## 1. 先区分你之前的两段代码

`process_ocr_pdfs()` 是本地推理：它在当前Python进程中实例化 `PPStructureV3`，只能读取当前机器上的文件。

`process_llm_pdfs()` 是HTTP调用：它把文件转成Base64发送到服务端。它使用了 `Authorization: token ...`，更像云端或旧版自建接口的协议。

PaddleOCR 3.x官方推荐使用 PaddleX Basic Serving。官方服务的默认接口是：

```text
POST /layout-parsing
```

默认情况下，返回结果中的图片和其他二进制内容是Base64，而不是图片URL。本项目的
[remote/paddle_client.py](https://github.com/Chal1ce/FinFlow/blob/main/remote/paddle_client.py) 同时兼容Base64和URL两种结果格式，
Token也是可选的。

官方文档：

- [PP-StructureV3 Pipeline](https://www.paddleocr.ai/latest/en/version3.x/pipeline_usage/PP-StructureV3.html)
- [PaddleOCR Serving](https://www.paddleocr.ai/latest/en/version3.x/inference_deployment/serving/serving.html)
- [PaddleOCR Installation](https://www.paddleocr.ai/latest/en/version3.x/installation.html)

## 2. 服务器端安装

以下命令在服务器执行。PaddlePaddle的GPU包必须根据服务器的CUDA、显卡驱动和Python版本，按照官方安装页选择，不能盲目固定一个版本。

```bash
conda create -n fin-paddleocr python=3.10 -y
conda activate fin-paddleocr

# 先按照官方 PaddlePaddle 安装页安装匹配服务器CUDA的推理引擎
# 再安装PP-StructureV3依赖
python -m pip install -U "paddleocr[doc-parser]"

# 如果环境中没有 paddlex 命令，再补装PaddleX
python -m pip install -U paddlex
paddlex --install serving
```

安装完成后检查：

```bash
python -c "import paddle; import paddleocr; print(paddle.__version__); print(paddleocr.__version__)"
paddlex --help
nvidia-smi
```

## 3. 配置长PDF

金融年报通常有上百页。官方服务对超过10页的PDF默认可能只处理前10页，需要把 `max_num_input_imgs` 设为 `null`。

先在服务器生成一份Pipeline配置：

```bash
mkdir -p /home/csn/paddleocr
python - <<'PY'
from paddleocr import PPStructureV3

pipeline = PPStructureV3()
pipeline.export_paddlex_config_to_yaml("/home/csn/paddleocr/PP-StructureV3.yaml")
PY
```

在生成的YAML中确认或补充：

```yaml
Serving:
  extra:
    max_num_input_imgs: null
```

如果服务器没有GPU，把启动命令中的 `--device gpu` 改成 `--device cpu`。

## 4. 先前台启动验证

推荐先只绑定服务器本机地址，再通过SSH隧道访问，避免把未认证的推理端口暴露到公网：

```bash
paddlex --serve \
  --pipeline /home/csn/paddleocr/PP-StructureV3.yaml \
  --host 127.0.0.1 \
  --port 8080 \
  --device gpu
```

看到类似下面的日志才表示服务已监听：

```text
Uvicorn running on http://127.0.0.1:8080
```

如果希望在内网直接访问，可以改成 `--host 0.0.0.0`，但必须配合服务器防火墙、VPN或反向代理，不建议直接开放公网。

## 5. 本地主机通过SSH隧道访问

在本地主机另开一个终端，保持隧道运行：

```bash
ssh -N -L 18080:127.0.0.1:8080 user@your-server
```

此时本地主机的 `127.0.0.1:18080` 会转发到服务器的 `127.0.0.1:8080`。然后在本项目根目录执行：

```bash
python -m remote.paddle_client \
  --api-url http://127.0.0.1:18080/layout-parsing \
  --input data/raw_pdfs/2023/600519/600519_2023_annual.pdf \
  --output-root data/parsed_md
```

输出结构：

```text
data/parsed_md/
└── 600519_2023_annual/
    ├── output.md
    ├── result.json
    ├── ocr_segments.json
    ├── segments/
    │   └── segment-0001/result.json
    └── images/
```

如果你使用的是旧的带Token服务，只需要加：

```bash
--token "$PADDLEOCR_LOCAL_TOKEN"
```

隧道保持运行时，把本地地址写入项目 `.env`，批量采集就能直接使用自建服务：

```bash
export PADDLEOCR_LOCAL_API_URL="http://127.0.0.1:18080/layout-parsing"
export PADDLEOCR_LOCAL_TIMEOUT_SECONDS="900"
export PADDLEOCR_LOCAL_PAGE_BATCH_SIZE="20"
```

`PADDLEOCR_LOCAL_PAGE_BATCH_SIZE` 只限制单次 Serving 请求的页数，不限制一份 PDF 的
总页数。客户端会把成功分段保存在 `segments/`，后续运行只重试失败或缺失段；只有全部
页段成功并完成覆盖校验后才写出合并的 `result.json` 和 `output.md`。

```bash
python -m spiders.scholarly_collector \
  --config config/collection.json \
  --ocr-backend local
```

它会把本轮已下载的 PDF 通过本地 `127.0.0.1:18080` 转发到服务器 OCR，不再占用官方云端队列。

## 6. 官方 PaddleOCR 云端 API

官方云端接口和服务器上的 PaddleX Serving 不是同一个协议。它是异步任务接口：
提交文件后得到 `jobId`，客户端轮询任务状态，完成后下载 `resultUrl.jsonUrl` 指向的
JSONL 文件。

任务提交和状态轮询使用 `Authorization: Bearer <Token>`。但 `resultUrl.jsonUrl` 及 JSONL 中
引用的图片 URL 是官方返回的 BOS 短链，链接自身已包含临时授权参数，下载时**不能**附带
Bearer Token；否则 BOS 会把请求识别为 BCE 鉴权请求，并因缺少 BCE 的 `date` / `x-bce-date`
和签名而返回 `MissingDateHeader`。客户端已自动区分这两类请求。

你在消息中贴出的 Token 已经暴露，不能继续使用它，应该立即在官方控制台撤销并生成
新 Token。新 Token 只通过环境变量提供：

```bash
export PADDLEOCR_CLOUD_TOKEN="新Token"
export PADDLEOCR_CLOUD_MODEL="PaddleOCR-VL-1.6"
```

项目的运行时配置集中在 [config.py](https://github.com/Chal1ce/FinFlow/blob/main/config.py)，常用配置包括：

```bash
export FIN_DOC_DATA_ROOT="data"
export PADDLEOCR_CLOUD_POLL_INTERVAL_SECONDS="5"
export PADDLEOCR_CLOUD_TIMEOUT_SECONDS="1800"
```

官方云端任务队列已满时接口会返回 `code 10010`。客户端会按指数退避重试提交，
默认最多 3 次、间隔 10 秒，可通过环境变量调整：

```bash
export PADDLEOCR_CLOUD_SUBMIT_RETRY_ATTEMPTS="3"
export PADDLEOCR_CLOUD_SUBMIT_RETRY_BACKOFF_SECONDS="10"
```

自建服务器 Serving 的地址和参数也使用统一配置，例如：

```bash
export PADDLEOCR_LOCAL_API_URL="http://127.0.0.1:18080/layout-parsing"
export PADDLEOCR_LOCAL_TIMEOUT_SECONDS="900"
export PADDLEOCR_LOCAL_PAGE_BATCH_SIZE="20"
```

使用本地 PDF：

```bash
python -m remote.paddle_cloud_client \
  --input data/raw_pdfs/2023/600519/600519_2023_annual.pdf \
  --output-root data/parsed_md/cloud
```

使用官方 API 可访问的文件 URL：

```bash
python -m remote.paddle_cloud_client \
  --file-url "https://example.com/report.pdf" \
  --output-root data/parsed_md/cloud
```

每个文件会生成一个派生目录，包含：

```text
data/parsed_md/cloud/<文件名>/
├── job.json
├── result.jsonl
├── output.md
├── pages/page_0001.md
└── images/
    ├── markdown/
    └── output/
```

官方 API 的默认参数是 `PaddleOCR-VL-1.6`，文档代码中的三个可选能力默认关闭；可用
JSON 环境变量覆盖：

```bash
export PADDLEOCR_CLOUD_OPTIONAL_PAYLOAD='{"useDocOrientationClassify":false,"useDocUnwarping":false,"useChartRecognition":false}'
```

云端客户端也兼容结果中的 URL 图片和 Base64 图片，但测试不会访问真实云端服务。

## 7. 服务器常驻运行

前台验证通过后，可以使用systemd。将下面的路径替换成服务器实际路径：

```ini
[Unit]
Description=PP-StructureV3 PaddleX Serving
After=network.target

[Service]
Type=simple
User=csn
WorkingDirectory=/home/csn/paddleocr
Environment=PATH=/home/csn/miniconda3/envs/fin-paddleocr/bin:/usr/bin:/bin
ExecStart=/home/csn/miniconda3/envs/fin-paddleocr/bin/paddlex --serve --pipeline /home/csn/paddleocr/PP-StructureV3.yaml --host 127.0.0.1 --port 8080 --device gpu
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

保存到 `/etc/systemd/system/fin-paddleocr.service` 后执行：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now fin-paddleocr
sudo systemctl status fin-paddleocr
journalctl -u fin-paddleocr -f
```

## 8. Base64和文件传输的取舍

当前客户端把整个PDF编码成Base64，最适合先打通流程。Base64会增加约三分之一的请求体积，超大年报可能占用较多内存。

后续批量处理时，可以改成：

```text
本地 rsync/scp PDF 到服务器
        ↓
服务器服务读取服务器本地文件或可访问URL
        ↓
客户端只下载 Markdown、JSON和图片结果
```

但第一阶段建议先使用SSH隧道 + Base64客户端，验证服务、结果格式和长PDF配置都正确后，再做批量传输优化。
