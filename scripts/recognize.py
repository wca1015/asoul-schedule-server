"""管道A：周程表 VLM 识别（Qwen-VL-Max）。

将周程表图片交给多模态大模型，提取结构化日程信息。

识别采用「按列提取」策略：让模型先转抄 7 个日期列头，再逐列列出卡片
（而不是直接推算整张网格的日期）——2026-09-14 与 2026-09-21 两次
「整体错位一天」事故均源于模型把卡片配错了列；按列提取 + 列头日期
程序化锚定可消除该错位（列头是印刷体日期，可靠性远高于视觉对齐）。

要求模型严格输出合法 JSON，便于程序解析。
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta

from dashscope import MultiModalConversation

from common import CST, get_env

COLUMN_SCHEMA_PROMPT = """你是一个A-SOUL周程表信息提取助手。

这是一张枝江娱乐「直播日历」海报：顶部一行 7 个日期列头（如 09.21 星期一），
每个日期列头下方悬挂着当天的直播卡片。请按列提取，输出 JSON：

{
  "week_start": "YYYY-MM-DD",
  "week_end": "YYYY-MM-DD",
  "columns": [
    {"label": "照抄第1列的列头文字", "events": []},
    {"label": "照抄第2列的列头文字", "events": []},
    ...共 7 项，从左到右
  ]
}

每个 event 的字段：
{
  "time": "HH:MM",
  "member": "成员英文名",
  "title": "直播标题",
  "desc": "副标题或描述，没有则留空字符串",
  "tag": "live|show|special|rest",
  "group_type": "none|asoul|xinyi_sinuo|zhijiang_variety",
  "format": "normal|theater|night_talk|game_room|collab|commercial"
}

规则：
1. 严格输出合法JSON，不要包含任何markdown标记、注释或多余文字
2. label 必须逐字照抄该列的列头文字（如 "09.22 星期二"）。7 个列头从左到右
   依次输出；即使某列没有卡片，该列的 label 也必须输出
3. 卡片归属：卡片悬挂在其所属日期的列头正下方，水平方向与列头对齐；
   请逐列核对后再写，严禁把某列的卡片记到相邻列（历史上曾因列错位导致
   整周日程整体提前一天）。若某列显示「休息日」「中秋快乐」「勿忘国耻」
   等占位文字，该列 events 为空数组 []
   ⚠️ 特别注意：第 1 列（week_start）常常没有卡片（如显示「休息日」），
   千万不要把第 2 列的卡片挪到第 1 列
4. time 统一 24 小时制 "HH:MM"
5. member 只能是：bella, jiaran, nailin, xinyi, sinuo, unknown，
   或一期双人组合（bella_jiaran / bella_nailin / jiaran_nailin，
   组合内两个 key 按字典序 贝拉<嘉然<乃琳 排列，用下划线连接）：
   - 单人直播：填对应成员英文名
   - 一期双人直播（海报标注"X&Y直播"）：填对应组合键，例如"乃琳&贝拉直播" → "bella_nailin"
   - 一期全员团播 / "A-SOUL"团播 / 心宜+思诺 / 枝江综艺 等多人企划条目：一律填 "unknown"
   - 无法确定时填 "unknown"
6. title 必须严格照抄卡片上的文字（含标点与省略号），不得改写或同音/形近替换
   （如「你的宾果！不，是你的宾果」不要写成「你的宴果……」）
7. 年份推算：海报只印月日不印年份，week_start / week_end 的年份
   必须按今天（{today}）所在年份推算（周程表覆盖「本周或下周」），
   严禁照抄海报上可能缺失/错误的年份
8. group_type 团播分组判断：单人直播与一期双人直播一律填 "none"；
   多人企划/团播条目按海报角标与实际参与成员判断：
   - 海报标注"A-SOUL"（如"A-SOUL夜谈"）或一期生全员参与的团播 → "asoul"
   - 海报标注"心宜思诺"（如"心宜思诺的聊天室"）或心宜+思诺双人企划 → "xinyi_sinuo"
     （「有点宜思的世界」「命题KTV」等心宜思诺专属节目同属此列，勿误填 zhijiang_variety）
   - 一期+二期共同参与或海报标注"枝江综艺" → "zhijiang_variety"
   ⚠️ 当海报把"乃琳&贝拉直播"等一期双人单列成一条时：
   不要因为它包含两位一期生就填 "asoul"，更不要误判成"枝江综艺"——
   按第5条把 member 填成组合键，group_type 一律填 "none"
9. format 直播形式判断：一律以海报明确标注/角标为准，不要凭标题联想：
   - 标注"夜谈"（如"A-SOUL夜谈"） → "night_talk"
   - 标注或标题含"小剧场" → "theater"
   - 标注"游戏室"/"游戏回" → "game_room"
   - 标注"联动"/"连麦" → "collab"
   - 仅当标题明确含"工商"/"商务"/品牌合作字样 → "commercial"（绝不凭空猜"工商"）
   - 其余（普通直播、只标"节目"但没有具体形式词的）→ "normal"
"""

_WEEKDAY_CN = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

# 列头日期写法：09.21 / 9/21 / 9-21 / 9月21日 / 9月21号
_LABEL_DATE_RE = re.compile(r"(\d{1,2})\s*[.\-/月]\s*(\d{1,2})")


def _strip_code_fence(raw: str) -> str:
    """去掉模型可能输出的 markdown 代码块标记。"""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw
    for fence in ("```json", "```"):
        raw = raw.replace(fence, "")
    return raw.strip()


def _parse_iso_date(value) -> date | None:
    """解析 YYYY-MM-DD；失败返回 None（不抛异常）。"""
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _adjust_year(d: date, today: date) -> date:
    """海报不印年份：按今天年份推算后校正跨年偏差（如 12 月底看 01.04）。"""
    try:
        if (d - today).days > 180:
            return d.replace(year=d.year - 1)
        if (today - d).days > 180:
            return d.replace(year=d.year + 1)
    except ValueError:
        pass
    return d


def _parse_label_date(label: str, today: date) -> date | None:
    """从列头文字（如 "09.22 星期二"）解析出日期；失败返回 None。"""
    m = _LABEL_DATE_RE.search(label or "")
    if not m:
        return None
    try:
        d = date(today.year, int(m.group(1)), int(m.group(2)))
    except ValueError:
        return None
    return _adjust_year(d, today)


def _columns_to_schedule(data: dict) -> dict:
    """把「按列」识别结果转换为标准 week_start/week_end/days 结构。

    日期以每列 label（列头文字）解析结果为准——这是修复「整体错位一天」的
    关键：即使模型把卡片放错了列，只要列头转抄正确，日期以上下对齐的列头
    为准。label 无法解析或与 week_start 不连续时，回退为该列序号推算
    （week_start + i），保证流程不中断。
    """
    today = datetime.now(CST).date()
    columns = data.get("columns")
    if not isinstance(columns, list) or not columns:
        raise ValueError("识别结果缺少 columns 列表")

    week_start = _parse_iso_date(data.get("week_start"))
    if week_start is None:
        for col in columns:
            week_start = _parse_label_date(str(col.get("label", "")), today)
            if week_start is not None:
                break
    if week_start is None:
        raise ValueError("无法确定 week_start（列头均无法解析出日期）")

    days: list[dict] = []
    for i, col in enumerate(columns):
        label_date = _parse_label_date(str(col.get("label", "")), today)
        if label_date is None or not (0 <= (label_date - week_start).days <= 6):
            if label_date is not None:
                print(
                    f"[recognize] 第{i + 1}列 label={col.get('label')!r} 与 week_start "
                    f"不连续（可能解析异常），回退按列序推算"
                )
            label_date = week_start + timedelta(days=i)
        days.append(
            {
                "date": label_date.isoformat(),
                "weekday": _WEEKDAY_CN[label_date.weekday()],
                "events": col.get("events") or [],
            }
        )

    days.sort(key=lambda d: d["date"])
    return {
        "week_start": days[0]["date"],
        "week_end": days[-1]["date"],
        "days": days,
    }


def recognize_schedule(image_url: str) -> dict:
    """调用 Qwen-VL-Max 识别周程表图片，返回 {week_start, week_end, days}。

    提示词要求模型「按列」输出（columns + label 列头）；若模型仍按旧结构
    （days）输出则兼容直接返回。返回结构始终含 days（供校验/发布使用）。
    """
    get_env("DASHSCOPE_API_KEY")  # dashscope 依赖该环境变量，提前校验

    # 注入当前日期（含年份）：海报通常不印年份，避免 VLM 把年份认错
    today = datetime.now(CST).strftime("%Y-%m-%d")
    prompt = COLUMN_SCHEMA_PROMPT.replace("{today}", today)

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
    result = json.loads(cleaned)

    # 「按列」结构 → 标准 days 结构（日期以列头为准，程序化锚定防错位）
    if isinstance(result.get("columns"), list):
        result = _columns_to_schedule(result)
    return result
