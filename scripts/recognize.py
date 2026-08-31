"""管道A：周程表 VLM 识别（Qwen-VL-Max）。

将周程表图片交给多模态大模型，提取结构化日程信息。
要求模型严格输出合法 JSON，便于程序解析。
"""
from __future__ import annotations

import json
from datetime import datetime

from dashscope import MultiModalConversation

from common import CST, get_env

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
          "tag": "live|show|special|rest",
          "group_type": "none|asoul|xinyi_sinuo|zhijiang_variety",
          "format": "normal|theater|night_talk|game_room|collab|commercial"
        }
      ]
    }
  ]
}

3. member 只能是：bella, jiaran, nailin, xinyi, sinuo
   无法确定时填 "unknown"；团播/多人企划条目一律填 "unknown"
4. 某天没有安排，events 为空数组 []
5. "休息"字样对应 tag 填 "rest"
6. 时间统一为24小时制 "HH:MM"
7. 一周7天必须全部列出，不能遗漏
7a. 今天是 {today}。海报上通常只印月日不印年份，week_start / week_end / 各天 date
    的年份必须按今天所在年份推算（周程表覆盖「本周或下周」），
    严禁照抄海报上可能缺失/错误的年份
8. group_type 团播分组判断：单人直播一律填 "none"；
   多人企划/团播条目按参与成员判断：
   - 一期生全员（贝拉/嘉然/乃琳）或标注"A-SOUL"的团播 → "asoul"
   - 心宜+思诺双人企划 → "xinyi_sinuo"
   - 一期+二期共同参与或标注"枝江综艺" → "zhijiang_variety"
9. format 直播形式判断：普通直播填 "normal"；
   标题含"小剧场" → "theater"；含"夜谈" → "night_talk"；
   含"游戏室"/"游戏回" → "game_room"；含"联动" → "collab"；
   含"工商"/商务合作 → "commercial"
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

    # 注入当前日期（含年份）：海报通常不印年份，避免 VLM 把年份认错
    today = datetime.now(CST).strftime("%Y-%m-%d")
    prompt = RECOGNITION_PROMPT.replace("{today}", today)

    response = MultiModalConversation.call(
        model="qwen-vl-max",
        messages=[
            {
                "role": "user",
                "content": [
                    {"image": image_url},
                    {"text": prompt},
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
