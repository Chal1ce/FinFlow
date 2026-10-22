"""Actionable error summaries without persisting provider payloads or credentials."""

from __future__ import annotations

GOVERNANCE_MESSAGES = {
    "LLM response did not contain a JSON object": (
        "governance_missing_json", "治理模型未返回 JSON 对象。", "续跑会复用已保存的成功响应，仅重新请求尚未完成的部分。"
    ),
    "LLM response contained invalid JSON": (
        "governance_invalid_json", "治理模型返回了无效 JSON。", "检查模型的 JSON 输出支持；已有成功响应会继续复用。"
    ),
    "governance response refused or truncated": (
        "governance_incomplete_response", "治理模型拒答或输出被截断。", "检查模型输出限制及拒答情况，再续跑已有队列。"
    ),
}


def governance_diagnostic(error):
    """Keep structured inner errors; recognize only fixed legacy governance messages."""
    if error.get("diagnostic"):
        return dict(error["diagnostic"])
    raw = str(error.get("error_msg") or "")
    message = raw.removeprefix("GovernanceError: ")
    if message in GOVERNANCE_MESSAGES:
        code, text, hint = GOVERNANCE_MESSAGES[message]
        return {"error_type": "GovernanceError", "code": code, "message": text, "hint": hint}
    return {
        "code": "governance_failed", "message": "正文治理失败。",
        "hint": "查看嵌套治理步骤或处理报告中的具体失败原因。",
    }


class PipelineStageError(RuntimeError):
    def __init__(self, diagnostic):
        self.diagnostic = diagnostic
        super().__init__(diagnostic["message"])


def download_diagnostic(record):
    """Translate downloader output into fixed messages; never echo arbitrary errors."""
    raw = str(record.get("error_msg") or "")
    if raw == "file does not start with a PDF header":
        code = "download_not_pdf"
        message = "下载结果不是 PDF，可能是网站验证页或错误页。"
        hint = "查看隔离文件；换用已成功的来源，或稍后重试该网站。"
    elif raw.startswith("file is too small:"):
        code, message, hint = "download_too_small", "下载文件过小，未通过 PDF 检查。", "检查来源链接或换用其他来源。"
    elif raw == "PDF end marker %%EOF was not found":
        code, message, hint = "download_incomplete_pdf", "下载文件缺少 PDF 结束标记。", "重试下载，或换用其他来源。"
    elif "HTTP Error 403" in raw or "HTTP Error 429" in raw:
        code, message, hint = "download_access_limited", "来源网站拒绝访问或限流。", "稍后重试，或换用已成功的来源。"
    elif "timed out" in raw.lower() or "TimeoutError" in raw:
        code, message, hint = "download_timeout", "下载请求超时。", "检查网络，稍后重试。"
    else:
        code, message, hint = "download_failed", "PDF 下载失败。", "检查来源链接、网络及本地下载清单。"
    return {
        "code": code,
        "message": message,
        "hint": hint,
        **{key: record[key] for key in ("file_size", "quarantine_path") if record.get(key) is not None},
    }


def exception_diagnostic(exc):
    if isinstance(exc, PipelineStageError):
        return {"error_type": type(exc).__name__, **exc.diagnostic}
    status = getattr(exc, "status_code", getattr(exc, "code", None))
    status = status if isinstance(status, int) and 100 <= status <= 599 else None
    messages = {
        401: ("服务鉴权失败。", "检查当前阶段对应服务的 API key/token。"),
        403: ("服务拒绝访问。", "检查服务权限或来源访问限制。"),
        404: ("服务地址或模型不存在。", "检查当前阶段的 API 地址及模型名称。"),
        429: ("服务限流或配额不足。", "检查服务配额，稍后重试。"),
    }
    name = type(exc).__name__
    if name == "GovernanceError" and str(exc) in GOVERNANCE_MESSAGES:
        return governance_diagnostic({"error_msg": str(exc)})
    if status in messages:
        message, hint = messages[status]
    elif status and status >= 500:
        message, hint = "远端服务暂时失败。", "稍后续跑已有队列。"
    elif "Timeout" in name:
        message, hint = "阶段请求超时。", "检查网络与该阶段的超时配置。"
    elif "Connection" in name:
        message, hint = "无法连接远端服务。", "检查网络、代理及当前阶段的服务地址。"
    elif isinstance(exc, FileNotFoundError):
        message, hint = "阶段所需的本地文件不存在。", "检查任务输入及配置中的文件路径。"
    else:
        message, hint = "阶段执行失败。", "根据 stage、task_uid 和 location 定位任务及代码。"
    result = {"error_type": name, "code": "http_error" if status else "stage_failed", "message": message, "hint": hint}
    if status:
        result["http_status"] = status
    traceback = exc.__traceback__
    while traceback and traceback.tb_next:
        traceback = traceback.tb_next
    if traceback:
        from pathlib import Path

        result["location"] = f"{Path(traceback.tb_frame.f_code.co_filename).name}:{traceback.tb_lineno}"
    return result
