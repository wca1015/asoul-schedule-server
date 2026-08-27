"""管道A：周程表 VLM 识别（Qwen-VL-Max）。

将周程表图片交给多模态大模型，提取结构化日程信息。
要求模型严格输出合法 JSON，便于程序解析。
"""
from __future__ import annotations

import json

from dashscope import MultiModalConversation

from common import get_env

RECOGNITION_PROMPT = """你是一个A-SOUL周程表信息提取助手。

请仔细观察这张日程表图片，提取所有直播安排信息。

输出要求：
1. 严格输出合法JSON，不要包含任何markdown标记、注释或多余文字
2. 按以下结构输出：
{
  "week_start": "YYYY-MM-DD",
  "week_end": "YYYY-MM-DD",
  "days": [
    {
      "date": "YYYY-MM-DD",
      "weekday": "星期X",
      "events": [
        {
          "time": "HH:MM",
          "member": "成员英文名",
          "title": "直播标题",
          "desc": "副标题或描述，没有则留空字符串",
          "tag": "live|show|special|rest"
        }
      ]
    }
  ]
}

3. member 只能是：bella, jiaran, nailin, xinyi, sinuo
   无法确定时填 "unknown"
4. 某天没有安排，events 为空数组 []
5. "休息"字样对应 tag 填 "rest"
6. 时间统一为24小时制 "HH:MM"
7. 一周7天必须全部列出，不能遗漏
"""


def _strip_code_fence(raw: str) -> str:
    """去掉模型可能输出的 markdown 代码块标记。"""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw
    for fence in ("```json", "```"):
        raw = raw.replace(fence, "")
    return raw.strip()


def recognize_schedule(image_url: str) -> dict:
    """调用 Qwen-VL-Max 识别周程表图片，返回解析后的 dict。"""
    get_env("DASHSCOPE_API_KEY")  # dashscope 依赖该环境变量，提前校验

    response = MultiModalConversation.call(
        model="qwen-vl-max",
        messages=[
            {
                "role": "user",
                "content": [
                    {"image": image_url},
                    {"text": RECOGNITION_PROMPT},
                ],
            }
        ],
        result_format="message",
    )

    if response.status_code != 200:
        raise RuntimeError(f"VLM 调用失败: {response.code} {response.message}")

    raw_text = response.output.choices[0].message.content[0]["text"]
    cleaned = _strip_code_fence(raw_text)
    return json.loads(cleaned)
