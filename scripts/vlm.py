"""视觉大模型调用的统一入口（候选模型 + 自动降级）。

百炼会下线/改名模型：2026-10 的 qwen-vl-max 因下线返回 404
"Model not exist."，导致管道A 的周程表识别连续失败一周，且没有替代模型，
只能等人工发现。这里按 `common.vlm_models()` 的顺序依次尝试候选模型，
只有「模型不存在 / 无权限 / 未开通」这类模型级错误才降级到下一个候选；
图片尺寸、内容审核、超时等其它错误直接抛出，避免把真正的问题掩盖成
「多试几个模型就好」。
"""
from __future__ import annotations

from common import vlm_models

# 模型级错误（换一个模型就可能成功）的特征串，统一转小写后匹配
_MODEL_LEVEL_MARKERS = (
    "model not exist",
    "model_not_found",
    "access denied",
    "accessdenied",
    "not authorized",
    "未开通",
    "无权限",
)


def is_model_level_error(code: object, message: object) -> bool:
    """判断调用失败是否属于「换一个候选模型可能成功」的模型级错误。"""
    text = f"{code or ''} {message or ''}".lower()
    return any(marker in text for marker in _MODEL_LEVEL_MARKERS)


def call_vlm(messages: list[dict], result_format: str = "message"):
    """调用多模态大模型，返回成功的 response；模型级失败时自动降级。"""
    from dashscope import MultiModalConversation

    candidates = vlm_models()
    failures: list[str] = []
    for index, model in enumerate(candidates):
        response = MultiModalConversation.call(
            model=model,
            messages=messages,
            result_format=result_format,
        )
        if response.status_code == 200:
            if index > 0:
                print(f"[vlm] 已降级使用模型 {model}（前序失败: {'; '.join(failures)}）")
            return response

        detail = f"{model}: {response.code} {response.message}"
        if not is_model_level_error(response.code, response.message):
            raise RuntimeError(f"VLM 调用失败: {response.code} {response.message}")
        failures.append(detail)
        print(f"[vlm] 模型 {model} 不可用（{response.code} {response.message}），尝试下一个候选")

    raise RuntimeError("VLM 调用失败（所有候选模型均不可用）: " + "; ".join(failures))
