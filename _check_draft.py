"""临时验证脚本：确认 draft.json 年份修正后合法。"""
import sys
import json
from datetime import date

sys.path.insert(0, "scripts")
from validate import validate_schedule

data = json.load(open("data/draft.json", encoding="utf-8"))
print("week_start:", data["week_start"], "| week_end:", data["week_end"])

ok = True
for d in data["days"]:
    y, m, dd = map(int, d["date"].split("-"))
    real_wd = "星期" + "一二三四五六日"[date(y, m, dd).weekday()]
    match = real_wd == d["weekday"]
    if not match:
        ok = False
    print(f"  {d['date']} 标注={d['weekday']} 实际={real_wd} {'✓' if match else '✗'}")

errors = validate_schedule(data)
print("validate_schedule:", "通过" if not errors else errors)
print("日期与星期全部匹配:", ok)
