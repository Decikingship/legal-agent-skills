#!/usr/bin/env python3
"""
法律计算器 —— 确定性计算，不做法律判断

设计原则：
  1. **不内置利率数值。** LPR 逐月发布、会变，硬编进来算错了完全看不出来。
     本工具负责分段、天数、公式；利率必须由调用方提供，并注明来源。
     内置的只有「写在法规里、不随时间漂移」的规则（诉讼费分段、迟延履行金日万分之一点七五等）。
  2. **计算过程全部展开。** 律师要能逐段复核，不能只给一个数。
  3. **不判断该不该算。** 例如「经济补偿按 2N 还是 N」取决于解除是否违法，
     那是法律判断，须由律师指定 --multiple，工具不猜。

子命令：
  interest   分段计息（逾期利息、资金占用费）
  severance  经济补偿金 / 违法解除赔偿金 / 未签合同二倍工资
  courtfee   诉讼费、保全费
  delay      迟延履行期间加倍部分债务利息
  period     期间计算（起算、届满、顺延）
"""

import argparse
import json
import sys
from datetime import date, datetime, timedelta

# ── 写在法规里的固定规则（仍应经搜法核验现行有效性）────────────────────
# 《诉讼费用交纳办法》第十三条 财产案件受理费分段累进
COURT_FEE_TIERS = [
    (10_000, 0.0, 50.0),          # 不超过1万元，每件50元
    (100_000, 0.025, None),
    (200_000, 0.02, None),
    (500_000, 0.015, None),
    (1_000_000, 0.01, None),
    (2_000_000, 0.009, None),
    (5_000_000, 0.008, None),
    (10_000_000, 0.007, None),
    (20_000_000, 0.006, None),
    (float("inf"), 0.005, None),
]
# 《诉讼费用交纳办法》第十四条 财产保全申请费
PRESERVE_TIERS = [(1_000, 0.0, 30.0), (100_000, 0.01, None), (float("inf"), 0.005, None)]
PRESERVE_CAP = 5_000.0
# 《关于执行程序中计算迟延履行期间的债务利息适用法律若干问题的解释》
DELAY_DAILY_RATE = 0.000175  # 日万分之一点七五

SOURCE_NOTE = {
    "courtfee": "《诉讼费用交纳办法》第十三条",
    "preserve": "《诉讼费用交纳办法》第十四条（每件最高5000元）",
    "delay": "《关于执行程序中计算迟延履行期间的债务利息适用法律若干问题的解释》第一条",
    "severance": "《劳动合同法》第四十七条、第八十七条、第八十二条",
}


def d(s):
    s = str(s).replace("-", "").replace("/", "").strip()
    if len(s) != 8 or not s.isdigit():
        raise SystemExit(f"错误：日期格式应为 YYYYMMDD 或 YYYY-MM-DD，收到「{s}」")
    return date(int(s[:4]), int(s[4:6]), int(s[6:]))


def money(x):
    return round(float(x) + 1e-9, 2)


# ── 分段计息 ──────────────────────────────────────────────────────────
def calc_interest(args):
    start, end = d(args.start), d(args.end)
    if end <= start:
        raise SystemExit("错误：截止日必须晚于起算日")

    segs = []
    if args.rate_table:
        table = json.load(open(args.rate_table, encoding="utf-8"))
        # [{"from":"20220820","rate":0.0365,"source":"..."} ...]
        table = sorted(table, key=lambda r: d(r["from"]))
        for i, row in enumerate(table):
            s = max(d(row["from"]), start)
            e = min(d(table[i + 1]["from"]) if i + 1 < len(table) else end, end)
            if e > s:
                segs.append((s, e, float(row["rate"]), row.get("source", "")))
    elif args.rate is not None:
        segs.append((start, end, float(args.rate), args.rate_source or "调用方提供"))
    else:
        raise SystemExit(
            "错误：必须提供 --rate 或 --rate-table。\n"
            "本工具不内置 LPR 等利率数值——利率逐期变动，内置值一旦过期，"
            "算错了完全看不出来。请从全国银行间同业拆借中心公布的数据取值，"
            "或在文书中注明利率来源。")

    basis = 365
    rows, total = [], 0.0
    for s, e, r, src in segs:
        days = (e - s).days
        amt = float(args.principal) * r * days / basis
        total += amt
        rows.append({"起": s.isoformat(), "止": e.isoformat(), "天数": days,
                     "年利率": r, "利息": money(amt), "利率来源": src})

    return {
        "计算项": "分段计息",
        "本金": money(args.principal),
        "起算日": start.isoformat(), "截止日": end.isoformat(),
        "计息天数合计": sum(x["天数"] for x in rows),
        "计息基数": f"按{basis}日年化",
        "分段明细": rows,
        "利息合计": money(total),
        "本息合计": money(float(args.principal) + total),
        "提示": [
            "⚠️ 利率数值由调用方提供，本工具不核验其真实性，请注明来源",
            "起算日与截止日的确定（催告日、起诉日、判决确定之日、实际清偿日）"
            "属法律判断，须由律师依约定与法律规定确定，本工具按给定日期计算",
            "约定利率是否超过司法保护上限，须另行检索现行规定核验",
        ],
    }


# ── 经济补偿 ──────────────────────────────────────────────────────────
def service_years(start, end):
    """《劳动合同法》47条：满一年一个月；六个月以上不满一年按一年；不满六个月半个月。"""
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day < start.day:
        months -= 1
    months = max(months, 0)
    full = months // 12
    rem = months % 12
    if rem == 0:
        return full, 0.0, f"{full}年整"
    if rem >= 6:
        return full + 1, 0.0, f"{full}年{rem}个月，不满一年但满六个月，按一年计"
    return full, 0.5, f"{full}年{rem}个月，不满六个月，加计半个月"


def calc_severance(args):
    start, end = d(args.start), d(args.end)
    yrs, half, desc = service_years(start, end)
    base = float(args.monthly_wage)
    notes = [f"工作年限：{desc}"]

    capped = False
    if args.avg_wage:
        cap = float(args.avg_wage) * 3
        if base > cap:
            base = cap
            capped = True
            notes.append(
                f"月工资 {money(args.monthly_wage)} 高于当地上年度职工月平均工资三倍 "
                f"{money(cap)}，按三倍计（《劳动合同法》47条第2款）")
    else:
        notes.append(
            "⚠️ 未提供当地上年度职工月平均工资（--avg-wage），未做三倍封顶判断。"
            "月工资较高时结果可能偏高")

    years_paid = yrs + half
    if capped and years_paid > 12:
        years_paid = 12
        notes.append("适用三倍封顶的，支付年限最高不超过十二年")

    n = money(base * years_paid)
    result = {
        "计算项": "经济补偿金", "法律依据": SOURCE_NOTE["severance"],
        "入职日": start.isoformat(), "解除/终止日": end.isoformat(),
        "计算基数(月工资)": money(base), "计发年限": years_paid,
        "经济补偿金(N)": n,
    }
    m = args.multiple
    if m == "n1":
        result["额外一个月代通知金(+1)"] = money(base)
        result["合计(N+1)"] = money(n + base)
        notes.append("N+1 中的「+1」是代通知金，仅适用于《劳动合同法》40条情形，"
                     "且用人单位选择支付而非提前三十日通知时才产生")
    elif m == "2n":
        result["违法解除赔偿金(2N)"] = money(n * 2)
        notes.append("2N 适用于用人单位违法解除或终止（《劳动合同法》87条）。"
                     "**是否构成违法解除属法律判断，本工具不作认定**")

    if args.unsigned_months:
        um = min(int(args.unsigned_months), 11)
        result["未签书面合同二倍工资差额"] = money(float(args.monthly_wage) * um)
        notes.append(f"未签书面劳动合同二倍工资：按 {um} 个月计（法定最多11个月，"
                     f"《劳动合同法》82条）。该项另有仲裁时效问题，须单独判断")

    notes.append("⚠️ 月工资应为解除前十二个月的平均工资，含奖金、津贴、补贴，"
                 "计算基数的口径本身常有争议，须核对工资构成")
    result["提示"] = notes
    return result


# ── 诉讼费 / 保全费 ────────────────────────────────────────────────────
def tiered(amount, tiers, cap=None):
    rows, total, lower = [], 0.0, 0.0
    for upper, rate, flat in tiers:
        if amount <= lower:
            break
        part = min(amount, upper) - lower
        if flat is not None:
            fee = flat
            rows.append({"区间": f"0–{int(upper)}", "适用": f"每件{flat}元", "费用": money(fee)})
        else:
            fee = part * rate
            rows.append({"区间": f"{int(lower)}–{int(upper) if upper != float('inf') else '以上'}",
                         "该段金额": money(part), "费率": rate, "费用": money(fee)})
        total += fee
        lower = upper
    if cap and total > cap:
        rows.append({"说明": f"超过上限，按 {cap} 元计"})
        total = cap
    return rows, money(total)


def calc_courtfee(args):
    amt = float(args.amount)
    rows, total = tiered(amt, COURT_FEE_TIERS)
    out = {"计算项": "案件受理费（财产案件）", "法律依据": SOURCE_NOTE["courtfee"],
           "诉讼标的额": money(amt), "分段明细": rows, "一审受理费": total}
    notes = ["分段累进，非按总额乘单一费率",
             "简易程序减半交纳；适用小额诉讼、调解结案的另有减半规定",
             "二审受理费按上诉请求的金额另行计算，非按一审标的额"]
    if args.appeal:
        _, ap = tiered(float(args.appeal), COURT_FEE_TIERS)
        out["上诉请求金额"] = money(args.appeal)
        out["二审受理费"] = ap
    if args.simplified:
        out["简易程序减半后"] = money(total / 2)
    if args.preserve is not None:
        prows, ptotal = tiered(float(args.preserve), PRESERVE_TIERS, PRESERVE_CAP)
        out["保全申请费"] = {"依据": SOURCE_NOTE["preserve"],
                             "保全财产数额": money(args.preserve),
                             "分段明细": prows, "保全费": ptotal}
        notes.append("保全费每件最高5000元；保全担保费用（保险费）另计，不在本项内")
    notes.append("⚠️ 费用标准以现行《诉讼费用交纳办法》为准，"
                 "部分法院、部分案件类型有特别规定，须以受理法院实际核定为准")
    out["提示"] = notes
    return out


# ── 迟延履行金 ────────────────────────────────────────────────────────
def calc_delay(args):
    start, end = d(args.start), d(args.end)
    days = (end - start).days
    if days <= 0:
        raise SystemExit("错误：迟延截止日必须晚于起算日")
    amt = float(args.principal) * DELAY_DAILY_RATE * days
    return {
        "计算项": "迟延履行期间加倍部分债务利息",
        "法律依据": SOURCE_NOTE["delay"],
        "债务本金": money(args.principal),
        "迟延起算日": start.isoformat(), "计算截止日": end.isoformat(),
        "迟延天数": days,
        "日利率": "万分之一点七五（0.0175%）",
        "加倍部分债务利息": money(amt),
        "提示": [
            "本项为「加倍部分」，与生效法律文书确定的一般债务利息**并列**，不是替代关系",
            "起算日为生效法律文书确定的履行期间届满次日；未确定履行期间的，"
            "自法律文书生效之日起算",
            "被执行人有多笔债务的清偿抵充顺序另有规定，须单独判断",
        ],
    }


# ── 期间计算 ──────────────────────────────────────────────────────────
def calc_period(args):
    start = d(args.start)
    holidays = set()
    if args.holidays:
        holidays = {d(x) for x in json.load(open(args.holidays, encoding="utf-8"))}

    begin = start + timedelta(days=1)  # 期间开始的时和日，不计算在期间内
    if args.unit == "day":
        end = begin + timedelta(days=int(args.length) - 1)
    else:
        m = begin.month - 1 + int(args.length)
        y = begin.year + m // 12
        m = m % 12 + 1
        day = begin.day
        while True:
            try:
                end = date(y, m, day) - timedelta(days=1)
                break
            except ValueError:
                day -= 1

    orig = end
    shifted = []
    while end.weekday() >= 5 or end in holidays:
        why = "周末" if end.weekday() >= 5 else "法定节假日"
        shifted.append({"原届满日": end.isoformat(), "原因": why})
        end += timedelta(days=1)

    out = {
        "计算项": "期间计算",
        "起算基准日": start.isoformat(),
        "期间开始日": begin.isoformat(),
        "期间长度": f"{args.length}{'日' if args.unit == 'day' else '个月'}",
        "届满日(未顺延)": orig.isoformat(),
        "届满日(顺延后)": end.isoformat(),
        "顺延过程": shifted or "无需顺延",
        "剩余天数": (end - date.today()).days,
        "提示": [
            "期间开始的时和日不计算在内，自次日起算（《民事诉讼法》期间规定）",
            "期间届满的最后一日是法定休假日的，以休假日后的第一日为届满日",
            "期间不包括在途时间，诉讼文书在届满前交邮的不算过期",
        ],
    }
    if not args.holidays:
        out["提示"].insert(0, "⚠️ 未提供法定节假日表（--holidays），仅按周末顺延。"
                              "春节、国庆等长假会影响届满日，务必补充当年节假日安排")
    return out


def main():
    p = argparse.ArgumentParser(description="法律计算器（确定性计算，不做法律判断）")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("interest", help="分段计息")
    a.add_argument("--principal", required=True, type=float, help="本金")
    a.add_argument("--start", required=True, help="起算日 YYYYMMDD")
    a.add_argument("--end", required=True, help="截止日 YYYYMMDD")
    a.add_argument("--rate", type=float, help="年利率，如 0.0365")
    a.add_argument("--rate-source", dest="rate_source", help="利率来源说明")
    a.add_argument("--rate-table", dest="rate_table",
                   help='分段利率表 JSON：[{"from":"20220820","rate":0.0365,"source":"LPR"}]')

    b = sub.add_parser("severance", help="经济补偿金 / 赔偿金 / 二倍工资")
    b.add_argument("--start", required=True, help="入职日")
    b.add_argument("--end", required=True, help="解除或终止日")
    b.add_argument("--monthly-wage", dest="monthly_wage", required=True, type=float,
                   help="解除前十二个月平均工资")
    b.add_argument("--avg-wage", dest="avg_wage", type=float,
                   help="当地上年度职工月平均工资，用于三倍封顶判断")
    b.add_argument("--multiple", choices=["n", "n1", "2n"], default="n",
                   help="n=经济补偿；n1=加代通知金；2n=违法解除赔偿金。**须由律师判断后指定**")
    b.add_argument("--unsigned-months", dest="unsigned_months", type=int,
                   help="未签书面劳动合同月数，法定最多11个月")

    c = sub.add_parser("courtfee", help="诉讼费、保全费")
    c.add_argument("--amount", required=True, type=float, help="诉讼标的额")
    c.add_argument("--appeal", type=float, help="上诉请求金额，另算二审受理费")
    c.add_argument("--simplified", action="store_true", help="适用简易程序，减半")
    c.add_argument("--preserve", type=float, help="申请保全的财产数额")

    e = sub.add_parser("delay", help="迟延履行期间加倍部分债务利息")
    e.add_argument("--principal", required=True, type=float, help="债务本金")
    e.add_argument("--start", required=True, help="履行期届满次日")
    e.add_argument("--end", required=True, help="计算截止日")

    f = sub.add_parser("period", help="期间计算")
    f.add_argument("--start", required=True, help="起算基准日（如判决送达日）")
    f.add_argument("--length", required=True, type=int, help="期间长度")
    f.add_argument("--unit", choices=["day", "month"], default="day")
    f.add_argument("--holidays", help="法定节假日 JSON 数组文件：[\"20260101\", ...]")

    args = p.parse_args()
    fn = {"interest": calc_interest, "severance": calc_severance,
          "courtfee": calc_courtfee, "delay": calc_delay, "period": calc_period}[args.cmd]
    print(json.dumps(fn(args), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
