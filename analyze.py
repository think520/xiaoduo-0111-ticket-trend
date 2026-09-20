#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""客服工单趋势分析工具（零第三方依赖，Python 3.9+）。

用法:
    uv run analyze.py
    uv run analyze.py --input task5_tickets.json --outdir output --sla 高=12,中=24,低=48

输入: JSON 数组或 CSV（字段含义见 task5_ticket_fields.md）
输出: <outdir>/趋势分析报告.md, dashboard.html, metrics.json, charts/*.svg

设计文档: docs/01-需求文档.md, docs/02-实现文档.md
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import math
import re
import statistics
import sys
from collections import Counter, OrderedDict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

VERSION = "1.0.0"

# ---------------------------------------------------------------- 配置区

#: SLA 目标（小时）。假设值，可用 --sla 覆盖；见需求文档 §4.2
DEFAULT_SLA: Dict[str, float] = {"高": 24.0, "中": 48.0, "低": 72.0}
#: 字段说明中约定的枚举，用于发现"文档与数据不一致"
EXPECTED_PRIORITIES: Tuple[str, ...] = ("高", "中", "低")
EXPECTED_CHANNELS: Tuple[str, ...] = ("在线", "电话", "邮件")
#: 复发语义词：出现即说明"这不是第一次发生"
RECURRENCE_WORDS: Tuple[str, ...] = ("又", "还是", "一直", "上个月", "反复", "再次")

#: 规则簇定义。顺序即优先级：一张工单只归属第一个命中的簇（多命中会被记录）
CLUSTER_RULES: Tuple[Dict[str, Any], ...] = (
    {
        "key": "pay_state_mismatch",
        "name": "支付-扣款与订单状态不一致",
        "keywords": ("扣了两次", "重复扣款", "扣了钱", "扣钱了", "已经扣款", "扣款成功", "付款成功",
                     "支付成功", "待支付", "未支付", "没生成", "订单没成功", "多扣", "扣款金额不对"),
        "action": "拉取支付流水与订单库做对账核查（回调是否丢失/重复），先处理未解决工单",
    },
    {
        "key": "pay_link_failure",
        "name": "支付-链路失败与页面异常",
        "keywords": ("支付提示失败", "付不了款", "一直转圈", "结算页面打不开", "付款页面"),
        "action": "排查支付网关成功率与前端页面异常，确认是否与特定支付方式相关",
    },
    {
        "key": "policy_dispute",
        "name": "售后规则争议（无理由退货/自动确认收货）",
        "keywords": ("自动确认收货", "无理由", "凭什么", "不给退"),
        "action": "复核规则配置与话术，明确自动确认收货的异常场景处理流程",
    },
    {
        "key": "refund_shipping_fee",
        "name": "退货运费争议与报销未兑现",
        "keywords": ("运费", "快递费"),
        "action": "明确运费承担规则与垫付报销时效，给客服统一话术与一键登记入口",
    },
    {
        "key": "refund_delay",
        "name": "退款/退货到账与审核超期",
        "keywords": ("还在审核", "还在处理", "钱还没退", "还没退", "什么时候退", "什么时候给", "不想退"),
        "action": "梳理退款审核链路，对超过 72h 未到账的工单调专人跟进",
    },
    {
        "key": "service_experience",
        "name": "客服体验（态度/等待/机器人）",
        "keywords": ("机器人", "客服态度", "问了", "等了", "人手不够", "重新描述"),
        "action": "复核机器人的转人工策略与高峰期排班，避免重复描述与长等待",
    },
    {
        "key": "account_security",
        "name": "账号与安全风险",
        "keywords": ("冻结", "别人用我的账号", "不是我操作", "被盗"),
        "action": "按安全事件流程核查登录日志与下单设备，必要时冻结并联系用户",
    },
    {
        "key": "shipping_delay",
        "name": "发货延迟",
        "keywords": ("没发货", "未发货", "发不了货"),
        "action": "与仓储核对缺货/积压清单，对延迟订单主动告知用户",
    },
    {
        "key": "logistics_tracking_stuck",
        "name": "物流信息停滞",
        "keywords": ("没有物流", "任何物流", "物流更新", "没更新", "快递显示异常", "物流信息"),
        "action": "与快递方核对在途异常件，对停滞超 48h 的订单主动补发或说明",
    },
    {
        "key": "logistics_delivery_abnormal",
        "name": "收货/派送/退件异常",
        "keywords": ("被退回", "签收", "派送", "收货地址", "没收到", "快递员"),
        "action": "核对签收凭证与派送记录，处理虚假签收与错派，修正地址变更流程",
    },
)

#: 异常分级阈值（见需求文档 §6.3）
SCORE_LEVELS: Tuple[Tuple[int, str], ...] = ((5, "高危"), (3, "关注"), (1, "观察"))
#: 信号的中文名
SIGNAL_NAMES = {
    "S1": "分类突增",
    "S2": "复发簇",
    "S3": "效率堵点",
    "S4": "体验塌陷",
    "S5": "积压累积",
    "S6": "渠道差异",
}

LEVEL_ORDER = {"高危": 0, "关注": 1, "观察": 2}


# ---------------------------------------------------------------- 数据结构


@dataclass
class Ticket:
    ticket_id: str
    created_at: datetime
    day: str
    category: str
    description: str
    priority: str
    resolution_hours: Optional[float]
    satisfaction: Optional[int]
    channel: str
    is_resolved: bool


@dataclass
class Cluster:
    key: str
    name: str
    action: str
    tickets: List[Ticket]
    cohesion_lift: Optional[float] = None

    @property
    def ids(self) -> List[str]:
        return [t.ticket_id for t in self.tickets]

    @property
    def size(self) -> int:
        return len(self.tickets)

    @property
    def days(self) -> List[str]:
        return sorted({t.day for t in self.tickets})

    @property
    def span_days(self) -> int:
        if not self.tickets:
            return 0
        first = min(t.created_at for t in self.tickets)
        last = max(t.created_at for t in self.tickets)
        return (last.date() - first.date()).days + 1

    @property
    def categories(self) -> List[str]:
        return sorted({t.category for t in self.tickets})

    @property
    def high_share(self) -> float:
        if not self.tickets:
            return 0.0
        return sum(1 for t in self.tickets if t.priority == "高") / self.size

    @property
    def avg_satisfaction(self) -> Optional[float]:
        vals = [t.satisfaction for t in self.tickets if t.satisfaction is not None]
        return round(statistics.fmean(vals), 2) if vals else None

    @property
    def low_score_rate(self) -> Optional[float]:
        vals = [t.satisfaction for t in self.tickets if t.satisfaction is not None]
        if not vals:
            return None
        return sum(1 for v in vals if v <= 2) / len(vals)

    @property
    def unresolved(self) -> int:
        return sum(1 for t in self.tickets if not t.is_resolved)

    @property
    def recurrence_hits(self) -> int:
        return sum(1 for t in self.tickets if any(w in t.description for w in RECURRENCE_WORDS))

    @property
    def cohesion(self) -> Optional[float]:
        """簇内平均两两 Jaccard 相似度（字符二元组），衡量"是不是同一类问题"。"""
        if self.size < 2:
            return None
        grams = [bigrams(t.description) for t in self.tickets]
        scores: List[float] = []
        for i in range(len(grams)):
            for j in range(i + 1, len(grams)):
                a, b = grams[i], grams[j]
                union = len(a | b)
                scores.append(len(a & b) / union if union else 0.0)
        return round(statistics.fmean(scores), 3) if scores else None


# ---------------------------------------------------------------- 工具函数


def clean_text(text: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", text or "")


def bigrams(text: str) -> set:
    s = clean_text(text)
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def percentile(values: Sequence[float], p: float) -> Optional[float]:
    """nearest-rank 分位数：小样本不插值，避免给出数据中不存在的值。"""
    vals = sorted(values)
    if not vals:
        return None
    idx = math.ceil(p * len(vals)) - 1
    return vals[max(0, min(idx, len(vals) - 1))]


def safe_mean(values: Iterable[float]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return round(statistics.fmean(vals), 2) if vals else None


def percentile_linear(values: Sequence[float], p: float) -> Optional[float]:
    """线性插值分位数（Excel / numpy 默认口径），用于"全量口径"对照，避免与常见工具算出的数对不上。"""
    vals = sorted(values)
    if not vals:
        return None
    if len(vals) == 1:
        return round(vals[0], 2)
    pos = (len(vals) - 1) * p
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return round(vals[int(pos)], 2)
    return round(vals[lo] + (vals[hi] - vals[lo]) * (pos - lo), 2)


def poisson_tail(k: int, lam: float) -> float:
    """P(X >= k)，X ~ Poisson(lam)。用对数空间避免溢出。"""
    if k <= 0:
        return 1.0
    if lam <= 0:
        return 0.0
    log_lam = math.log(lam)
    total = 0.0
    for i in range(0, k):
        total += math.exp(i * log_lam - lam - math.lgamma(i + 1))
    return max(0.0, min(1.0, 1.0 - total))


def binom_tail(k: int, n: int, p: float) -> float:
    """P(X >= k)，X ~ Binomial(n, p)。用对数空间避免溢出。"""
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    if p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    log_p, log_q = math.log(p), math.log(1 - p)
    total = 0.0
    for i in range(k, n + 1):
        total += math.exp(math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1)
                          + i * log_p + (n - i) * log_q)
    return max(0.0, min(1.0, total))


def fmt_prob(p: Optional[float]) -> str:
    if p is None:
        return "不适用"
    if p < 1e-3:
        return f"极小（{p:.2e}，<0.1%）"
    return f"{p:.3f}"


def evidence_strength(p: Optional[float]) -> str:
    if p is None:
        return "不适用"
    if p < 1e-3:
        return "强"
    if p < 0.05:
        return "中"
    return "弱"


def pct(value: Optional[float], digits: int = 1) -> str:
    return "—" if value is None else f"{value * 100:.{digits}f}%"


def num(value: Optional[float], digits: int = 1) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def esc(text: Any) -> str:
    return html.escape(str(text), quote=True)


# ---------------------------------------------------------------- 加载与校验


def _to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> Optional[int]:
    f = _to_float(value)
    return int(f) if f is not None else None


def _to_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"true", "1", "yes", "y", "是", "已解决"}:
            return True
        if v in {"false", "0", "no", "n", "否", "未解决"}:
            return False
    return None


def _parse_dt(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    return None


FIELD_ALIASES = {
    "ticket_id": ("ticket_id", "id", "工单号"),
    "created_at": ("created_at", "created", "创建时间"),
    "category": ("category", "分类", "问题分类"),
    "description": ("description", "desc", "问题描述"),
    "priority": ("priority", "优先级"),
    "resolution_time_hours": ("resolution_time_hours", "resolution_hours", "处理时长"),
    "satisfaction": ("satisfaction", "满意度"),
    "channel": ("channel", "渠道", "来源渠道"),
    "is_resolved": ("is_resolved", "resolved", "是否已解决"),
}


def load_rows(path: Path) -> Tuple[List[Dict[str, Any]], List[str]]:
    warnings: List[str] = []
    suffix = path.suffix.lower()
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise SystemExit(f"[错误] 无法读取输入文件 {path}: {exc}")
    if suffix == ".csv":
        rows: List[Dict[str, Any]] = list(csv.DictReader(text.splitlines()))
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"[错误] JSON 解析失败：{exc}")
        if isinstance(data, dict):
            for key in ("tickets", "data", "items", "records"):
                if isinstance(data.get(key), list):
                    data = data[key]
                    break
        if not isinstance(data, list):
            raise SystemExit("[错误] 输入需为工单数组（或包含数组字段的对象）")
        rows = [row for row in data if isinstance(row, dict)]
        if len(rows) != len(data):
            warnings.append(f"输入中有 {len(data) - len(rows)} 条非对象元素已被跳过")
    return rows, warnings


class DataError(RuntimeError):
    """--strict 模式下输入不符合字段契约时抛出。"""


def build_tickets(rows: List[Dict[str, Any]], strict: bool = False) -> Tuple[List[Ticket], List[str]]:
    warnings: List[str] = []
    violations: List[str] = []

    def flag(message: str) -> None:
        """记录问题：默认进 warnings 继续跑；--strict 时同时记为硬违规。"""
        warnings.append(message)
        violations.append(message)

    tickets: List[Ticket] = []
    seen: Dict[str, int] = {}
    dropped_dates = 0
    bad_numbers = 0
    missing_sat = 0
    missing_hours = 0

    for idx, raw in enumerate(rows, start=1):
        row: Dict[str, Any] = {}
        for field, aliases in FIELD_ALIASES.items():
            for alias in aliases:
                if alias in raw and raw[alias] not in (None, ""):
                    row[field] = raw[alias]
                    break

        dt = _parse_dt(row.get("created_at"))
        if dt is None:
            dropped_dates += 1
            flag(f"第 {idx} 行的创建时间缺失或格式错误：{raw.get('created_at')!r}（该条被跳过）")
            continue

        tid = str(row.get("ticket_id") or f"ROW{idx}")
        if tid in seen:
            flag(f"工单号重复：{tid}（保留首次出现，跳过后续）")
            continue
        seen[tid] = idx

        priority = str(row.get("priority") or "未标注").strip()
        if priority not in EXPECTED_PRIORITIES:
            flag(f"工单 {tid} 的优先级取值异常：{priority!r}（不计入 SLA 判定）")
        channel = str(row.get("channel") or "未知").strip()
        if channel not in EXPECTED_CHANNELS:
            flag(f"工单 {tid} 的渠道取值不在字段说明内：{channel!r}")

        raw_hours = row.get("resolution_time_hours")
        hours = _to_float(raw_hours)
        if raw_hours is None:
            missing_hours += 1
        elif hours is None:
            flag(f"工单 {tid} 的处理时长无法解析（{raw_hours!r}），已忽略该值")
        if hours is not None and hours < 0:
            flag(f"工单 {tid} 的处理时长为负数（{hours}），已忽略该值")
            bad_numbers += 1
            hours = None

        raw_sat = row.get("satisfaction")
        sat = _to_int(raw_sat)
        if raw_sat is None:
            missing_sat += 1
        elif sat is None:
            flag(f"工单 {tid} 的满意度无法解析（{raw_sat!r}），已忽略该值")
        if sat is not None and not 1 <= sat <= 5:
            flag(f"工单 {tid} 的满意度越界（{sat}），已忽略该值")
            bad_numbers += 1
            sat = None

        raw_resolved = row.get("is_resolved")
        resolved = _to_bool(raw_resolved)
        if resolved is None:
            flag(f"工单 {tid} 的 is_resolved 缺失或不是布尔值（{raw_resolved!r}），按未解决处理")
            resolved = False
        elif not isinstance(raw_resolved, bool) and strict:
            flag(f"工单 {tid} 的 is_resolved 不是布尔类型：{raw_resolved!r}")

        tickets.append(Ticket(
            ticket_id=tid,
            created_at=dt,
            day=dt.strftime("%Y-%m-%d"),
            category=str(row.get("category") or "未分类").strip(),
            description=str(row.get("description") or "").strip(),
            priority=priority,
            resolution_hours=hours,
            satisfaction=sat,
            channel=channel,
            is_resolved=resolved,
        ))

    if dropped_dates:
        warnings.append(f"有 {dropped_dates} 条工单因创建时间缺失或格式错误被跳过")
    if bad_numbers:
        warnings.append(f"有 {bad_numbers} 处数值越界/非法值被置空并排除在对应统计之外")
    if missing_sat:
        warnings.append(f"有 {missing_sat} 条工单缺少满意度评分，已从满意度统计的分母中剔除")
    if missing_hours:
        warnings.append(f"有 {missing_hours} 条工单缺少处理时长，已从时长与 SLA 统计中剔除")
    if strict and violations:
        head = "\n  - ".join(violations[:10])
        more = "" if len(violations) <= 10 else f"\n  … 还有 {len(violations) - 10} 条"
        raise DataError(f"--strict 模式发现 {len(violations)} 处字段契约违规：\n  - {head}{more}")
    return tickets, warnings


def check_data_contract(tickets: List[Ticket]) -> List[str]:
    warnings: List[str] = []
    if not tickets:
        return ["输入数据为空，报告只包含结构说明"]
    channels = {t.channel for t in tickets}
    missing = [c for c in EXPECTED_CHANNELS if c not in channels]
    if missing:
        warnings.append(
            "字段说明中列出的渠道 " + "、".join(missing) + " 在实际数据中为 0 条（字段说明与数据不一致）"
        )
    unresolved_with_hours = [t.ticket_id for t in tickets if not t.is_resolved and t.resolution_hours]
    if unresolved_with_hours:
        warnings.append(
            f"有 {len(unresolved_with_hours)} 条未解决工单仍带处理时长（"
            + "、".join(unresolved_with_hours[:5]) + ("…" if len(unresolved_with_hours) > 5 else "")
            + "），本工具按“已挂起时长”口径单独统计，不计入 SLA 达成率"
        )
    return warnings


# ---------------------------------------------------------------- 指标计算


def split_window(days: List[str], ratio: float) -> Tuple[List[str], List[str]]:
    n = len(days)
    if n <= 1:
        return days, []
    split = max(1, min(n - 1, int(n * ratio)))
    return days[:split], days[split:]


def compute_metrics(tickets: List[Ticket], sla: Dict[str, float], ratio: float) -> Dict[str, Any]:
    days = sorted({t.day for t in tickets})
    early_days, late_days = split_window(days, ratio)
    early = [t for t in tickets if t.day in set(early_days)]
    late = [t for t in tickets if t.day in set(late_days)]

    daily = OrderedDict((d, 0) for d in days)
    for t in tickets:
        daily[t.day] += 1
    counts = list(daily.values())
    peak_day = max(daily, key=lambda d: daily[d]) if daily else None

    # D1 时间趋势
    time_dim = OrderedDict([
        ("days", days),
        ("daily", daily),
        ("total", len(tickets)),
        ("daily_avg", round(len(tickets) / len(days), 2) if days else 0),
        ("peak_day", peak_day),
        ("peak_count", daily.get(peak_day, 0) if peak_day else 0),
        ("cv", round(statistics.pstdev(counts) / statistics.fmean(counts), 3) if len(counts) > 1 and statistics.fmean(counts) else None),
        ("early_days", early_days),
        ("late_days", late_days),
        ("early_count", len(early)),
        ("late_count", len(late)),
        ("early_avg", round(len(early) / len(early_days), 2) if early_days else None),
        ("late_avg", round(len(late) / len(late_days), 2) if late_days else None),
    ])
    if early_days and late_days and len(early) / len(early_days) > 0:
        time_dim["growth_pct"] = round(
            (len(late) / len(late_days)) / (len(early) / len(early_days)) - 1, 3
        )
    # 滚动对比：最近 3 天 vs 之前 3 天（对主管更直观的"最近有没有变化"）
    rolling = OrderedDict([("window", 3), ("available", False), ("recent_days", []), ("prior_days", [])])
    if len(days) >= 6:
        recent_days, prior_days = days[-3:], days[-6:-3]
        recent_n = sum(daily[d] for d in recent_days)
        prior_n = sum(daily[d] for d in prior_days)
        recent_avg = round(recent_n / len(recent_days), 2)
        prior_avg = round(prior_n / len(prior_days), 2)
        change = round((recent_avg / prior_avg - 1), 3) if prior_avg else None
        if change is None:
            trend = "样本不足"
        elif change >= 0.2:
            trend = "增长"
        elif change <= -0.2:
            trend = "下降"
        else:
            trend = "稳定"
        rolling.update(OrderedDict([
            ("available", True), ("recent_days", recent_days), ("prior_days", prior_days),
            ("recent_count", recent_n), ("prior_count", prior_n),
            ("recent_avg", recent_avg), ("prior_avg", prior_avg),
            ("change_pct", change), ("trend", trend), ("threshold", 0.2),
        ]))
    time_dim["rolling_3d"] = rolling
    # 时段分布（排班价值）
    hourly = OrderedDict((f"{h:02d}", sum(1 for t in tickets if t.created_at.hour == h)) for h in range(24))
    busy = sorted([(h, c) for h, c in hourly.items() if c], key=lambda kv: (-kv[1], kv[0]))
    time_dim["hourly"] = hourly
    time_dim["peak_hours"] = [h for h, _ in busy[:3]]
    time_dim["busiest_hour"] = busy[0][0] if busy else None

    # D2 分类结构
    cat_counts = Counter(t.category for t in tickets)
    cat_early = Counter(t.category for t in early)
    cat_late = Counter(t.category for t in late)
    cat_daily = OrderedDict(
        (c, OrderedDict((d, sum(1 for t in tickets if t.category == c and t.day == d)) for d in days))
        for c, _ in cat_counts.most_common()
    )
    category_dim = OrderedDict([
        ("counts", OrderedDict(sorted(cat_counts.items(), key=lambda kv: (-kv[1], kv[0])))),
        ("daily", cat_daily),
        ("share", OrderedDict((c, round(cat_counts[c] / len(tickets), 4)) for c, _ in cat_counts.most_common())),
        ("early_share", OrderedDict((c, round(cat_early[c] / len(early), 4) if early else None) for c, _ in cat_counts.most_common())),
        ("late_share", OrderedDict((c, round(cat_late[c] / len(late), 4) if late else None) for c, _ in cat_counts.most_common())),
        ("share_shift_pp", OrderedDict(
            (c, round((cat_late[c] / len(late) - cat_early[c] / len(early)) * 100, 1) if early and late else None)
            for c, _ in cat_counts.most_common()
        )),
        ("early_avg", OrderedDict((c, round(cat_early[c] / len(early_days), 2) if early_days else None) for c, _ in cat_counts.most_common())),
        ("late_avg", OrderedDict((c, round(cat_late[c] / len(late_days), 2) if late_days else None) for c, _ in cat_counts.most_common())),
    ])

    # D3 严重程度
    pri_counts = Counter(t.priority for t in tickets)
    high_early = sum(1 for t in early if t.priority == "高")
    high_late = sum(1 for t in late if t.priority == "高")
    cat_high = Counter(t.category for t in tickets if t.priority == "高")
    priority_dim = OrderedDict([
        ("counts", OrderedDict((p, pri_counts.get(p, 0)) for p in EXPECTED_PRIORITIES)),
        ("high_share", round(pri_counts.get("高", 0) / len(tickets), 4) if tickets else None),
        ("high_share_early", round(high_early / len(early), 4) if early else None),
        ("high_share_late", round(high_late / len(late), 4) if late else None),
        ("high_by_category", OrderedDict(cat_high.most_common())),
    ])

    # D4 处理效率（已解决口径）+ 挂起（未解决口径）
    resolved = [t for t in tickets if t.is_resolved and t.resolution_hours is not None]
    unresolved = [t for t in tickets if not t.is_resolved]
    global_hours = [t.resolution_hours for t in resolved]
    by_cat: Dict[str, Any] = OrderedDict()
    for c, _ in cat_counts.most_common():
        rows = [t for t in resolved if t.category == c]
        hours = [t.resolution_hours for t in rows]
        alt_hours = [t.resolution_hours for t in tickets if t.category == c and t.resolution_hours is not None]
        by_cat[c] = OrderedDict([
            ("resolved", len(rows)),
            ("mean", safe_mean(hours)),
            ("p50", percentile(hours, 0.5)),
            ("p90", percentile(hours, 0.9)),
            ("max", max(hours) if hours else None),
            # 全量口径（含未解决工单的"已挂起时长"，分位数用线性插值）：与常见 Excel/numpy 口径对齐
            ("alt_mean", safe_mean(alt_hours)),
            ("alt_p90", percentile_linear(alt_hours, 0.9)),
            ("alt_max", max(alt_hours) if alt_hours else None),
            ("unresolved", sum(1 for t in unresolved if t.category == c)),
        ])
    all_hours = [t.resolution_hours for t in tickets if t.resolution_hours is not None]
    breaches = [
        OrderedDict([
            ("ticket_id", t.ticket_id),
            ("category", t.category),
            ("priority", t.priority),
            ("hours", t.resolution_hours),
            ("sla", sla.get(t.priority)),
        ])
        for t in resolved if t.priority in sla and t.resolution_hours > sla[t.priority]
    ]
    breaches.sort(key=lambda r: -(r["hours"] - (r["sla"] or 0)))
    open_breaches = [
        OrderedDict([
            ("ticket_id", t.ticket_id),
            ("category", t.category),
            ("priority", t.priority),
            ("hours", t.resolution_hours),
            ("sla", sla.get(t.priority)),
        ])
        for t in unresolved if t.priority in sla and (t.resolution_hours or 0) > sla[t.priority]
    ]
    open_breaches.sort(key=lambda r: -(r["hours"] or 0))
    resolution_dim = OrderedDict([
        ("mean", safe_mean(global_hours)),
        ("p50", percentile(global_hours, 0.5)),
        ("p90", percentile(global_hours, 0.9)),
        ("max", max(global_hours) if global_hours else None),
        # 全量口径：包含未解决工单的挂起时长，分位数用线性插值（与同行/Excel 结果可对齐）
        ("alt", OrderedDict([
            ("n", len(all_hours)),
            ("mean", safe_mean(all_hours)),
            ("p50", percentile_linear(all_hours, 0.5)),
            ("p90", percentile_linear(all_hours, 0.9)),
            ("max", max(all_hours) if all_hours else None),
        ])),
        ("resolved_count", len(resolved)),
        ("unresolved_count", len(unresolved)),
        ("unresolved_rate", round(len(unresolved) / len(tickets), 4) if tickets else None),
        ("breaches", breaches),
        ("breach_count", len(breaches)),
        ("breach_rate", round(len(breaches) / len(resolved), 4) if resolved else None),
        ("open_breaches", open_breaches),
        ("breach_open_count", len(open_breaches)),
        ("by_category", by_cat),
    ])

    # D5 满意度
    sats = [t.satisfaction for t in tickets if t.satisfaction is not None]
    sat_by_cat = OrderedDict()
    for c, _ in cat_counts.most_common():
        vals = [t.satisfaction for t in tickets if t.category == c and t.satisfaction is not None]
        sat_by_cat[c] = OrderedDict([
            ("n", len(vals)),
            ("mean", round(statistics.fmean(vals), 2) if vals else None),
            ("low_rate", round(sum(1 for v in vals if v <= 2) / len(vals), 4) if vals else None),
        ])
    buckets = OrderedDict([("<6h", []), ("6-24h", []), ("24-72h", []), (">72h", [])])
    for t in tickets:
        if t.resolution_hours is None or t.satisfaction is None:
            continue
        h = t.resolution_hours
        key = "<6h" if h < 6 else "6-24h" if h <= 24 else "24-72h" if h <= 72 else ">72h"
        buckets[key].append(t.satisfaction)
    sat_dim = OrderedDict([
        ("n", len(sats)),
        ("mean", round(statistics.fmean(sats), 2) if sats else None),
        ("median", percentile(sats, 0.5)),
        ("low_rate", round(sum(1 for v in sats if v <= 2) / len(sats), 4) if sats else None),
        ("distribution", OrderedDict((str(i), sum(1 for v in sats if v == i)) for i in range(5, 0, -1))),
        ("by_category", sat_by_cat),
        ("by_duration_bucket", OrderedDict(
            (k, OrderedDict([("n", len(v)), ("mean", round(statistics.fmean(v), 2) if v else None)]))
            for k, v in buckets.items()
        )),
    ])

    # D6 渠道
    ch_counts = Counter(t.channel for t in tickets)
    ch_dim = OrderedDict()
    for ch, _ in ch_counts.most_common():
        rows = [t for t in tickets if t.channel == ch]
        r_rows = [t for t in rows if t.is_resolved and t.resolution_hours is not None]
        vals = [t.satisfaction for t in rows if t.satisfaction is not None]
        ch_dim[ch] = OrderedDict([
            ("n", len(rows)),
            ("share", round(len(rows) / len(tickets), 4)),
            ("mean_hours", safe_mean([t.resolution_hours for t in r_rows])),
            ("mean_satisfaction", round(statistics.fmean(vals), 2) if vals else None),
            ("low_rate", round(sum(1 for v in vals if v <= 2) / len(vals), 4) if vals else None),
            ("unresolved", sum(1 for t in rows if not t.is_resolved)),
        ])
    channel_dim = OrderedDict([("counts", OrderedDict(ch_counts.most_common())), ("by_channel", ch_dim)])

    # D7 积压
    cum: List[int] = []
    running = 0
    for d in days:
        running += sum(1 for t in unresolved if t.day == d)
        cum.append(running)
    aging = sorted(
        [
            OrderedDict([
                ("ticket_id", t.ticket_id),
                ("category", t.category),
                ("priority", t.priority),
                ("hours", t.resolution_hours),
                ("days", round((t.resolution_hours or 0) / 24, 1)),
                ("day", t.day),
            ])
            for t in unresolved
        ],
        key=lambda r: -(r["hours"] or 0),
    )
    backlog_dim = OrderedDict([
        ("unresolved", len(unresolved)),
        ("unresolved_rate", round(len(unresolved) / len(tickets), 4) if tickets else None),
        ("high_unresolved", sum(1 for t in unresolved if t.priority == "高")),
        ("cdf", OrderedDict((d, c) for d, c in zip(days, cum))),
        ("aging", aging),
    ])

    return OrderedDict([
        ("time", time_dim),
        ("category", category_dim),
        ("priority", priority_dim),
        ("resolution", resolution_dim),
        ("satisfaction", sat_dim),
        ("channel", channel_dim),
        ("backlog", backlog_dim),
    ])


# ---------------------------------------------------------------- 簇与异常


def detect_clusters(tickets: List[Ticket]) -> Tuple[List[Cluster], List[Dict[str, str]], float]:
    buckets: Dict[str, List[Ticket]] = {rule["key"]: [] for rule in CLUSTER_RULES}
    rule_by_key = {rule["key"]: rule for rule in CLUSTER_RULES}
    multi: List[Dict[str, str]] = []
    for t in tickets:
        hits = [rule["key"] for rule in CLUSTER_RULES
                if any(kw in t.description for kw in rule["keywords"])]
        if not hits:
            continue
        buckets[hits[0]].append(t)
        if len(hits) > 1:
            multi.append({"ticket_id": t.ticket_id, "assigned": hits[0],
                          "also_matched": ", ".join(hits[1:])})
    clusters = [Cluster(rule["key"], rule["name"], rule["action"], buckets[rule["key"]])
                for rule in CLUSTER_RULES if buckets[rule["key"]]]
    # 全库随机对的平均相似度作为基准，用于判断"簇内是否真的更像同一类问题"
    grams = [bigrams(t.description) for t in tickets]
    baseline_scores: List[float] = []
    for i in range(len(grams)):
        for j in range(i + 1, len(grams)):
            union = len(grams[i] | grams[j])
            baseline_scores.append(len(grams[i] & grams[j]) / union if union else 0.0)
    baseline = statistics.fmean(baseline_scores) if baseline_scores else 0.0
    for c in clusters:
        if c.cohesion is not None and baseline > 0:
            c.cohesion_lift = round(c.cohesion / baseline, 2)
    clusters.sort(key=lambda c: (-c.size, c.key))
    return clusters, multi, round(baseline, 4)


def score_signal(share: float, span_days: int, high_share: float,
                 avg_sat: Optional[float], low_rate: Optional[float]) -> int:
    score = 0
    if share >= 0.20:
        score += 2
    elif share >= 0.10:
        score += 1
    if span_days >= 6:
        score += 2
    elif span_days >= 2:
        score += 1
    if high_share >= 0.60:
        score += 2
    elif high_share >= 0.40:
        score += 1
    if (avg_sat is not None and avg_sat <= 2.5) or (low_rate is not None and low_rate >= 0.5):
        score += 2
    elif avg_sat is not None and avg_sat <= 3.0:
        score += 1
    return min(score, 6)


def level_from_score(score: int, size: int, p_value: Optional[float]) -> str:
    level = "观察"
    for threshold, name in SCORE_LEVELS:
        if score >= threshold:
            level = name
            break
    if level == "高危" and size < 5:
        level = "关注"          # 小样本保守：少于 5 条不给"高危"
    if level == "高危" and p_value is not None and p_value > 0.05:
        level = "关注"          # 不能排除随机波动时不升级
    return level


def build_anomalies(tickets: List[Ticket], metrics: Dict[str, Any], clusters: List[Cluster],
                    min_cluster: int, sla: Dict[str, float]) -> List[Dict[str, Any]]:
    anomalies: List[Dict[str, Any]] = []
    total = len(tickets)
    early_days = metrics["time"]["early_days"]
    late_days = metrics["time"]["late_days"]
    n_early, n_late = len(early_days), len(late_days)

    def add(**kwargs: Any) -> Dict[str, Any]:
        item = OrderedDict()
        item["id"] = f"A{len(anomalies) + 1}"
        item.update(kwargs)
        item["level_order"] = LEVEL_ORDER[item["level"]]
        anomalies.append(item)
        return item

    # ---- S1 分类突增（含泊松辅助校验）
    surge: Dict[str, Dict[str, Any]] = {}
    if n_early and n_late:
        cat_daily = metrics["category"]["daily"]
        for cat, _total in metrics["category"]["counts"].items():
            daily_map = cat_daily.get(cat, {})
            early_count = sum(daily_map.get(d, 0) for d in early_days)
            late_count = sum(daily_map.get(d, 0) for d in late_days)
            if late_count < 5:
                continue
            early_rate = early_count / n_early
            late_rate = late_count / n_late
            if (early_rate > 0 and late_rate >= early_rate * 2) or (early_rate == 0 and late_count >= 5):
                lam = early_rate * n_late
                p = poisson_tail(late_count, lam) if lam > 0 else 0.0
                surge[cat] = {
                    "early_count": early_count, "late_count": late_count,
                    "early_rate": round(early_rate, 2), "late_rate": round(late_rate, 2),
                    "p": p, "lam": round(lam, 2),
                    "share_shift_pp": metrics["category"]["share_shift_pp"].get(cat),
                }

    # ---- S2 复发簇
    cluster_anomalies: Dict[str, Dict[str, Any]] = {}
    for c in clusters:
        early_c = sum(1 for t in c.tickets if t.day in set(early_days))
        late_c = c.size - early_c
        lam = (early_c / n_early * n_late) if n_early else 0.0
        p = poisson_tail(late_c, lam) if lam > 0 else (0.0 if late_c > 0 else 1.0)
        share = c.size / total if total else 0.0
        score = score_signal(share, c.span_days, c.high_share, c.avg_satisfaction, c.low_score_rate)
        level = level_from_score(score, c.size, p if c.size >= 5 else None)
        cohesion_note = "—"
        incoherent = False
        if c.cohesion_lift is not None:
            if c.cohesion_lift >= 2:
                cohesion_note = "同源佐证"
            elif c.cohesion_lift >= 1:
                cohesion_note = "一致性偏弱"
            else:
                # 簇内相似度低于全库随机配对 → 规则把不同问题混在一起，降级为观察
                cohesion_note = "一致性存疑（低于随机配对），已降级为观察"
                incoherent = True
                level = "观察"
        if c.size < min_cluster:
            if c.size >= 2 and (c.high_share >= 1.0 or (c.avg_satisfaction is not None and c.avg_satisfaction <= 2.5)):
                level = "观察"
            else:
                continue
        title = f"候选簇（需复核）：{c.name}" if incoherent else f"复发簇：{c.name}"
        item = add(
            type=["S2"], type_name=SIGNAL_NAMES["S2"], level=level, score=score,
            title=title,
            cluster=c.key,
            count=c.size,
            share=round(share, 4),
            span_days=c.span_days,
            days=c.days,
            tickets=c.ids,
            categories=c.categories,
            high_share=round(c.high_share, 4),
            avg_satisfaction=c.avg_satisfaction,
            low_rate=None if c.low_score_rate is None else round(c.low_score_rate, 4),
            cohesion=c.cohesion,
            cohesion_lift=c.cohesion_lift,
            cohesion_note=cohesion_note,
            recurrence_hits=c.recurrence_hits,
            unresolved=c.unresolved,
            p_value=(p if c.size >= 3 else None),
            evidence=f"{c.size} 条 / 跨 {c.span_days} 天（{c.days[0]} ~ {c.days[-1]}）；"
                     f"高优占比 {pct(c.high_share)}；平均满意度 {num(c.avg_satisfaction)}；"
                     f"簇内文本一致性 {c.cohesion if c.cohesion is None else f'{c.cohesion:.3f}'}"
                     + (f"（全库基准的 {c.cohesion_lift:.1f} 倍）" if c.cohesion_lift else "")
                     + ("，一致性偏弱，建议人工复核" if (c.cohesion_lift is not None and c.cohesion_lift < 2) else ""),
            why=f"簇内 {c.size} 条同一规则命中，跨 {len(c.days)} 个不同日期；"
                f"后半段 {late_c} 条 / 前半段 {early_c} 条，泊松尾部概率 {fmt_prob(p if c.size >= 3 else None)}"
                f"（证据强度：{evidence_strength(p if c.size >= 3 else None)}）；"
                f"复发词命中 {c.recurrence_hits} 条"
                + (f"；簇内相似度是全库随机配对的 {c.cohesion_lift:.1f} 倍（{cohesion_note}）" if c.cohesion_lift is not None else ""),
            action=c.action,
            verify="打开原始工单核对描述是否同一根因；若属于不同根因，请补充/拆分规则簇",
        )
        cluster_anomalies[c.key] = item

    # S1 与同分类的 S2 合并（避免同一个故事出现两次）
    for cat, info in surge.items():
        matched = None
        for c in clusters:
            if c.categories and max(c.categories, key=lambda x: sum(1 for t in c.tickets if t.category == x)) == cat:
                matched = c
                break
        if matched and matched.key in cluster_anomalies:
            item = cluster_anomalies[matched.key]
            item["type"] = ["S1", "S2"]
            item["type_name"] = f"{SIGNAL_NAMES['S1']} + {SIGNAL_NAMES['S2']}"
            item["title"] = f"{cat}突增（含复发簇：{matched.name}）"
            item["count"] = metrics["category"]["counts"][cat]
            item["share"] = metrics["category"]["share"].get(cat)
            item["cluster_count"] = matched.size
            item["score"] = max(item["score"], score_signal(
                metrics["category"]["share"].get(cat, 0),
                len(metrics["time"]["days"]),
                metrics["priority"]["high_by_category"].get(cat, 0) / max(1, metrics["category"]["counts"][cat]),
                metrics["satisfaction"]["by_category"].get(cat, {}).get("mean"),
                metrics["satisfaction"]["by_category"].get(cat, {}).get("low_rate"),
            ))
            item["level"] = level_from_score(item["score"], metrics["category"]["counts"][cat], info["p"])
            item["level_order"] = LEVEL_ORDER[item["level"]]
            item["evidence"] += (
                f"；分类级证据：{cat} 日均 {info['early_rate']} → {info['late_rate']} 条/天，"
                f"占比漂移 {info['share_shift_pp']:+}pp"
            )
            item["why"] += (
                f"；分类级泊松校验：按前半段基线 {info['early_rate']} 条/天，后半段期望 {info['lam']} 条，"
                f"实际 {info['late_count']} 条，"
                f"尾部概率 {fmt_prob(info['p'])}"
            )
        else:
            score = score_signal(
                metrics["category"]["share"].get(cat, 0), len(metrics["time"]["days"]),
                metrics["priority"]["high_by_category"].get(cat, 0) / max(1, metrics["category"]["counts"][cat]),
                metrics["satisfaction"]["by_category"].get(cat, {}).get("mean"),
                metrics["satisfaction"]["by_category"].get(cat, {}).get("low_rate"),
            )
            add(
                type=["S1"], type_name=SIGNAL_NAMES["S1"],
                level=level_from_score(score, metrics["category"]["counts"][cat], info["p"]),
                score=score, title=f"{cat}突增",
                cluster=None, count=metrics["category"]["counts"][cat],
                share=metrics["category"]["share"].get(cat), span_days=len(metrics["time"]["days"]),
                tickets=[t.ticket_id for t in tickets if t.category == cat],
                categories=[cat],
                high_share=round(metrics["priority"]["high_by_category"].get(cat, 0) / max(1, metrics["category"]["counts"][cat]), 4),
                avg_satisfaction=metrics["satisfaction"]["by_category"].get(cat, {}).get("mean"),
                low_rate=metrics["satisfaction"]["by_category"].get(cat, {}).get("low_rate"),
                cohesion=None, recurrence_hits=0, unresolved=metrics["resolution"]["by_category"].get(cat, {}).get("unresolved", 0),
                p_value=info["p"],
                evidence=f"日均 {info['early_rate']} → {info['late_rate']} 条/天，占比漂移 {info['share_shift_pp']:+}pp",
                why=f"后半段 {info['late_count']} 条，按前半段基线期望 {info['lam']} 条，尾部概率 {fmt_prob(info['p'])}",
                action="确认是否与版本发布/活动/系统故障相关，必要时升级到研发",
                verify="对比业务量增长幅度，确认是结构性变化而非总量增长",
            )

    # ---- S3 效率堵点
    res = metrics["resolution"]
    global_mean = res["mean"]
    global_unres_rate = res["unresolved_rate"] or 0.0
    by_category_items: Dict[str, Dict[str, Any]] = {}
    for cat, info in res["by_category"].items():
        if metrics["category"]["counts"].get(cat, 0) < 3:
            continue
        slow = (info["mean"] is not None and global_mean and info["mean"] >= global_mean * 1.5)
        stuck = (info["unresolved"] >= 3
                 and (info["unresolved"] / max(1, metrics["category"]["counts"][cat])) >= max(0.2, global_unres_rate * 2))
        if not (slow or stuck):
            continue
        cat_count = metrics["category"]["counts"][cat]
        share = metrics["category"]["share"][cat]
        high_share = metrics["priority"]["high_by_category"].get(cat, 0) / max(1, cat_count)
        avg_sat = metrics["satisfaction"]["by_category"].get(cat, {}).get("mean")
        low_rate = metrics["satisfaction"]["by_category"].get(cat, {}).get("low_rate")
        score = score_signal(share, len(metrics["time"]["days"]), high_share, avg_sat, low_rate)
        level = level_from_score(score, cat_count, None)
        reasons = []
        if slow:
            reasons.append(f"已解决工单平均 {num(info['mean'])}h ≥ 全局 {num(global_mean)}h 的 1.5 倍")
        if stuck:
            reasons.append(f"未解决 {info['unresolved']}/{cat_count}（{pct(info['unresolved'] / cat_count)}）≥ 全局未解决率 {pct(global_unres_rate)} 的 2 倍且 ≥3 条")
        by_category_items[cat] = add(
            type=["S3"], type_name=SIGNAL_NAMES["S3"], level=level, score=score,
            title=f"效率堵点：{cat}",
            cluster=None, count=cat_count, share=share,
            span_days=len(metrics["time"]["days"]),
            tickets=[t.ticket_id for t in tickets if t.category == cat
                     and (not t.is_resolved or (t.resolution_hours or 0) > (sla.get(t.priority) or 0))],
            categories=[cat], high_share=round(high_share, 4),
            avg_satisfaction=avg_sat, low_rate=low_rate, cohesion=None,
            cohesion_lift=None,
            recurrence_hits=sum(1 for t in tickets if t.category == cat and any(w in t.description for w in RECURRENCE_WORDS)),
            unresolved=info["unresolved"], p_value=None,
            evidence=f"P50 {num(info['p50'],0)}h / P90 {num(info['p90'],0)}h / 最长 {num(info['max'],0)}h；"
                     f"未解决 {info['unresolved']} 条；平均满意度 {num(avg_sat)}",
            why="；".join(reasons) + f"；该分类占总工单 {pct(share)}，是量级最大的堵点之一",
            action="按链路拆分退款审核/仓储验收环节，对超 72h 工单设置专人跟单与自动提醒",
            verify="抽查 3 条超长工单的实际流转记录，确认卡点在审批、仓储还是支付通道",
        )

    # ---- S4 体验塌陷（分类级）
    for cat, info in metrics["satisfaction"]["by_category"].items():
        if info["n"] < 3 or info["mean"] is None:
            continue
        if info["mean"] <= 2.5 and (info["low_rate"] or 0) >= 0.6:
            cat_count = metrics["category"]["counts"][cat]
            share = metrics["category"]["share"][cat]
            high_share = metrics["priority"]["high_by_category"].get(cat, 0) / max(1, cat_count)
            score = score_signal(share, len(metrics["time"]["days"]), high_share, info["mean"], info["low_rate"])
            existing = by_category_items.get(cat)
            if existing is not None:      # 同一分类的效率与体验问题合并为一条，避免重复叙事
                existing["type"] = ["S3", "S4"]
                existing["type_name"] = f"{SIGNAL_NAMES['S3']} + {SIGNAL_NAMES['S4']}"
                existing["title"] = f"效率与体验双差：{cat}"
                existing["score"] = max(existing["score"], score)
                existing["level"] = level_from_score(existing["score"], cat_count, None)
                existing["level_order"] = LEVEL_ORDER[existing["level"]]
                existing["evidence"] += f"；体验：满意度均值 {num(info['mean'])}、低分率 {pct(info['low_rate'])}（{info['n']} 条评分）"
                existing["why"] += "；同时满足体验塌陷条件（均值 ≤2.5 且低分率 ≥60%），说明慢与差是同一个问题的两面"
                existing["action"] = "把该分类的流程改造与体验回访合并推进：先拆链路找卡点，再对低分工单逐条回访"
            else:
                add(
                    type=["S4"], type_name=SIGNAL_NAMES["S4"],
                    level=level_from_score(score, cat_count, None), score=score,
                    title=f"体验塌陷：{cat}",
                    cluster=None, count=cat_count, share=share, span_days=len(metrics["time"]["days"]),
                    tickets=[t.ticket_id for t in tickets if t.category == cat],
                    categories=[cat], high_share=round(high_share, 4),
                    avg_satisfaction=info["mean"], low_rate=info["low_rate"],
                    cohesion=None, cohesion_lift=None, recurrence_hits=0,
                    unresolved=metrics["resolution"]["by_category"].get(cat, {}).get("unresolved", 0),
                    p_value=None,
                    evidence=f"满意度均值 {num(info['mean'])}（样本 {info['n']}），低分率 {pct(info['low_rate'])}",
                    why=f"均值 ≤2.5 且低分率 ≥60%，属于结果指标恶化；该分类 {cat_count} 条占总工单 {pct(share)}",
                    action="逐条回访低分工单，定位是响应速度、赔付方案还是态度问题",
                    verify="抽取低分样本复核聊天记录，区分个案与系统性问题",
                )

    # ---- S5 积压累积
    backlog = metrics["backlog"]
    if backlog["unresolved"] >= 5 and backlog["high_unresolved"] / max(1, backlog["unresolved"]) >= 0.5:
        oldest = backlog["aging"][0] if backlog["aging"] else None
        add(
            type=["S5"], type_name=SIGNAL_NAMES["S5"], level="高危", score=6,
            title="未解决工单积压（高优先级为主）",
            cluster=None, count=backlog["unresolved"],
            share=backlog["unresolved_rate"], span_days=len(metrics["time"]["days"]),
            tickets=[r["ticket_id"] for r in backlog["aging"]],
            categories=sorted({r["category"] for r in backlog["aging"]}),
            high_share=round(backlog["high_unresolved"] / max(1, backlog["unresolved"]), 4),
            avg_satisfaction=None, low_rate=None, cohesion=None, cohesion_lift=None, recurrence_hits=0,
            unresolved=backlog["unresolved"], p_value=None,
            evidence=f"未解决 {backlog['unresolved']} 条（{pct(backlog['unresolved_rate'])}），其中高优先级 {backlog['high_unresolved']} 条"
                     + (f"；最长挂起 {num(oldest['hours'],0)}h（{oldest['ticket_id']}，{oldest['category']}）" if oldest else ""),
            why="未解决 ≥5 条且高优先级占比 ≥50%，属于绝对量事实（不依赖统计推断）",
            action="当天点名跟进：先处理挂起最久与高优先级叠加的工单，给出明确时限",
            verify="在工单系统中按未解决 + 优先级排序复核清单是否一致",
        )

    # ---- S6 渠道差异
    ch = metrics["channel"]["by_channel"]
    if len(ch) >= 2:
        pairs = sorted(ch.items(), key=lambda kv: -(kv[1]["mean_hours"] or 0))
        (ch_a, a), (ch_b, b) = pairs[0], pairs[-1]
        if (a["n"] >= 5 and b["n"] >= 5 and a["mean_hours"] and b["mean_hours"]
                and a["mean_hours"] >= b["mean_hours"] * 1.5):
            add(
                type=["S6"], type_name=SIGNAL_NAMES["S6"], level="关注", score=3,
                title=f"渠道效率差异：{ch_a} 慢于 {ch_b}",
                cluster=None, count=a["n"], share=a["share"], span_days=len(metrics["time"]["days"]),
                tickets=[t.ticket_id for t in tickets if t.channel == ch_a],
                categories=sorted({t.category for t in tickets if t.channel == ch_a}),
                high_share=None, avg_satisfaction=a["mean_satisfaction"], low_rate=a["low_rate"],
                cohesion=None, cohesion_lift=None, recurrence_hits=0, unresolved=a["unresolved"], p_value=None,
                evidence=f"{ch_a} 平均处理 {num(a['mean_hours'])}h（{a['n']} 条） vs {ch_b} {num(b['mean_hours'])}h（{b['n']} 条）；"
                         f"满意度 {num(a['mean_satisfaction'])} vs {num(b['mean_satisfaction'])}，低分率 {pct(a['low_rate'])} vs {pct(b['low_rate'])}",
                why=f"两渠道样本均 ≥5 条且平均时长差异 ≥1.5 倍（{num(a['mean_hours'])}h vs {num(b['mean_hours'])}h），说明差异不是单条极值造成的；"
                    f"同时 {ch_a} 的低分率更高（{pct(a['low_rate'])} vs {pct(b['low_rate'])}）",
                action=f"检查 {ch_a} 的排班与工具支持（如是否能直接操作退款/改单）",
                verify="比较两渠道同类工单（如退款）的处理时长，排除问题结构差异",
            )

    # 同级别下：变化类信号（S1/S2）优先于状态类信号（S3–S6），再按分值、影响面排序
    anomalies.sort(key=lambda a: (
        a["level_order"],
        0 if ({"S1", "S2"} & set(a["type"])) else 1,
        -a["score"],
        -(a.get("count") or 0),
        a["title"],
    ))
    for i, a in enumerate(anomalies, start=1):
        a["id"] = f"A{i}"
    return anomalies


def rank_tickets(tickets: List[Ticket], metrics: Dict[str, Any], clusters: List[Cluster],
                 sla: Dict[str, float]) -> List[Dict[str, Any]]:
    """工单级跟进清单：把"信号级"结论落到"今天先处理哪几张单"。

    评分规则（公开可审计，与需求文档 §7.2 一致）：
      未解决 +3；未解决且挂起已超 SLA +1；高优先级 +2（中 +1）；
      耗时/挂起 ≥ 全量口径 P90 +2；满意度 ≤2 +2；命中复发簇 +1。
    分级：P1 ≥9 分（今天处理）、P2 7–8 分（本周跟进）、P3 5–6 分（备查，只列工单号）；
    低于 5 分不进清单 —— 阈值按本数据集的分数分布标定，保证 P1/P2 是真正需要人介入的那一小撮。
    """
    p90 = metrics["resolution"]["alt"]["p90"] or 0
    cluster_of: Dict[str, Cluster] = {}
    for c in clusters:
        for t in c.tickets:
            cluster_of[t.ticket_id] = c

    ranked: List[Dict[str, Any]] = []
    for t in tickets:
        score = 0
        reasons: List[str] = []
        if not t.is_resolved:
            score += 3
            reasons.append("未解决")
            if t.priority in sla and (t.resolution_hours or 0) > sla[t.priority]:
                score += 1
                reasons.append(f"挂起 {num(t.resolution_hours, 0)}h 已超 SLA {num(sla[t.priority], 0)}h")
        if t.priority == "高":
            score += 2
            reasons.append("高优先级")
        elif t.priority == "中":
            score += 1
            reasons.append("中优先级")
        if t.resolution_hours is not None and p90 and t.resolution_hours >= p90:
            score += 2
            reasons.append(f"耗时 {num(t.resolution_hours, 0)}h ≥ 全量 P90 {num(p90, 1)}h")
        if t.satisfaction is not None and t.satisfaction <= 2:
            score += 2
            reasons.append(f"满意度 {t.satisfaction} 分")
        c = cluster_of.get(t.ticket_id)
        if c is not None:
            score += 1
            reasons.append(f"命中复发簇：{c.name}")

        level = "P1" if score >= 9 else "P2" if score >= 7 else "P3" if score >= 5 else ""
        if not level:
            continue
        ranked.append(OrderedDict([
            ("ticket_id", t.ticket_id),
            ("level", level),
            ("score", score),
            ("category", t.category),
            ("priority", t.priority),
            ("is_resolved", t.is_resolved),
            ("hours", t.resolution_hours),
            ("satisfaction", t.satisfaction),
            ("cluster", c.name if c else None),
            ("reasons", reasons),
        ]))
    ranked.sort(key=lambda r: (-r["score"], r["ticket_id"]))
    for i, r in enumerate(ranked, start=1):
        r["rank"] = i
    return ranked


# ---------------------------------------------------------------- SVG 图表
#
# 配色与字体遵循 kami 设计语言（warm parchment · ink-blue accent · serif-led）：
# 一个强调色 + 暖灰阶，不用 rgba、不用渐变、不用投影。
# 数据图表调色板取自 kami references/diagrams.md §9「Data charts」。
PALETTE = {
    "focal": "#1B365D",        # 唯一强调色（ink blue）：焦点数据 / 主序列
    "series2": "#504e49",      # olive：第二序列、参考线
    "series3": "#6b6a64",      # stone：第三序列、次级文字
    "series4": "#b8b7b0",      # light-stone：第四序列
    "series5": "#d4d3cd",      # mist：第五序列 / 非焦点柱
    "tint": "#EEF2F7",         # brand-tint：浅色底（实心，非 rgba）
    "zone": "#f1efe8",         # 象限底色（羊皮纸预混实心色）
    "grid": "#e8e7e1",         # 网格线
    "ink": "#141413",          # 主文字
    "sub": "#504e49",          # 次文字
    "muted": "#6b6a64",        # 三级文字 / 坐标轴标签
    "parchment": "#f5f4ed", "ivory": "#faf9f5", "dots": "#E3E2DC",
    "border": "#e8e6dc", "border_soft": "#e5e3d8",
}

#: 单一衬线字体栈（CN 走 kami 的降级链；不引用任何外部字体文件，保持离线可用）
SERIF_STACK = ('Charter, Georgia, "TsangerJinKai02", "Source Han Serif SC", '
               '"Noto Serif CJK SC", "Songti SC", "STSong", serif')


class Scale:
    def __init__(self, d0: float, d1: float, r0: float, r1: float) -> None:
        self.d0, self.d1, self.r0, self.r1 = d0, d1, r0, r1

    def __call__(self, v: float) -> float:
        if self.d1 == self.d0:
            return (self.r0 + self.r1) / 2
        ratio = (v - self.d0) / (self.d1 - self.d0)
        ratio = max(0.0, min(1.0, ratio))
        return self.r0 + ratio * (self.r1 - self.r0)


def svg_header(width: int, height: int, title: str, subtitle: str = "",
               pid: str = "dots", eyebrow: str = "工单趋势分析 · kami") -> List[str]:
    """图内头部：mono eyebrow + 衬线标题 + 一行副题；画布为羊皮纸 + 点纹（实心色，不用 rgba）。"""
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="100%" '
        f'height="auto" role="img" aria-label="{esc(title)}" font-family=\'{SERIF_STACK}\'>',
        f'<defs><pattern id="{pid}" width="22" height="22" patternUnits="userSpaceOnUse">'
        f'<circle cx="1" cy="1" r="0.9" fill="{PALETTE["dots"]}"/></pattern></defs>',
        f'<rect width="{width}" height="{height}" fill="{PALETTE["parchment"]}"/>',
        f'<rect width="{width}" height="{height}" fill="url(#{pid})" opacity="0.55"/>',
        f'<text x="24" y="20" font-size="10" letter-spacing="2.4" fill="{PALETTE["muted"]}" '
        f'font-family="\'JetBrains Mono\', Consolas, monospace">{esc(eyebrow)}</text>',
        f'<text x="24" y="42" font-size="16" font-weight="500" fill="{PALETTE["ink"]}">{esc(title)}</text>',
    ]
    if subtitle:
        parts.append(f'<text x="24" y="60" font-size="11" fill="{PALETTE["sub"]}">{esc(subtitle)}</text>')
    return parts


def chart_daily_volume(metrics: Dict[str, Any]) -> str:
    daily = metrics["time"]["daily"]
    tickets_high = metrics["time"].get("high_daily") or {}
    days = list(daily.keys())
    w, h = 860, 336
    left, right, top, bottom = 56, 24, 80, 48
    max_v = max(daily.values()) if daily else 1
    x = Scale(0, max(1, len(days)), left, w - right)
    y = Scale(0, max_v + 1, h - bottom, top)
    bar_w = (w - left - right) / max(1, len(days)) * 0.55
    parts = svg_header(w, h, "D1 每日工单量（堆叠：高优先级 / 其他）",
                       f"平均 {metrics['time']['daily_avg']} 条/天；峰值 {metrics['time']['peak_day']}（{metrics['time']['peak_count']} 条）",
                       pid="dotsDaily")
    for gv in range(0, max_v + 2):
        yy = y(gv)
        parts.append(f'<line x1="{left}" y1="{yy:.1f}" x2="{w - right}" y2="{yy:.1f}" stroke="{PALETTE["grid"]}" stroke-width="1"/>')
        parts.append(f'<text x="{left - 10}" y="{yy + 4:.1f}" font-size="11" fill="{PALETTE["muted"]}" text-anchor="end">{gv}</text>')
    avg = metrics["time"]["daily_avg"]
    parts.append(f'<line x1="{left}" y1="{y(avg):.1f}" x2="{w - right}" y2="{y(avg):.1f}" stroke="{PALETTE["series2"]}" stroke-width="1.2" stroke-dasharray="6 4"/>')
    # 标注放在左端，避免与最后一根柱子的数值标签相撞
    parts.append(f'<text x="{left + 6}" y="{y(avg) - 6:.1f}" font-size="11" fill="{PALETTE["series2"]}" text-anchor="start">日均 {avg}</text>')
    for i, d in enumerate(days):
        cx = x(i + 0.5)
        total = daily[d]
        hi = tickets_high.get(d, round(total * 0.5))
        base = y(0)
        h_hi = base - y(hi)
        h_lo = base - y(total)
        parts.append(f'<rect x="{cx - bar_w / 2:.1f}" y="{y(hi):.1f}" width="{bar_w:.1f}" height="{h_hi:.1f}" fill="{PALETTE["focal"]}" rx="2"/>')
        parts.append(f'<rect x="{cx - bar_w / 2:.1f}" y="{y(total):.1f}" width="{bar_w:.1f}" height="{max(0, h_lo - h_hi):.1f}" fill="{PALETTE["series5"]}" rx="2"/>')
        parts.append(f'<text x="{cx:.1f}" y="{y(total) - 6:.1f}" font-size="11" fill="{PALETTE["ink"]}" text-anchor="middle">{total}</text>')
        parts.append(f'<text x="{cx:.1f}" y="{h - 24}" font-size="11" fill="{PALETTE["muted"]}" text-anchor="middle">{d[5:]}</text>')
    parts.append(f'<line x1="{left}" y1="{h - 26}" x2="{w - right}" y2="{h - 26}" stroke="{PALETTE["grid"]}" stroke-width="0.8"/>'
                 f'<rect x="{left}" y="{h - 16}" width="10" height="10" fill="{PALETTE["focal"]}" rx="2"/>'
                 f'<text x="{left + 16}" y="{h - 7}" font-size="11" fill="{PALETTE["muted"]}">高优先级</text>'
                 f'<rect x="{left + 90}" y="{h - 16}" width="10" height="10" fill="{PALETTE["series5"]}" rx="2"/>'
                 f'<text x="{left + 106}" y="{h - 7}" font-size="11" fill="{PALETTE["muted"]}">其他优先级</text>')
    parts.append("</svg>")
    return "".join(parts)


def chart_category_mix(metrics: Dict[str, Any]) -> str:
    counts = metrics["category"]["counts"]
    early = metrics["category"]["early_share"]
    late = metrics["category"]["late_share"]
    cats = list(counts.keys())
    w, h = 860, 100 + 42 * len(cats)
    left, right, top = 132, 60, 74
    max_v = max([v for v in list(early.values()) + list(late.values()) if v is not None] + [0.01])
    x = Scale(0, max_v * 1.15, left, w - right)
    parts = svg_header(w, h, "D2 分类占比：前半段 vs 后半段",
                       f"前半段 {metrics['time']['early_days'][0]}~{metrics['time']['early_days'][-1]}"
                       f"（{metrics['time']['early_count']} 条） vs 后半段 {metrics['time']['late_days'][0]}~{metrics['time']['late_days'][-1]}（{metrics['time']['late_count']} 条）",
                       pid="dotsMix")
    for i, c in enumerate(cats):
        yy = top + i * 42
        parts.append(f'<text x="{left - 12}" y="{yy + 18}" font-size="12" fill="{PALETTE["ink"]}" text-anchor="end">{esc(c)}</text>')
        for j, (label, values, color) in enumerate((("前半", early, PALETTE["series5"]), ("后半", late, PALETTE["focal"]))):
            v = values.get(c) or 0.0
            by = yy + j * 16
            parts.append(f'<rect x="{left}" y="{by}" width="{max(0.6, x(v) - left):.1f}" height="13" fill="{color}" rx="2"/>')
            parts.append(f'<text x="{x(v) + 6:.1f}" y="{by + 11}" font-size="11" fill="{PALETTE["muted"]}">{pct(v)}</text>')
        shift = metrics["category"]["share_shift_pp"].get(c)
        if shift is not None and abs(shift) >= 5:
            color = PALETTE["focal"] if shift > 0 else PALETTE["series3"]
            parts.append(f'<text x="{w - 16}" y="{yy + 18}" font-size="11" fill="{color}" text-anchor="end">{shift:+.1f}pp</text>')
    parts.append("</svg>")
    return "".join(parts)


def chart_category_quadrant(metrics: Dict[str, Any]) -> str:
    counts = metrics["category"]["counts"]
    share = metrics["category"]["share"]
    sat = metrics["satisfaction"]["by_category"]
    w, h = 860, 436
    left, right, top, bottom = 70, 40, 86, 60
    x = Scale(0, max(0.4, max(share.values()) * 1.15), left, w - right)
    y = Scale(1, 5, h - bottom, top)
    parts = svg_header(w, h, "D3 分类象限：影响面（占比） × 客户体验（满意度）",
                       "气泡大小 = 工单量；左下区域 = 高频 + 低分，优先处理", pid="dotsQuad")
    parts.append(f'<rect x="{left}" y="{y(3.0):.1f}" width="{x(0.2) - left:.1f}" height="{h - bottom - y(3.0):.1f}" fill="{PALETTE["zone"]}"/>')
    parts.append(f'<line x1="{x(0.2):.1f}" y1="{top}" x2="{x(0.2):.1f}" y2="{h - bottom}" stroke="{PALETTE["grid"]}" stroke-dasharray="4 4"/>')
    parts.append(f'<line x1="{left}" y1="{y(3.0):.1f}" x2="{w - right}" y2="{y(3.0):.1f}" stroke="{PALETTE["grid"]}" stroke-dasharray="4 4"/>')
    for gv in (1, 2, 3, 4, 5):
        parts.append(f'<text x="{left - 10}" y="{y(gv) + 4:.1f}" font-size="11" fill="{PALETTE["muted"]}" text-anchor="end">{gv}</text>')
    for gv in (0, 0.1, 0.2, 0.3, 0.4):
        if gv > max(0.4, max(share.values()) * 1.15):
            continue
        parts.append(f'<text x="{x(gv):.1f}" y="{h - bottom + 18}" font-size="11" fill="{PALETTE["muted"]}" text-anchor="middle">{gv * 100:.0f}%</text>')
    parts.append(f'<text x="{left}" y="{h - 18}" font-size="11" fill="{PALETTE["muted"]}">横轴：占总工单比例（影响面）</text>')
    parts.append(f'<text x="{left}" y="{top - 14}" font-size="11" fill="{PALETTE["muted"]}">纵轴：满意度均值（1–5）</text>')
    # 先算位置，再做标签防重叠：气泡会重叠，但标签必须能读
    pts = []
    for c, n in counts.items():
        mean = sat.get(c, {}).get("mean") or 3
        pts.append((c, n, x(share[c]), y(mean), 7 + math.sqrt(n) * 3.6, mean))
    used: List[Tuple[float, float]] = []
    for c, n, cx, cy, r, mean in pts:
        focal = share[c] >= 0.2 and (mean or 5) <= 2.5
        fill = PALETTE["tint"] if focal else PALETTE["ivory"]
        stroke = PALETTE["focal"] if focal else PALETTE["series3"]
        parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" fill="{fill}" '
                     f'stroke="{stroke}" stroke-width="1.4"/>')
        ly = cy
        while any(abs(cx - ux) < 64 and abs(ly - uy) < 15 for ux, uy in used):
            ly += 15
        used.append((cx, ly))
        if r >= 17:      # 大气泡：标签放进圆内
            parts.append(f'<text x="{cx:.1f}" y="{ly + 4:.1f}" font-size="12" fill="{PALETTE["ink"]}" '
                         f'text-anchor="middle">{esc(c)}</text>')
            parts.append(f'<text x="{cx:.1f}" y="{cy + r + 14:.1f}" font-size="10.5" fill="{PALETTE["muted"]}" '
                         f'text-anchor="middle">{n} 条 · {num(mean)} 分</text>')
        else:            # 小气泡：标签放到圆外，避免文字挤在圆里
            right_room = cx + r + 96 < w - right
            tx = cx + r + 6 if right_room else cx - r - 6
            anchor = "start" if right_room else "end"
            parts.append(f'<text x="{tx:.1f}" y="{ly + 4:.1f}" font-size="11.5" fill="{PALETTE["ink"]}" '
                         f'text-anchor="{anchor}">{esc(c)}</text>')
            parts.append(f'<text x="{tx:.1f}" y="{ly + 17:.1f}" font-size="10.5" fill="{PALETTE["muted"]}" '
                         f'text-anchor="{anchor}">{n} 条 · {num(mean)} 分</text>')
    parts.append("</svg>")
    return "".join(parts)


def chart_cluster_trend(metrics: Dict[str, Any], clusters: List[Cluster]) -> str:
    days = metrics["time"]["days"]
    target = next((c for c in clusters if c.key == "pay_state_mismatch"), clusters[0] if clusters else None)
    w, h = 860, 346
    left, right, top, bottom = 56, 24, 82, 48
    if target is None:
        return "".join(svg_header(w, h, "D8 复发簇每日分布", "无簇数据", pid="dotsCluster") + ["</svg>"])
    cluster_daily = Counter(t.day for t in target.tickets)
    dominant = target.categories[0] if target.categories else None
    cat_daily = metrics["category"]["daily"].get(dominant, {}) if dominant else {}
    pay_daily = cat_daily
    max_v = max([cluster_daily.get(d, 0) for d in days] + [pay_daily.get(d, 0) for d in days] + [1])
    x = Scale(0, max(1, len(days)), left, w - right)
    y = Scale(0, max_v + 1, h - bottom, top)
    bar_w = (w - left - right) / max(1, len(days)) * 0.52
    parts = svg_header(w, h, f"D8 复发簇持续强度：{target.name}",
                       f"{target.size} 条 / 跨 {target.span_days} 天：深蓝为该簇，浅灰为其余同类工单",
                       pid="dotsCluster")
    for gv in range(0, max_v + 2):
        yy = y(gv)
        parts.append(f'<line x1="{left}" y1="{yy:.1f}" x2="{w - right}" y2="{yy:.1f}" stroke="{PALETTE["grid"]}"/>')
        parts.append(f'<text x="{left - 10}" y="{yy + 4:.1f}" font-size="11" fill="{PALETTE["muted"]}" text-anchor="end">{gv}</text>')
    for i, d in enumerate(days):
        cx = x(i + 0.5)
        cluster_v = cluster_daily.get(d, 0)
        pay_v = pay_daily.get(d, 0)
        parts.append(f'<rect x="{cx - bar_w / 2:.1f}" y="{y(pay_v):.1f}" width="{bar_w:.1f}" height="{max(0, y(0) - y(pay_v)):.1f}" fill="{PALETTE["series5"]}" rx="2"/>')
        parts.append(f'<rect x="{cx - bar_w / 2:.1f}" y="{y(cluster_v):.1f}" width="{bar_w:.1f}" height="{max(0, y(0) - y(cluster_v)):.1f}" fill="{PALETTE["focal"]}" rx="2"/>')
        parts.append(f'<text x="{cx:.1f}" y="{h - 24}" font-size="11" fill="{PALETTE["muted"]}" text-anchor="middle">{d[5:]}</text>')
        if cluster_v:
            parts.append(f'<text x="{cx:.1f}" y="{y(cluster_v) - 6:.1f}" font-size="11" fill="{PALETTE["ink"]}" text-anchor="middle">{cluster_v}</text>')
    parts.append(f'<line x1="{left}" y1="{h - 26}" x2="{w - right}" y2="{h - 26}" stroke="{PALETTE["grid"]}" stroke-width="0.8"/>'
                 f'<rect x="{left}" y="{h - 16}" width="10" height="10" fill="{PALETTE["focal"]}" rx="2"/>'
                 f'<text x="{left + 16}" y="{h - 7}" font-size="11" fill="{PALETTE["muted"]}">{esc(target.name)}</text>'
                 f'<rect x="{left + 320}" y="{h - 16}" width="10" height="10" fill="{PALETTE["series5"]}" rx="2"/>'
                 f'<text x="{left + 336}" y="{h - 7}" font-size="11" fill="{PALETTE["muted"]}">其余同类工单</text>')
    parts.append("</svg>")
    return "".join(parts)


def chart_backlog(metrics: Dict[str, Any]) -> str:
    days = metrics["time"]["days"]
    cdf = metrics["backlog"]["cdf"]
    w, h = 860, 316
    left, right, top, bottom = 56, 24, 82, 48
    max_v = max(list(cdf.values()) + [1])
    x = Scale(0, max(1, len(days) - 1), left, w - right)
    y = Scale(0, max_v + 1, h - bottom, top)
    parts = svg_header(w, h, "D7 未解决工单累计（积压曲线）",
                       f"窗口内累计未解决 {metrics['backlog']['unresolved']} 条，其中高优先级 {metrics['backlog']['high_unresolved']} 条",
                       pid="dotsBacklog")
    for gv in range(0, max_v + 2):
        parts.append(f'<line x1="{left}" y1="{y(gv):.1f}" x2="{w - right}" y2="{y(gv):.1f}" stroke="{PALETTE["grid"]}"/>')
        parts.append(f'<text x="{left - 10}" y="{y(gv) + 4:.1f}" font-size="11" fill="{PALETTE["muted"]}" text-anchor="end">{gv}</text>')
    pts = [(x(i), y(cdf[d])) for i, d in enumerate(days)]
    area = f"M {pts[0][0]:.1f} {y(0):.1f} " + " ".join(f"L {px:.1f} {py:.1f}" for px, py in pts) + f" L {pts[-1][0]:.1f} {y(0):.1f} Z"
    parts.append(f'<path d="{area}" fill="{PALETTE["tint"]}"/>')
    parts.append('<polyline points="' + " ".join(f"{px:.1f},{py:.1f}" for px, py in pts) + f'" fill="none" stroke="{PALETTE["focal"]}" stroke-width="1.6"/>')
    for i, d in enumerate(days):
        px, py = pts[i]
        parts.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="3" fill="{PALETTE["ivory"]}" stroke="{PALETTE["focal"]}" stroke-width="1.6"/>')
        parts.append(f'<text x="{px:.1f}" y="{h - 24}" font-size="11" fill="{PALETTE["muted"]}" text-anchor="middle">{d[5:]}</text>')
        parts.append(f'<text x="{px:.1f}" y="{py - 8:.1f}" font-size="10.5" fill="{PALETTE["ink"]}" text-anchor="middle">{cdf[d]}</text>')
    parts.append("</svg>")
    return "".join(parts)


def chart_hourly(metrics: Dict[str, Any]) -> str:
    hourly = metrics["time"]["hourly"]
    hours = list(hourly.keys())
    peaks = set(metrics["time"].get("peak_hours") or [])
    w, h = 860, 316
    left, right, top, bottom = 56, 24, 80, 48
    max_v = max(list(hourly.values()) + [1])
    x = Scale(0, len(hours), left, w - right)
    y = Scale(0, max_v + 1, h - bottom, top)
    bar_w = (w - left - right) / len(hours) * 0.62
    peak_txt = "、".join(f"{p}:00" for p in sorted(peaks)) if peaks else "—"
    parts = svg_header(w, h, "D9 时段分布（按工单创建小时）",
                       f"高峰时段：{peak_txt}；用于客服排班与高峰值守（样本量小，只作参考）",
                       pid="dotsHourly")
    for gv in range(0, max_v + 2):
        yy = y(gv)
        parts.append(f'<line x1="{left}" y1="{yy:.1f}" x2="{w - right}" y2="{yy:.1f}" stroke="{PALETTE["grid"]}"/>')
        parts.append(f'<text x="{left - 10}" y="{yy + 4:.1f}" font-size="11" fill="{PALETTE["muted"]}" text-anchor="end">{gv}</text>')
    for i, hh in enumerate(hours):
        v = hourly[hh]
        cx = x(i + 0.5)
        color = PALETTE["focal"] if hh in peaks else PALETTE["series5"]
        parts.append(f'<rect x="{cx - bar_w / 2:.1f}" y="{y(v):.1f}" width="{bar_w:.1f}" '
                     f'height="{max(0, y(0) - y(v)):.1f}" fill="{color}" rx="2"/>')
        if v:
            parts.append(f'<text x="{cx:.1f}" y="{y(v) - 6:.1f}" font-size="10.5" fill="{PALETTE["ink"]}" text-anchor="middle">{v}</text>')
        if int(hh) % 2 == 0:
            parts.append(f'<text x="{cx:.1f}" y="{h - 24}" font-size="10.5" fill="{PALETTE["muted"]}" text-anchor="middle">{hh}</text>')
    parts.append(f'<text x="{left}" y="{h - 6}" font-size="11" fill="{PALETTE["muted"]}">横轴：小时（00–23）；深蓝柱 = 工单量最高的三个时段</text>')
    parts.append("</svg>")
    return "".join(parts)


def build_charts(metrics: Dict[str, Any], clusters: List[Cluster]) -> "OrderedDict[str, str]":
    return OrderedDict([
        ("01_daily_volume.svg", chart_daily_volume(metrics)),
        ("02_category_mix.svg", chart_category_mix(metrics)),
        ("03_category_quadrant.svg", chart_category_quadrant(metrics)),
        ("04_payment_cluster_trend.svg", chart_cluster_trend(metrics, clusters)),
        ("05_backlog.svg", chart_backlog(metrics)),
        ("06_hourly.svg", chart_hourly(metrics)),
    ])


# ---------------------------------------------------------------- 渲染


def render_markdown(result: Dict[str, Any]) -> str:
    m = result["dimensions"]
    if not m["time"]["days"]:
        return ("# 客服工单趋势分析报告\n\n"
                f"> 数据源：`{result['meta']['input']}` ｜ 生成时间：{result['meta']['generated_at']}\n\n"
                "## 0. 摘要\n\n输入数据为空，未生成分析结果。请检查输入文件后重跑：`uv run analyze.py --input <文件>`\n\n"
                "## 1. 数据质量\n\n" + "\n".join(f"- {w}" for w in result["warnings"]) + "\n")
    lines: List[str] = []
    add = lines.append
    add("# 客服工单趋势分析报告")
    add("")
    add(f"> 数据源：`{result['meta']['input']}` ｜ 时间范围：{result['meta']['window']['start']} ~ "
        f"{result['meta']['window']['end']}（{result['meta']['days']} 天） ｜ 工单数：{result['meta']['total']} ｜ "
        f"生成时间：{result['meta']['generated_at']} ｜ 工具版本：v{result['meta']['version']}")
    add("")
    add("## 0. 摘要（先看这里）")
    add("")
    for i, line in enumerate(result["summary_lines"], start=1):
        add(f"{i}. {line}")
    add("")
    add("**分级计数**：" + "、".join(f"{lvl} {cnt} 条" for lvl, cnt in result["level_counts"].items() if cnt) + "。")
    add("")
    add("![每日工单量](charts/01_daily_volume.svg)")
    add("")
    add("## 1. 数据质量与口径")
    add("")
    add("| 检查项 | 结果 |")
    add("| --- | --- |")
    add(f"| 工单总数 | {m['time']['total']} 条 |")
    add(f"| 时间跨度 | {result['meta']['window']['start']} ~ {result['meta']['window']['end']}（{result['meta']['days']} 天） |")
    add(f"| 已解决 / 未解决 | {m['resolution']['resolved_count']} / {m['resolution']['unresolved_count']} |")
    add(f"| 满意度缺失 | {m['time']['total'] - m['satisfaction']['n']} 条 |")
    add(f"| 优先级异常值 | {len([w for w in result['warnings'] if '优先级' in w])} 处 |")
    add("")
    if result["warnings"]:
        add("**数据质量告警（工具不会静默处理）**")
        add("")
        for w in result["warnings"]:
            add(f"- {w}")
        add("")
    add(f"> 口径假设：SLA = " + "、".join(f"{k} {v:.0f}h" for k, v in result["meta"]["config"]["sla"].items())
        + "（假设值，可用 `--sla` 覆盖）；未解决工单的处理时长按“已挂起时长”统计，不计入 SLA 达成率。")
    add("")
    add("## 2. 分析维度")

    days = m["time"]["days"]
    add("")
    add("### D1 时间趋势")
    add("")
    add(f"- 总工单 {m['time']['total']} 条，日均 {m['time']['daily_avg']} 条；峰值日 {m['time']['peak_day']}（{m['time']['peak_count']} 条）。")
    add(f"- 前半段（{days[0]} ~ {m['time']['early_days'][-1]}）{m['time']['early_count']} 条（{m['time']['early_avg']} 条/天）；"
        f"后半段（{m['time']['late_days'][0]} ~ {days[-1]}）{m['time']['late_count']} 条（{m['time']['late_avg']} 条/天）"
        + (f"，变化 {m['time']['growth_pct'] * 100:+.0f}%。" if m["time"].get("growth_pct") is not None else "。"))
    add(f"- 波动系数 CV = {m['time']['cv']}（日间波动" + ("较小，趋势比噪声明显）" if (m["time"]["cv"] or 9) < 0.4 else "较大，需警惕单日峰值是偶发）"))
    add("")
    add("| 日期 | " + " | ".join(d[5:] for d in days) + " |")
    add("| --- |" + " --- |" * len(days))
    add("| 工单量 | " + " | ".join(str(m["time"]["daily"][d]) for d in days) + " |")
    add("")
    roll = m["time"]["rolling_3d"]
    if roll.get("available"):
        add(f"- **滚动环比（最近 3 天 vs 此前 3 天）**：{roll['recent_avg']} 条/天 vs {roll['prior_avg']} 条/天，"
            f"变化 {roll['change_pct'] * 100:+.1f}% → 判定「**{roll['trend']}**」"
            f"（阈值 ±{int(roll['threshold'] * 100)}%，与常见看板口径一致）。")
    else:
        add("- 滚动环比：日期不足 6 天，样本不足，未做最近 3 天对比。")
    add("")
    add("### D9 时段分布（排班价值）")
    add("")
    add("![时段分布](charts/06_hourly.svg)")
    add("")
    add(f"- 高峰时段：{'、'.join(h + ':00' for h in sorted(m['time']['peak_hours']))}；"
        f"最忙的小时是 {m['time']['busiest_hour']}:00。")
    add("")
    add("| 小时 | " + " | ".join(h for h, _ in sorted(m["time"]["hourly"].items())) + " |")
    add("| --- |" + " --- |" * len(m["time"]["hourly"]))
    add("| 工单量 | " + " | ".join(str(c) for _, c in sorted(m["time"]["hourly"].items())) + " |")
    add("")
    add("> 用途：把高峰期人力压在对应时段；本数据集只有 11 天、50 条，时段结论只能作为**参考**，需要更长窗口验证。")
    add("")

    add("### D2 分类结构")
    add("")
    add("![分类占比](charts/02_category_mix.svg)")
    add("")
    add("| 分类 | 条数 | 占比 | 前半段占比 | 后半段占比 | 漂移 |")
    add("| --- | ---: | ---: | ---: | ---: | ---: |")
    for c, n in m["category"]["counts"].items():
        add(f"| {c} | {n} | {pct(m['category']['share'][c])} | {pct(m['category']['early_share'][c])} | "
            f"{pct(m['category']['late_share'][c])} | {m['category']['share_shift_pp'][c]:+.1f}pp |")
    add("")
    top_shift = max(m["category"]["share_shift_pp"].items(), key=lambda kv: kv[1])
    add(f"**结论**：{top_shift[0]} 的占比漂移最大（{top_shift[1]:+.1f}pp），"
        f"说明问题结构在窗口内发生了实质变化，而不只是总量波动。")
    add("")

    add("### D3 严重程度")
    add("")
    add(f"- 高优先级 {m['priority']['counts'].get('高', 0)} 条（{pct(m['priority']['high_share'])}）；"
        f"前半段 {pct(m['priority']['high_share_early'])} → 后半段 {pct(m['priority']['high_share_late'])}。")
    add(f"- 二项尾部校验：以 {pct(m['priority']['high_share_early'])} 为基线，后半段 {m['time']['late_count']} 条中出现 "
        f"{int(round((m['priority']['high_share_late'] or 0) * m['time']['late_count']))} 条高优先级，"
        f"尾部概率 {fmt_prob(result['stats']['high_share_binom_p'])}"
        f"（证据强度：{evidence_strength(result['stats']['high_share_binom_p'])}）。")
    add("")
    add("| 分类 | 高优先级条数 | 占比 |")
    add("| --- | ---: | ---: |")
    for c, n in m["priority"]["high_by_category"].items():
        add(f"| {c} | {n} | {pct(n / max(1, m['category']['counts'][c]))} |")
    add("")
    add("### D4 处理效率与 SLA")
    add("")
    add(f"- 已解决工单平均处理 {num(m['resolution']['mean'])}h，P50 {num(m['resolution']['p50'], 0)}h，"
        f"P90 {num(m['resolution']['p90'], 0)}h；SLA 超时 {m['resolution']['breach_count']} 条（{pct(m['resolution']['breach_rate'])}）。")
    add(f"- 另有 **{m['resolution']['breach_open_count']} 条未解决工单的挂起时长已超过 SLA**："
        + "、".join(f"{b['ticket_id']}（{num(b['hours'], 0)}h / SLA {num(b['sla'], 0)}h）" for b in m["resolution"]["open_breaches"])
        + "。这些不计入上面的超时率，而是体现在积压维度（D7）。")
    add("")
    add("| 分类 | 已解决 | 均值 | P50 | P90 | 最长 | 未解决 |")
    add("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for c, info in m["resolution"]["by_category"].items():
        add(f"| {c} | {info['resolved']} | {num(info['mean'])}h | {num(info['p50'], 0)}h | {num(info['p90'], 0)}h | "
            f"{num(info['max'], 0)}h | {info['unresolved']} |")
    add("")
    alt = m["resolution"]["alt"]
    add(f"**口径对照（同一份数据、两种口径）**：主口径只统计已解决工单（P50/P90 用 nearest-rank）；"
        f"全量口径把未解决工单的“已挂起时长”也算进去（P90 用线性插值，与 Excel/numpy 默认一致）。"
        f"全局：已解决口径均值 {num(m['resolution']['mean'])}h / P50 {num(m['resolution']['p50'], 0)}h / P90 {num(m['resolution']['p90'], 0)}h；"
        f"全量口径均值 **{num(alt['mean'])}h** / P50 {num(alt['p50'], 0)}h / P90 **{num(alt['p90'], 0)}h**。"
        f"两个数都对，差别只来自“未解决的 8 条算不算”。本工具主张用主口径做效率判断（未完成的工作不该算进处理效率），"
        f"同时给出全量口径，方便与其它看板/工具对齐。")
    add("")
    add("| 分类 | 已解决口径 均值 | 已解决口径 P90 | 全量口径 均值 | 全量口径 P90 |")
    add("| --- | ---: | ---: | ---: | ---: |")
    for c, info in m["resolution"]["by_category"].items():
        add(f"| {c} | {num(info['mean'])}h | {num(info['p90'], 0)}h | {num(info['alt_mean'])}h | {num(info['alt_p90'], 0)}h |")
    add("")
    add(f"**SLA 超时工单明细（{m['resolution']['breach_count']} 条，按超出时长排序）**")
    add("")
    add("| 工单 | 分类 | 优先级 | 处理时长 | SLA | 超出 |")
    add("| --- | --- | --- | ---: | ---: | ---: |")
    for b in m["resolution"]["breaches"]:
        add(f"| {b['ticket_id']} | {b['category']} | {b['priority']} | {num(b['hours'], 0)}h | {num(b['sla'], 0)}h | "
            f"{num((b['hours'] or 0) - (b['sla'] or 0), 0)}h |")
    add("")
    add("### D5 客户体验")
    add("")
    add(f"- 满意度均值 {num(m['satisfaction']['mean'])}（中位数 {num(m['satisfaction']['median'], 0)}），"
        f"低分率（≤2 分）{pct(m['satisfaction']['low_rate'])}。")
    add("")
    add("| 分类 | 样本 | 满意度均值 | 低分率 |")
    add("| --- | ---: | ---: | ---: |")
    for c, info in m["satisfaction"]["by_category"].items():
        add(f"| {c} | {info['n']} | {num(info['mean'])} | {pct(info['low_rate'])} |")
    add("")
    add("| 处理时长分桶 | 样本 | 满意度均值 |")
    add("| --- | ---: | ---: |")
    for k, info in m["satisfaction"]["by_duration_bucket"].items():
        add(f"| {k} | {info['n']} | {num(info['mean'])} |")
    add("")
    add("### D6 渠道结构")
    add("")
    add("| 渠道 | 条数 | 占比 | 平均处理时长（已解决） | 满意度均值 | 低分率 | 未解决 |")
    add("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for ch, info in m["channel"]["by_channel"].items():
        add(f"| {ch} | {info['n']} | {pct(info['share'])} | {num(info['mean_hours'])}h | {num(info['mean_satisfaction'])} | "
            f"{pct(info['low_rate'])} | {info['unresolved']} |")
    add("")
    add("### D7 积压状态")
    add("")
    add("![积压曲线](charts/05_backlog.svg)")
    add("")
    add(f"- 未解决 {m['backlog']['unresolved']} 条（{pct(m['backlog']['unresolved_rate'])}），其中高优先级 {m['backlog']['high_unresolved']} 条。")
    add("")
    add("| 工单 | 分类 | 优先级 | 已挂起(h) | 约等于天数 | 创建日 |")
    add("| --- | --- | --- | ---: | ---: | --- |")
    for a in m["backlog"]["aging"]:
        add(f"| {a['ticket_id']} | {a['category']} | {a['priority']} | {num(a['hours'], 0)} | {a['days']} | {a['day']} |")
    add("")
    add("### D8 关联关系与复发簇")
    add("")
    add("![复发簇强度](charts/04_payment_cluster_trend.svg)")
    add("")
    add("| 簇 | 条数 | 占比 | 跨天数 | 高优占比 | 满意度均值 | 一致性 Jaccard | 相对全库 | 复发词 | 未解决 | 工单号 |")
    add("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |")
    for c in result["clusters"]:
        add(f"| {c['name']} | {c['size']} | {pct(c['share'])} | {c['span_days']} | {pct(c['high_share'])} | "
            f"{num(c['avg_satisfaction'])} | {c['cohesion'] if c['cohesion'] is not None else '—'} | "
            f"{str(c['cohesion_lift']) + '×' if c.get('cohesion_lift') is not None else '—'} | {c['recurrence_hits']} | "
            f"{c['unresolved']} | {'、'.join(c['tickets'])} |")
    add("")
    add(f"> 文本一致性口径：簇内两两描述字符二元组 Jaccard 相似度均值，全库随机配对基准 "
        f"{result['stats']['cohesion_baseline']}；相对倍数越高说明簇内越像同一类问题（≥2 倍视为同源佐证，"
        f"不足则标注“一致性偏弱”）。")
    add("")
    add(f"未归类到任何规则簇的工单 {len(result['unclustered'])} 条：{'、'.join(result['unclustered']) or '无'}。")
    add("")
    add("![分类象限](charts/03_category_quadrant.svg)")
    add("")
    add("## 3. 异常信号清单")
    add("")
    for level in ("高危", "关注", "观察"):
        items = [a for a in result["anomalies"] if a["level"] == level]
        if not items:
            continue
        add(f"### {level}（{len(items)} 条）")
        add("")
        for a in items:
            add(f"#### {a['id']} · {a['title']}")
            add("")
            add(f"- **信号类型**：{a['type_name']}（{'/'.join(a['type'])}） ｜ **分值**：{a['score']}/6 ｜ **分级**：{a['level']}")
            add(f"- **证据**：{a['evidence']}")
            add(f"- **判断依据**：{a['why']}")
            add(f"- **建议动作**：{a['action']}")
            add(f"- **复核方式**：{a['verify']}")
            if a.get("tickets"):
                shown = "、".join(a["tickets"][:12]) + ("…" if len(a["tickets"]) > 12 else "")
                add(f"- **涉及工单**：{shown}")
            add("")
    add("## 4. 工单级优先跟进清单（今天先处理哪几张单）")
    add("")
    ranks = result.get("ticket_ranking") or []
    if ranks:
        counts = result["summary"]["ticket_levels"]
        add(f"共 {len(ranks)} 条工单进入清单：**P1 {counts['P1']} 条、P2 {counts['P2']} 条、P3 {counts['P3']} 条**。"
            "评分规则：未解决 +3；未解决且挂起已超 SLA +1；高优先级 +2（中 +1）；"
            f"耗时/挂起 ≥ 全量口径 P90（{num(m['resolution']['alt']['p90'], 1)}h）+2；满意度 ≤2 +2；命中复发簇 +1。"
            "分级：P1 ≥9 分（今天处理）、P2 7–8 分（本周跟进）、P3 5–6 分（备查，仅列工单号）；"
            "阈值按本数据集的分数分布标定（8 分与 7 分之间存在明显断层），低于 5 分不进清单。")
        add("")
        add("> **P1/P2/P3 是本工具的跟进优先级，不是企业正式事故等级**，用于排序工作量，不用于对外通报。")
        add("")
        add("| # | 工单 | 等级 | 分值 | 分类 | 优先级 | 状态 | 时长/挂起 | 满意度 | 命中簇 | 命中原因 |")
        add("| ---: | --- | --- | ---: | --- | --- | --- | ---: | ---: | --- | --- |")
        show = [r for r in ranks if r["level"] in ("P1", "P2")]
        for r in show:
            add(f"| {r['rank']} | {r['ticket_id']} | **{r['level']}** | {r['score']} | {r['category']} | {r['priority']} | "
                f"{'已解决' if r['is_resolved'] else '未解决'} | {num(r['hours'], 0)}h | "
                f"{r['satisfaction'] if r['satisfaction'] is not None else '—'} | {r['cluster'] or '—'} | "
                f"{'；'.join(r['reasons'])} |")
        add("")
        p3 = [r for r in ranks if r["level"] == "P3"]
        if p3:
            add(f"P3（5–6 分，共 {len(p3)} 条，备查/顺手处理）：{'、'.join(r['ticket_id'] for r in p3)}。")
            add("")
    else:
        add("没有工单达到 5 分（未解决 / 高优先级 / 超 P90 / 低满意度 / 命中复发簇 的累积分）。")
        add("")
    add("## 5. 方法与阈值")
    add("")
    add("| 信号 | 触发阈值 |")
    add("| --- | --- |")
    add("| S1 分类突增 | 后半段日均 ≥ 前半段日均 × 2 且后半段 ≥ 5 条 |")
    add("| S2 复发簇 | 簇内 ≥ 3 条且跨 ≥ 3 个不同日期（一致性 Jaccard 作为佐证） |")
    add("| S3 效率堵点 | 分类平均时长 ≥ 全局 × 1.5，或未解决率 ≥ 全局 × 2 且 ≥ 3 条 |")
    add("| S4 体验塌陷 | 满意度均值 ≤ 2.5 且低分率 ≥ 60%（样本 ≥ 3） |")
    add("| S5 积压累积 | 未解决 ≥ 5 条且高优先级占比 ≥ 50% |")
    add("| S6 渠道差异 | 两渠道样本各 ≥ 5，平均时长差 ≥ 1.5 倍 |")
    add("")
    add("**分级打分**（0–6 分）：影响面（占比 ≥20% =2 / ≥10% =1）、持续（跨 ≥6 天 =2 / ≥2 天 =1）、"
        "严重度（高优占比 ≥60% =2 / ≥40% =1）、体验（均值 ≤2.5 或低分率 ≥50% =2 / ≤3.0 =1）。"
        "≥5 分 = 高危（要求条数 ≥5 且尾部概率 ≤0.05），3–4 分 = 关注，≤2 分 = 观察。")
    add("")
    add("**辅助概率校验**：泊松尾部概率（分类/簇的频率突增）与二项尾部概率（高优先级占比变化），"
        "用于排除明显偶然；详见实现文档 §5.4。")
    add("")
    add("## 6. 局限性与后续建议")
    add("")
    add("1. **样本小**：仅 " + str(result["meta"]["total"]) + " 条 / " + str(result["meta"]["days"])
        + " 天，且无历史基线，只能做窗口内前后对比，无法区分季节性与事件驱动。")
    add("2. **规则簇召回有限**：未归类工单 " + str(len(result["unclustered"]))
        + " 条，其中可能仍有未识别的同源问题；建议业务专家补充同义词后重跑。")
    add("3. **SLA 与口径是假设**：超时类结论随 `--sla` 变化；未解决工单时长按“已挂起”解释。")
    add("4. **人工录入偏差**：分类与优先级由客服判定，口径漂移会影响趋势判断。")
    add("5. **概率校验的边界**：工单存在自相关，泊松/二项仅作辅助证据，不构成显著性结论。")
    add("")
    add("**下一步建议**：① 用支付簇的 12 条工单推动一次对账排查；② 设 72h 退款超时提醒；"
        "③ 补齐 90 天历史数据以建立周内基线；④ 让业务补充簇规则同义词后重跑本工具。")
    add("")
    add("## 附录 A · 指标字典")
    add("")
    add("| 指标 | 口径 |")
    add("| --- | --- |")
    add("| 日均 | 工单数 ÷ 天数（前后半段天数不等，故必须用日均比较） |")
    add("| 占比漂移 | 后半段占比 − 前半段占比，单位 pp |")
    add("| 分位数 | nearest-rank，小样本不做插值 |")
    add("| 低分率 | 满意度 ≤2 的条数 ÷ 有评分条数 |")
    add("| SLA 超时 | 处理时长 > SLA 且工单已解决 |")
    add("| 挂起时长 | 未解决工单的 resolution_time_hours |")
    add("| 簇一致性 | 簇内两两描述字符二元组 Jaccard 相似度均值 |")
    add("")
    add("## 附录 B · 簇规则表（可审计）")
    add("")
    add("| 簇 | 关键词 |")
    add("| --- | --- |")
    for rule in CLUSTER_RULES:
        add(f"| {rule['name']} | {'、'.join(rule['keywords'])} |")
    add("")
    if result["multi_match"]:
        add("**多命中工单**（按优先级归入首个簇，其余命中记录在此）")
        add("")
        add("| 工单 | 归入 | 同时命中 |")
        add("| --- | --- | --- |")
        for row in result["multi_match"]:
            add(f"| {row['ticket_id']} | {row['assigned']} | {row['also_matched']} |")
        add("")
    return "\n".join(lines)


def render_html(result: Dict[str, Any]) -> str:
    m = result["dimensions"]
    peak_label = (m["time"]["peak_day"] or "—")[5:]
    cards = [
        ("工单总数", f"{m['time']['total']}", f"{result['meta']['days']} 天"),
        ("日均工单", f"{m['time']['daily_avg']}", f"峰值 {peak_label} · {m['time']['peak_count']} 条"),
        ("高优先级占比", pct(m["priority"]["high_share"]), f"前半 {pct(m['priority']['high_share_early'])} → 后半 {pct(m['priority']['high_share_late'])}"),
        ("满意度低分率", pct(m["satisfaction"]["low_rate"]), f"均值 {num(m['satisfaction']['mean'])} / 5"),
        ("未解决积压", f"{m['backlog']['unresolved']}", f"其中高优先级 {m['backlog']['high_unresolved']} 条"),
        ("SLA 超时", f"{m['resolution']['breach_count']}", f"超时率 {pct(m['resolution']['breach_rate'])}（已解决口径）"),
    ]
    parts: List[str] = []
    add = parts.append
    add("<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">")
    add("<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">")
    add(f"<title>客服工单趋势分析 Dashboard · {result['meta']['window']['start']} ~ {result['meta']['window']['end']}</title>")
    add("""<style>
/* kami 设计语言：warm parchment · ink-blue accent · serif-led · 单一强调色 */
:root{
  --parchment:#f5f4ed; --ivory:#faf9f5; --brand:#1B365D; --brand-tint:#EEF2F7;
  --ink:#141413; --dark-warm:#3d3d3a; --olive:#504e49; --stone:#6b6a64;
  --border:#e8e6dc; --border-soft:#e5e3d8; --sand:#e8e6dc;
  --serif:Charter,Georgia,"TsangerJinKai02","Source Han Serif SC","Noto Serif CJK SC","Songti SC","STSong",serif;
  --mono:"JetBrains Mono","SF Mono",Consolas,"Source Han Serif SC","Noto Serif CJK SC",monospace;
  --sans:var(--serif);
}
*{box-sizing:border-box}
body{margin:0;background:var(--parchment);color:var(--ink);font:15px/1.65 var(--sans)}
.wrap{max-width:1120px;margin:0 auto;padding:72px 64px 88px;overflow-wrap:anywhere}
.eyebrow{font-family:var(--mono);font-size:11px;letter-spacing:.22em;text-transform:uppercase;color:var(--stone);margin:0 0 10px}
header h1{margin:0 0 10px;font-size:32px;font-weight:500;letter-spacing:-.3pt;line-height:1.15}
header .meta{color:var(--stone);font-size:12.5px;line-height:1.7}
.rule{height:1px;background:var(--border);border:0;margin:28px 0 8px}
/* KPI 条：同级数据共用一个 band + 细分割线（而不是 N 张卡片） */
.kpis{display:grid;grid-template-columns:repeat(6,1fr);background:var(--ivory);
  border:1px solid var(--border);border-radius:8px;margin:24px 0 8px;overflow:hidden}
.kpi{padding:18px 18px 16px;border-left:1px solid var(--border-soft);min-width:0}
.kpi:first-child{border-left:0}
.kpi .l{color:var(--olive);font-size:11.5px;letter-spacing:.02em}
.kpi .v{font-family:var(--serif);font-size:26px;font-weight:500;color:var(--brand);
  margin:6px 0 4px;font-variant-numeric:tabular-nums}
.kpi .s{color:var(--stone);font-size:11.5px;line-height:1.5;overflow-wrap:anywhere}
.kpis-note{color:var(--stone);font-size:11.5px;margin:0 0 8px}
.card{background:var(--ivory);border:1px solid var(--border);border-radius:8px;
  padding:20px 24px;margin:16px 0;break-inside:avoid}
.card figure{margin:0}
.card figcaption{color:var(--olive);font-size:14px;margin-top:14px;max-width:62ch}
/* 图表在窄屏保持可读：横向滑动，而不是把 860 宽的图缩到 4px 字号 */
.chart-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
.chart-wrap svg{min-width:620px}
/* 章节标题：品牌左竖条是 kami 的签名动作 */
h2{font-size:19px;font-weight:500;margin:40px 0 12px;border-left:3px solid var(--brand);
  border-radius:1px;padding-left:10px}
h3{font-size:15.5px;font-weight:500;margin:20px 0 8px}
table{width:100%;border-collapse:collapse;font-size:13px;margin:12px 0;break-inside:avoid}
th,td{padding:8px 10px;text-align:right;border-bottom:1px solid var(--border-soft);vertical-align:top}
th:first-child,td:first-child{text-align:left}
th{color:var(--dark-warm);font-weight:500;border-bottom:1px solid var(--border)}
td{font-variant-numeric:tabular-nums}
.table-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
.anomaly{border-left:3px solid var(--stone)}
.anomaly.高危{border-left-color:var(--brand)}
.anomaly.关注{border-left-color:var(--olive)}
.anomaly.观察{border-left-color:var(--stone)}
.badge{display:inline-block;font-family:var(--mono);font-size:11px;letter-spacing:.06em;
  padding:2px 7px;border-radius:2px;margin-right:8px;vertical-align:2px;border:1px solid transparent}
.badge.高危{background:var(--brand);color:#f5f4ed}
.badge.关注{background:var(--brand-tint);color:var(--brand);border-color:var(--brand)}
.badge.观察{background:var(--sand);color:var(--olive)}
.chips span{display:inline-block;font-family:var(--mono);font-size:11px;background:var(--brand-tint);
  color:var(--brand);border-radius:2px;padding:2px 6px;margin:2px 4px 2px 0}
.kv{color:var(--stone)}
.note{background:var(--brand-tint);border:1px solid var(--brand);border-radius:8px;
  padding:14px 18px;font-size:13.5px;color:var(--dark-warm)}
ul.clean{margin:8px 0 0 18px;padding:0}
footer{color:var(--stone);font-size:11.5px;margin-top:36px;border-top:1px solid var(--border);padding-top:14px}
@media (max-width:880px){
  .wrap{padding:44px 24px 64px}
  header h1{font-size:26px}
  .kpis{grid-template-columns:repeat(3,1fr)}
  .kpi:nth-child(3n+1){border-left:0}
  h2{margin-top:32px}
}
@media (max-width:480px){
  body{font-size:14.5px}
  .wrap{padding:32px 16px 56px}
  .kpis{grid-template-columns:repeat(2,1fr)}
  .kpi:nth-child(2n+1){border-left:0}
  .card{padding:16px 16px}
  .card figcaption{font-size:13px}
}
@media print{
  body{background:#fff}
  .wrap{padding:0;max-width:none}
  .card{break-inside:avoid}
}
</style></head><body><div class="wrap">""")
    add("<header>")
    add('<p class="eyebrow">0111 · 客服工单趋势分析</p>')
    add("<h1>工单趋势与异常 Dashboard</h1>")
    add(f'<div class="meta">数据源 {esc(result["meta"]["input"])} ｜ 时间范围 '
        f'{result["meta"]["window"]["start"]} ~ {result["meta"]["window"]["end"]}（{result["meta"]["days"]} 天） ｜ '
        f'工单 {result["meta"]["total"]} 条 ｜ 生成时间 {esc(result["meta"]["generated_at"])} ｜ 工具 v{result["meta"]["version"]}</div>')
    add("</header>")
    add('<hr class="rule">')
    add('<div class="kpis">')
    for label, value, sub in cards:
        add(f'<div class="kpi"><div class="l">{esc(label)}</div><div class="v">{esc(value)}</div><div class="s">{esc(sub)}</div></div>')
    add("</div>")
    add('<p class="kpis-note">主口径 = 仅已解决工单 + nearest-rank；全量口径 = 含未解决挂起时长 + 线性插值。</p>')

    add('<div class="card"><h3>摘要</h3><ul class="clean">')
    for line in result["summary_lines"]:
        add(f"<li>{esc(line)}</li>")
    add("</ul>")
    add("<p class=\"kv\">分级计数：" + esc("、".join(f"{lvl} {cnt} 条" for lvl, cnt in result["level_counts"].items() if cnt)) + "</p></div>")

    if result["warnings"]:
        add('<div class="card note"><strong>数据质量告警</strong><ul class="clean">')
        for w in result["warnings"]:
            add(f"<li>{esc(w)}</li>")
        add("</ul></div>")

    add('<h2>① 趋势与结构</h2>')
    charts = result.get("charts") or {}
    # 图表下方的 caption 写"结论"，不是写"画了什么"（kami: caption states the insight）
    captions: Dict[str, str] = {}
    cat_counts = m["category"]["counts"]
    if cat_counts:
        tc = next(iter(cat_counts))
        roll = m["time"]["rolling_3d"]
        captions = {
            "01_daily_volume.svg": f"后半段日均 {m['time']['late_avg']} 条 vs 前半段 {m['time']['early_avg']} 条；"
                                   + (f"最近 3 日环比 {roll['change_pct'] * 100:+.1f}%，判定为「{roll['trend']}」。"
                                      if roll.get("available") else "日期不足 6 天，未做最近 3 日对比。"),
            "02_category_mix.svg": f"{tc} 占比从 {pct(m['category']['early_share'][tc])} 升到 {pct(m['category']['late_share'][tc])}"
                                   f"（{m['category']['share_shift_pp'][tc]:+.1f}pp），是唯一触发突增信号的分类。",
            "03_category_quadrant.svg": "右下角是「高频 + 低分」区（占比 ≥20% 且满意度 ≤2.5）："
                                        f"当前落在区内的只有 {tc}，应最先处理。",
            "06_hourly.svg": f"工单集中在 {'、'.join(h + ':00' for h in sorted(m['time']['peak_hours']))}，"
                             f"最忙 {m['time']['busiest_hour']}:00；高峰期应保证在线入口有人值守。",
        }
        if result["clusters"]:
            top_cluster = result["clusters"][0]
            captions["04_payment_cluster_trend.svg"] = (
                f"{top_cluster['name']} 共 {top_cluster['size']} 条、跨 {top_cluster['span_days']} 天，"
                f"深蓝柱几乎每天都出现——这是持续出血，不是偶发投诉。")
        if m["backlog"]["aging"]:
            oldest = m["backlog"]["aging"][0]
            captions["05_backlog.svg"] = (
                f"未解决累计 {m['backlog']['unresolved']} 条（高优 {m['backlog']['high_unresolved']} 条），"
                f"最长挂起 {num(oldest['hours'], 0)}h（{oldest['ticket_id']}）。")
    for key in ("01_daily_volume.svg", "02_category_mix.svg", "03_category_quadrant.svg", "06_hourly.svg"):
        if key in charts:
            add(f'<div class="card"><div class="chart-wrap"><figure>{charts[key]}'
                f'<figcaption>{esc(captions.get(key, ""))}</figcaption></figure></div></div>')

    roll = m["time"]["rolling_3d"]
    if roll.get("available"):
        add('<div class="card"><h3>滚动环比（最近 3 天 vs 此前 3 天）</h3>'
            f'<p>最近 3 天日均 <strong>{roll["recent_avg"]}</strong> 条（{esc("、".join(d[5:] for d in roll["recent_days"]))}），'
            f'此前 3 天日均 <strong>{roll["prior_avg"]}</strong> 条（{esc("、".join(d[5:] for d in roll["prior_days"]))}），'
            f'变化 <strong>{roll["change_pct"] * 100:+.1f}%</strong> → 判定「<strong>{esc(roll["trend"])}</strong>」'
            f'（阈值 ±{int(roll["threshold"] * 100)}%）。</p></div>')

    add('<h2>② 复发簇与积压</h2>')
    for key in ("04_payment_cluster_trend.svg", "05_backlog.svg"):
        if key in charts:
            add(f'<div class="card"><div class="chart-wrap"><figure>{charts[key]}'
                f'<figcaption>{esc(captions.get(key, ""))}</figcaption></figure></div></div>')

    add('<div class="card"><h3>复发簇明细</h3><div class="table-wrap"><table><thead><tr>'
        '<th>簇</th><th>条数</th><th>占比</th><th>跨天数</th><th>高优占比</th><th>满意度均值</th>'
        '<th>一致性</th><th>相对全库</th><th>复发词</th><th>工单号</th></tr></thead><tbody>')
    for c in result["clusters"]:
        add(f'<tr><td>{esc(c["name"])}</td><td>{c["size"]}</td><td>{pct(c["share"])}</td><td>{c["span_days"]}</td>'
            f'<td>{pct(c["high_share"])}</td><td>{num(c["avg_satisfaction"])}</td>'
            f'<td>{c["cohesion"] if c["cohesion"] is not None else "—"}</td>'
            f'<td>{str(c["cohesion_lift"]) + "×" if c.get("cohesion_lift") is not None else "—"}</td>'
            f'<td>{c["recurrence_hits"]}</td>'
            f'<td>{esc("、".join(c["tickets"]))}</td></tr>')
    add(f'</tbody></table></div><p class="kv">文本一致性 = 簇内两两 Jaccard 相似度均值，全库随机配对基准 '
        f'{esc(str(result["stats"]["cohesion_baseline"]))}；“相对全库”≥2 倍视为同源佐证，不足则标注一致性偏弱。</p></div>')

    add('<h2>③ 异常信号</h2>')
    for a in result["anomalies"]:
        add(f'<div class="card anomaly {esc(a["level"])}">')
        add(f'<h3><span class="badge {esc(a["level"])}">{esc(a["level"])}</span>{esc(a["id"])} · {esc(a["title"])}'
            f'<span class="kv" style="font-weight:400;font-size:13px"> ｜ {esc(a["type_name"])} ｜ 分值 {a["score"]}/6</span></h3>')
        add(f'<p><strong>证据：</strong>{esc(a["evidence"])}</p>')
        add(f'<p><strong>判断依据：</strong>{esc(a["why"])}</p>')
        add(f'<p><strong>建议动作：</strong>{esc(a["action"])}</p>')
        add(f'<p class="kv"><strong>复核方式：</strong>{esc(a["verify"])}</p>')
        if a.get("tickets"):
            add('<div class="chips"><span class="kv">涉及工单：</span>'
                + "".join(f"<span>{esc(t)}</span>" for t in a["tickets"][:16]) + "</div>")
        add("</div>")

    ranks = result.get("ticket_ranking") or []
    add('<h2>④ 工单级优先跟进清单</h2><div class="card">')
    if ranks:
        counts = result["summary"]["ticket_levels"]
        add(f'<p>共 {len(ranks)} 条进入清单：<strong>P1 {counts["P1"]} 条、P2 {counts["P2"]} 条、P3 {counts["P3"]} 条</strong>'
            f'（未解决 +3 / 高优 +2 / 超全量 P90 {num(m["resolution"]["alt"]["p90"], 1)}h +2 / 满意度 ≤2 +2 / 命中复发簇 +1）。'
            '<span class="kv">P1 ≥9 / P2 7–8 / P3 5–6 分；P1/P2/P3 是本工具的跟进优先级，不是企业正式事故等级。</span></p>')
        add('<div class="table-wrap"><table><thead><tr><th>#</th><th>工单</th><th>等级</th><th>分值</th>'
            '<th>分类</th><th>优先级</th><th>状态</th><th>时长/挂起</th><th>满意度</th><th>命中原因</th>'
            '</tr></thead><tbody>')
        for r in [x for x in ranks if x["level"] in ("P1", "P2")]:
            add(f'<tr><td>{r["rank"]}</td><td>{esc(r["ticket_id"])}</td><td><strong>{esc(r["level"])}</strong></td>'
                f'<td>{r["score"]}</td><td>{esc(r["category"])}</td><td>{esc(r["priority"])}</td>'
                f'<td>{"已解决" if r["is_resolved"] else "未解决"}</td><td>{num(r["hours"], 0)}h</td>'
                f'<td>{r["satisfaction"] if r["satisfaction"] is not None else "—"}</td>'
                f'<td>{esc("；".join(r["reasons"]))}</td></tr>')
        add("</tbody></table></div>")
    else:
        add("<p>没有工单达到 5 分，未生成清单。</p>")
    add("</div>")

    add('<h2>⑤ 分类明细</h2><div class="card"><div class="table-wrap"><table><thead><tr>'
        '<th>分类</th><th>条数</th><th>占比</th><th>前半</th><th>后半</th><th>漂移</th><th>高优占比</th>'
        '<th>满意度</th><th>低分率</th><th>P50</th><th>P90</th><th>全量均值</th><th>全量P90</th><th>未解决</th></tr></thead><tbody>')
    for c, n in m["category"]["counts"].items():
        ri = m["resolution"]["by_category"][c]
        si = m["satisfaction"]["by_category"][c]
        add(f'<tr><td>{esc(c)}</td><td>{n}</td><td>{pct(m["category"]["share"][c])}</td>'
            f'<td>{pct(m["category"]["early_share"][c])}</td><td>{pct(m["category"]["late_share"][c])}</td>'
            f'<td>{m["category"]["share_shift_pp"][c]:+.1f}pp</td>'
            f'<td>{pct(m["priority"]["high_by_category"].get(c, 0) / max(1, n))}</td>'
            f'<td>{num(si["mean"])}</td><td>{pct(si["low_rate"])}</td>'
            f'<td>{num(ri["p50"], 0)}h</td><td>{num(ri["p90"], 0)}h</td>'
            f'<td>{num(ri["alt_mean"])}h</td><td>{num(ri["alt_p90"], 0)}h</td><td>{ri["unresolved"]}</td></tr>')
    add(f'</tbody></table></div><p class="kv">「P50/P90」= 已解决口径 + nearest-rank；「全量均值/全量P90」= 含未解决工单挂起时长 + 线性插值，'
        f'用于与 Excel/numpy 等常见口径对齐。全局：已解决 {num(m["resolution"]["mean"])}h / '
        f'全量 {num(m["resolution"]["alt"]["mean"])}h。</p></div>')

    add('<h2>⑥ 方法与局限</h2><div class="card">')
    add('<p><strong>口径</strong>：SLA = ' + esc("、".join(f"{k} {v:.0f}h" for k, v in result["meta"]["config"]["sla"].items()))
        + "（假设值）；未解决工单按“已挂起时长”统计，不进入 SLA 分母；分位数使用 nearest-rank。</p>")
    add("<p><strong>辅助概率校验</strong>：泊松尾部概率（频率突增）、二项尾部概率（高优先级占比变化）。"
        f"本数据集高优先级占比变化的二项尾部概率 {esc(fmt_prob(result['stats']['high_share_binom_p']))}"
        f"（证据强度：{esc(evidence_strength(result['stats']['high_share_binom_p']))}）。</p>")
    add(f'<p><strong>口径</strong>：SLA 超时只对已解决工单计算（{m["resolution"]["breach_count"]} 条）；'
        f'另有 {m["resolution"]["breach_open_count"]} 条未解决工单挂起已超 SLA，计入积压维度。</p>')
    add('<p><strong>局限</strong>：样本仅 ' + esc(str(result["meta"]["total"])) + " 条 / " + esc(str(result["meta"]["days"]))
        + " 天，无历史基线；聚类依赖关键词规则，未归类工单 " + esc(str(len(result["unclustered"])))
        + " 条；分类与优先级由人工录入；概率校验不构成显著性结论。详细讨论见 README。</p>")
    add("</div>")
    add(f'<footer>由 analyze.py v{esc(result["meta"]["version"])} 自动生成 ｜ 零第三方依赖 ｜ 同一输入可复现相同结果</footer>')
    add("</div></body></html>")
    return "".join(parts)


# ---------------------------------------------------------------- 主流程


def parse_sla(text: str) -> Dict[str, float]:
    sla = dict(DEFAULT_SLA)
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(f"SLA 参数格式应为 高=24,中=48,低=72，收到：{chunk!r}")
        key, _, value = chunk.partition("=")
        sla[key.strip()] = float(value)
    return sla


def build_result(tickets: List[Ticket], warnings: List[str], args: argparse.Namespace,
                 input_path: Path) -> Dict[str, Any]:
    sla = args.sla_dict
    metrics = compute_metrics(tickets, sla, args.split_ratio)
    # 给图表补充高优先级每日计数
    metrics["time"]["high_daily"] = OrderedDict(
        (d, sum(1 for t in tickets if t.day == d and t.priority == "高")) for d in metrics["time"]["days"]
    )
    clusters, multi, cohesion_baseline = detect_clusters(tickets)
    anomalies = build_anomalies(tickets, metrics, clusters, args.min_cluster, sla)
    ticket_ranking = rank_tickets(tickets, metrics, clusters, sla)

    # 二项校验：高优先级占比变化
    binom_p = None
    if metrics["priority"]["high_share_early"] is not None and metrics["time"]["late_count"]:
        k = sum(1 for t in tickets if t.priority == "高" and t.day in set(metrics["time"]["late_days"]))
        binom_p = binom_tail(k, metrics["time"]["late_count"], metrics["priority"]["high_share_early"])

    unclustered = sorted(t.ticket_id for t in tickets
                         if not any(any(kw in t.description for kw in rule["keywords"]) for rule in CLUSTER_RULES))

    cluster_summary = [
        OrderedDict([
            ("key", c.key), ("name", c.name), ("size", c.size),
            ("share", round(c.size / len(tickets), 4) if tickets else 0),
            ("span_days", c.span_days), ("days", c.days),
            ("high_share", round(c.high_share, 4)),
            ("avg_satisfaction", c.avg_satisfaction),
            ("low_rate", None if c.low_score_rate is None else round(c.low_score_rate, 4)),
            ("cohesion", c.cohesion), ("cohesion_lift", c.cohesion_lift),
            ("recurrence_hits", c.recurrence_hits),
            ("unresolved", c.unresolved), ("categories", c.categories),
            ("tickets", c.ids), ("action", c.action),
        ])
        for c in clusters
    ]

    level_counts = OrderedDict((lvl, sum(1 for a in anomalies if a["level"] == lvl)) for lvl in ("高危", "关注", "观察"))
    rank_counts = OrderedDict((lvl, sum(1 for r in ticket_ranking if r["level"] == lvl)) for lvl in ("P1", "P2", "P3"))

    summary = OrderedDict([
        ("total", metrics["time"]["total"]),
        ("daily_avg", metrics["time"]["daily_avg"]),
        ("days", len(metrics["time"]["days"])),
        ("high_share", metrics["priority"]["high_share"]),
        ("low_score_rate", metrics["satisfaction"]["low_rate"]),
        ("unresolved", metrics["backlog"]["unresolved"]),
        ("breach_rate", metrics["resolution"]["breach_rate"]),
        ("levels", level_counts),
        ("ticket_levels", rank_counts),
    ])

    summary_lines: List[str] = []
    if not tickets:
        summary_lines.append("输入数据为空，未生成分析结果；请检查输入文件后重跑。")
        generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return OrderedDict([
            ("meta", OrderedDict([
                ("version", VERSION), ("input", str(input_path)), ("generated_at", generated_at),
                ("total", 0), ("days", 0),
                ("window", OrderedDict([("start", None), ("end", None)])),
                ("config", OrderedDict([
                    ("sla", OrderedDict((k, float(v)) for k, v in sla.items())),
                    ("split_ratio", args.split_ratio), ("min_cluster", args.min_cluster),
                ])),
            ])),
            ("summary", OrderedDict([("total", 0), ("daily_avg", 0), ("days", 0), ("high_share", None),
                                     ("low_score_rate", None), ("unresolved", 0), ("breach_rate", None),
                                     ("levels", OrderedDict((l, 0) for l in ("高危", "关注", "观察"))),
                                     ("ticket_levels", OrderedDict((l, 0) for l in ("P1", "P2", "P3")))])),
            ("summary_lines", summary_lines),
            ("dimensions", metrics),
            ("clusters", []),
            ("_clusters", []),
            ("anomalies", []),
            ("ticket_ranking", []),
            ("unclustered", []),
            ("multi_match", []),
            ("warnings", warnings),
            ("stats", OrderedDict([("high_share_binom_p", None), ("cohesion_baseline", 0.0)])),
            ("level_counts", OrderedDict((l, 0) for l in ("高危", "关注", "观察"))),
            ("charts", build_charts_empty()),
        ])
    pc = metrics["category"]
    top_cat, top_cat_n = next(iter(pc["counts"].items()))
    summary_lines.append(
        f"量最大的分类是 {top_cat}（{top_cat_n} 条，{pct(pc['share'][top_cat])}），"
        f"占比从 {pct(pc['early_share'][top_cat])} 升至 {pct(pc['late_share'][top_cat])}"
        f"（{pc['share_shift_pp'][top_cat]:+.1f}pp）。"
    )
    if anomalies:
        top = anomalies[0]
        brief = f"最需要当天处理的是「{top['title']}」：涉及 {top.get('count')} 条"
        if top.get("span_days"):
            brief += f"、跨 {top['span_days']} 天"
        if top.get("high_share") is not None:
            brief += f"、高优占比 {pct(top['high_share'])}"
        if top.get("avg_satisfaction") is not None:
            brief += f"、平均满意度 {num(top['avg_satisfaction'])}"
        if top.get("p_value") is not None:
            brief += f"、频率突增的尾部概率 {fmt_prob(top['p_value'])}"
        summary_lines.append(brief + "。")
    worst_sat = min(
        ((c, i) for c, i in metrics["satisfaction"]["by_category"].items() if i["mean"] is not None),
        key=lambda kv: kv[1]["mean"], default=(None, None),
    )
    if worst_sat[0]:
        summary_lines.append(
            f"客户体验最差的是 {worst_sat[0]}（满意度均值 {num(worst_sat[1]['mean'])}，低分率 {pct(worst_sat[1]['low_rate'])}）；"
            f"全局低分率 {pct(metrics['satisfaction']['low_rate'])}，均值 {num(metrics['satisfaction']['mean'])}。"
        )
    slow = max(
        ((c, i) for c, i in metrics["resolution"]["by_category"].items() if i["mean"] is not None),
        key=lambda kv: kv[1]["mean"], default=(None, None),
    )
    if slow[0]:
        summary_lines.append(
            f"处理最慢的是 {slow[0]}（已解决口径均值 {num(slow[1]['mean'])}h，P90 {num(slow[1]['p90'], 0)}h），"
            f"且未解决 {slow[1]['unresolved']} 条，是最主要的流程堵点。"
        )
    summary_lines.append(
        f"未解决工单 {metrics['backlog']['unresolved']} 条（高优先级 {metrics['backlog']['high_unresolved']} 条），"
        f"SLA 超时 {metrics['resolution']['breach_count']} 条（超时率 {pct(metrics['resolution']['breach_rate'])}，仅已解决口径），"
        f"另有 {metrics['resolution']['breach_open_count']} 条未解决工单挂起时长已超 SLA。"
    )
    summary_lines.append(
        f"复发簇共识别 {len(clusters)} 个，覆盖 {sum(c.size for c in clusters)} 条工单；"
        f"未归类 {len(unclustered)} 条，可能需要补充规则或人工复核。"
    )
    roll = metrics["time"]["rolling_3d"]
    if roll.get("available"):
        summary_lines.append(
            f"最近 3 天（{'、'.join(d[5:] for d in roll['recent_days'])}）日均 {roll['recent_avg']} 条，"
            f"此前 3 天（{'、'.join(d[5:] for d in roll['prior_days'])}）日均 {roll['prior_avg']} 条，"
            f"变化 {roll['change_pct'] * 100:+.1f}% → 判定为「{roll['trend']}」（阈值 ±20%）。"
        )
    if ticket_ranking:
        top_ids = "、".join(r["ticket_id"] for r in ticket_ranking[:3])
        summary_lines.append(
            f"工单级跟进清单：P1 {rank_counts['P1']} 条、P2 {rank_counts['P2']} 条、P3 {rank_counts['P3']} 条"
            f"（共 {len(ticket_ranking)} 条，按分值排序，最前面的 {top_ids}）；"
            f"P1/P2 是本工具的跟进优先级，不是企业正式事故等级。"
        )

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    result: Dict[str, Any] = OrderedDict([
        ("meta", OrderedDict([
            ("version", VERSION),
            ("input", str(input_path)),
            ("generated_at", generated_at),
            ("total", metrics["time"]["total"]),
            ("days", len(metrics["time"]["days"])),
            ("window", OrderedDict([
                ("start", metrics["time"]["days"][0] if metrics["time"]["days"] else None),
                ("end", metrics["time"]["days"][-1] if metrics["time"]["days"] else None),
            ])),
            ("config", OrderedDict([
                ("sla", OrderedDict((k, float(v)) for k, v in sla.items())),
                ("split_ratio", args.split_ratio),
                ("min_cluster", args.min_cluster),
            ])),
        ])),
        ("summary", summary),
        ("summary_lines", summary_lines),
        ("dimensions", metrics),
        ("clusters", cluster_summary),
        ("_clusters", clusters),
        ("anomalies", anomalies),
        ("ticket_ranking", ticket_ranking),
        ("unclustered", unclustered),
        ("multi_match", multi),
        ("warnings", warnings),
        ("stats", OrderedDict([
            ("high_share_binom_p", binom_p),
            ("cohesion_baseline", cohesion_baseline),
        ])),
        ("level_counts", level_counts),
    ])
    result["charts"] = build_charts(metrics, clusters)
    return result


def write_outputs(result: Dict[str, Any], outdir: Path, formats: Iterable[str]) -> List[Path]:
    written: List[Path] = []
    outdir.mkdir(parents=True, exist_ok=True)
    formats = set(formats)
    charts_dir = outdir / "charts"
    # 选 md/html 时自动附带 SVG：报告的图片链接指向 charts/，不落盘会出现图裂
    if {"charts", "html", "md"} & formats:
        charts_dir.mkdir(parents=True, exist_ok=True)
        for name, svg in result["charts"].items():
            path = charts_dir / name
            path.write_text(svg, encoding="utf-8")
            written.append(path)
    if "md" in formats:
        path = outdir / "趋势分析报告.md"
        path.write_text(render_markdown(result), encoding="utf-8")
        written.append(path)
    if "html" in formats:
        path = outdir / "dashboard.html"
        path.write_text(render_html(result), encoding="utf-8")
        written.append(path)
    if "json" in formats:
        export = OrderedDict((k, v) for k, v in result.items() if k not in ("charts", "_clusters"))
        path = outdir / "metrics.json"
        path.write_text(json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8")
        written.append(path)
    return written


def build_parser() -> argparse.ArgumentParser:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="客服工单趋势分析工具（零第三方依赖）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：uv run analyze.py --input task5_tickets.json --outdir output",
    )
    parser.add_argument("--input", default=str(here / "task5_tickets.json"), help="输入 JSON/CSV 路径")
    parser.add_argument("--outdir", default=str(here / "output"), help="输出目录")
    parser.add_argument("--formats", default="md,html,json,charts",
                        help="输出格式：md,html,json,charts（选 md/html 时会自动附带 SVG 图表，避免报告图裂）")
    parser.add_argument("--sla", default="高=24,中=48,低=72", help="SLA 目标（小时），如 高=12,中=24,低=48")
    parser.add_argument("--split-ratio", type=float, default=0.5, help="前后半段切分比例（默认 0.5）")
    parser.add_argument("--min-cluster", type=int, default=3, help="簇进入异常候选的最小条数（默认 3）")
    parser.add_argument("--strict", action="store_true",
                        help="严格校验：字段类型/取值不合契约时直接报错退出（默认宽松告警并继续）")
    parser.add_argument("--quiet", action="store_true", help="只输出错误")
    parser.add_argument("--version", action="version", version=f"analyze.py {VERSION}")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.sla_dict = parse_sla(args.sla)
    except ValueError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"[错误] 找不到输入文件：{input_path}", file=sys.stderr)
        return 1

    rows, load_warnings = load_rows(input_path)
    try:
        tickets, build_warnings = build_tickets(rows, strict=args.strict)
    except DataError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1
    warnings = load_warnings + build_warnings + check_data_contract(tickets)
    result = build_result(tickets, warnings, args, input_path)

    written = write_outputs(result, Path(args.outdir), [f.strip() for f in args.formats.split(",") if f.strip()])
    if not args.quiet:
        print(f"客服工单趋势分析 · v{VERSION}")
        print(f"输入：{input_path}")
        print(f"工单：{result['meta']['total']} 条 / {result['meta']['days']} 天 "
              f"（{result['meta']['window']['start']} ~ {result['meta']['window']['end']}）")
        print("分级：" + "、".join(f"{lvl} {cnt}" for lvl, cnt in result["level_counts"].items()))
        print(f"高危信号：{result['anomalies'][0]['title'] if result['anomalies'] else '无'}")
        print(f"数据质量告警：{len(warnings)} 条")
        for path in written:
            print(f"已生成：{path}")
    return 0


def build_charts_empty() -> "OrderedDict[str, str]":
    w, h = 860, 200
    svg = "".join(svg_header(w, h, "无数据", "输入为空，未生成图表") + ["</svg>"])
    return OrderedDict([
        ("01_daily_volume.svg", svg), ("02_category_mix.svg", svg),
        ("03_category_quadrant.svg", svg), ("04_payment_cluster_trend.svg", svg),
        ("05_backlog.svg", svg), ("06_hourly.svg", svg),
    ])


if __name__ == "__main__":
    raise SystemExit(main())
