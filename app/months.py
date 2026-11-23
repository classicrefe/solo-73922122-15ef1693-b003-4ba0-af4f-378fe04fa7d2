"""月份工具：统一使用 'YYYY-MM' 字符串，字典序即时间序。"""
import re

_MONTH_RE = re.compile(r"^(\d{4})-(\d{1,2})$")


def parse_month(s):
    """解析 'YYYY-MM'（允许单位数月份），非法返回 None。"""
    if s is None:
        return None
    m = _MONTH_RE.match(str(s).strip())
    if not m:
        return None
    year, month = int(m.group(1)), int(m.group(2))
    if not (1 <= month <= 12):
        return None
    return f"{year:04d}-{month:02d}"


def next_month(month):
    year, mon = int(month[:4]), int(month[5:7])
    mon += 1
    if mon == 13:
        year, mon = year + 1, 1
    return f"{year:04d}-{mon:02d}"


def month_range(start, end):
    """[start, end] 闭区间逐月列表；start > end 时为空。"""
    out = []
    cur = start
    while cur <= end:
        out.append(cur)
        cur = next_month(cur)
    return out
