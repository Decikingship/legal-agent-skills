#!/usr/bin/env python3
"""
诉讼时效分析

时效错了没有补救。过了时效，实体权利还在但胜诉权没了——对方一提抗辩就败诉。

本脚本做确定性计算：
  起算日认定 → 期间长度（普通/特别）→ 中断事由逐次重算 → 中止事由扣减 →
  最长权利保护期校验 → 剩余天数与紧急度

**三个最容易出错的地方已内置处理：**
  1. **中断是重新起算，不是暂停。** 每次中断后期间从零重新计算——
     算成暂停会把时效算短，误判为已过期而放弃有效债权
  2. **最长保护期是硬顶。** 自权利受到损害之日起超过二十年的，法院不予保护，
     中断事由再多也无法突破（有特殊情况可申请延长）
  3. **时效抗辩不主动适用。** 法院不得主动援引时效，须由义务人提出——
     这决定了原告方的策略（不主动提及）与被告方的动作（必须主动抗辩）
"""

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta

# 特别时效期间。默认三年为普通时效。
SPECIAL_PERIODS = [
    (r"国际货物买卖|技术进出口", 4 * 365, "四年", "国际货物买卖合同和技术进出口合同争议"),
    (r"海上货物运输|海事请求|船舶", 365, "一年（部分海事请求为二年）",
     "海商领域时效期间特殊，须按海商法具体条文核对"),
    (r"人寿保险", 5 * 365, "五年", "人寿保险的保险金请求权"),
    (r"保险金请求权|保险赔偿", 2 * 365, "二年", "人寿保险以外的保险金请求权"),
]
# 不适用诉讼时效的请求权
NO_LIMITATION = [
    (r"停止侵害|排除妨碍|消除危险", "请求停止侵害、排除妨碍、消除危险"),
    (r"不动产物权|登记的动产物权|返还财产.{0,10}(登记|不动产)", "不动产物权及登记的动产物权的权利人请求返还财产"),
    (r"抚养费|赡养费|扶养费", "请求支付抚养费、赡养费或者扶养费"),
    (r"存款本金|国债|金融债券|向不特定对象发行的公司债券", "兑付存款本金及利息、国债等"),
]
# 中断事由
INTERRUPT_KINDS = {
    "催告": "权利人向义务人提出履行请求",
    "起诉": "权利人提起诉讼或申请仲裁",
    "承诺履行": "义务人同意履行义务",
    "申请支付令": "申请支付令、申报破产债权等与提起诉讼具有同等效力的情形",
    "对账确认": "义务人签署对账单、还款计划等承认债务的行为",
    "报案": "向公安、检察机关报案或控告（属与提起诉讼同等效力的情形）",
}
MAX_PROTECTION_YEARS = 20


def d(s):
    s = re.sub(r"\D", "", str(s))
    if len(s) != 8:
        raise SystemExit(f"错误：日期格式应为 YYYYMMDD，收到「{s}」")
    return date(int(s[:4]), int(s[4:6]), int(s[6:]))


def add_days(base, n):
    return base + timedelta(days=n)


def pick_period(case_type):
    for pat, days, label, note in SPECIAL_PERIODS:
        if re.search(pat, case_type or ""):
            return days, label, note
    return 3 * 365, "三年", "普通诉讼时效期间为三年（《民法典》第一百八十八条）"


def check_no_limitation(case_type, claim):
    txt = (case_type or "") + " " + (claim or "")
    return [{"情形": note} for pat, note in NO_LIMITATION if re.search(pat, txt)]


def main():
    p = argparse.ArgumentParser(description="诉讼时效分析",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--case-type", dest="case_type", default="", help="案由或纠纷类型")
    p.add_argument("--claim", default="", help="诉讼请求内容，用于判断是否适用时效")
    p.add_argument("--know-date", dest="know_date", required=True,
                   help="**知道或应当知道权利受损害及义务人之日** YYYYMMDD（时效起算日）")
    p.add_argument("--damage-date", dest="damage_date",
                   help="权利受到损害之日 YYYYMMDD，用于二十年最长保护期校验。"
                        "不填则以起算日代替")
    p.add_argument("--interrupt", nargs="*", default=[],
                   help='中断事由，格式 "YYYYMMDD:类型"，如 20230501:催告。可多个')
    p.add_argument("--suspend", nargs="*", default=[],
                   help='中止事由，格式 "开始:结束"，如 20240101:20240301。可多个')
    p.add_argument("--today", help="计算基准日 YYYYMMDD，默认今天")
    args = p.parse_args()

    today = d(args.today) if args.today else date.today()
    start = d(args.know_date)
    damage = d(args.damage_date) if args.damage_date else start
    days, label, period_note = pick_period(args.case_type)

    excluded = check_no_limitation(args.case_type, args.claim)

    # 中断：每次重新起算
    events, cur = [], start
    interrupts = []
    for it in args.interrupt:
        if ":" not in it:
            sys.exit(f"错误：中断事由格式应为 YYYYMMDD:类型，收到「{it}」")
        dt, kind = it.split(":", 1)
        interrupts.append((d(dt), kind.strip()))
    interrupts.sort()

    events.append({"日期": start.isoformat(), "事件": "时效起算",
                   "说明": "自权利人知道或应当知道权利受到损害以及义务人之日起算",
                   "此后届满日": add_days(cur, days).isoformat()})
    for dt, kind in interrupts:
        expire_before = add_days(cur, days)
        if dt > expire_before:
            events.append({
                "日期": dt.isoformat(), "事件": f"中断事由（{kind}）",
                "⚠️": f"该事由发生于上一期间届满日 {expire_before.isoformat()} **之后**，"
                       f"时效已过，**中断不发生效力**。除非能证明此前另有中断或中止事由",
                "此后届满日": expire_before.isoformat()})
            continue
        cur = dt
        events.append({
            "日期": dt.isoformat(), "事件": f"中断事由（{kind}）",
            "说明": INTERRUPT_KINDS.get(kind, "须核实是否构成法定中断事由")
                    + "。**中断后期间重新计算，不是暂停**",
            "此后届满日": add_days(cur, days).isoformat()})

    base_expire = add_days(cur, days)

    # 中止：末六个月内发生的障碍才产生中止效力，障碍消除后满六个月期间届满
    suspends, susp_note = [], []
    for sp in args.suspend:
        if ":" not in sp:
            sys.exit(f"错误：中止事由格式应为 开始:结束，收到「{sp}」")
        a, b = sp.split(":", 1)
        suspends.append((d(a), d(b)))
    final_expire = base_expire
    for a, b in sorted(suspends):
        last_six = add_days(base_expire, -180)
        if a < last_six:
            susp_note.append({
                "期间": f"{a.isoformat()} – {b.isoformat()}",
                "效力": "不产生中止",
                "说明": f"中止仅适用于时效期间**最后六个月内**发生的障碍。"
                        f"本次障碍开始于 {a.isoformat()}，早于最后六个月起点 "
                        f"{last_six.isoformat()}"})
            continue
        cand = add_days(b, 180)
        susp_note.append({
            "期间": f"{a.isoformat()} – {b.isoformat()}",
            "效力": "产生中止",
            "说明": f"障碍消除之日起满六个月时效期间届满，即 {cand.isoformat()}"})
        final_expire = max(final_expire, cand)

    # 二十年最长保护期
    max_expire = date(damage.year + MAX_PROTECTION_YEARS, damage.month, damage.day) \
        if not (damage.month == 2 and damage.day == 29) else date(damage.year + MAX_PROTECTION_YEARS, 3, 1)
    capped = final_expire > max_expire
    effective = min(final_expire, max_expire)

    remaining = (effective - today).days
    if excluded:
        status = "不适用诉讼时效"
    elif remaining < 0:
        status = "已过时效"
    elif remaining <= 90:
        status = "紧急（不足三个月）"
    elif remaining <= 180:
        status = "临近（不足六个月）"
    else:
        status = "时效内"

    notes = [
        "**中断是重新起算，不是暂停。** 每次中断后期间从零重新计算——"
        "算成暂停会把时效算短，可能误判为已过期而放弃有效债权",
        "**时效抗辩不主动适用。** 法院不得主动援引诉讼时效。"
        "对原告：不主动提及；对被告：**必须在一审中主动提出抗辩**，二审才提的一般不予支持",
        "中断事由的**证据比事由本身更重要**——催告需有送达凭证，"
        "对账单需有对方签章，微信催收需完整聊天记录与身份对应",
        "起算日的认定常有争议。「知道或应当知道」是主观标准，"
        "分期履行债务的时效自最后一期履行期限届满之日起算",
    ]
    if capped:
        notes.insert(0, f"⚠️ 计算届满日 {final_expire.isoformat()} 已超过二十年最长保护期 "
                        f"{max_expire.isoformat()}。**最长保护期是硬顶**，"
                        f"中断事由再多也无法突破（有特殊情况可申请人民法院延长）")
    if excluded:
        notes.insert(0, "⚠️ 本案请求可能属于**不适用诉讼时效**的情形，"
                        "下方计算仅供参考，须先确认请求权性质")
    if status in ("紧急（不足三个月）", "临近（不足六个月）"):
        notes.insert(0, f"⚠️ **{status}**，应立即安排起诉或采取中断措施并保全证据")
    if status == "已过时效":
        notes.insert(0, "⚠️ **按当前信息已过时效**。核对方向：是否有未记录的中断事由"
                        "（对账、还款、催告）、是否存在中止情形、起算日认定是否可争取推后")

    print(json.dumps({
        "案由": args.case_type or "未指定",
        "时效期间": {"长度": label, "依据": period_note},
        "不适用时效的情形": excluded or "未识别到",
        "起算日": start.isoformat(),
        "时间轴": events,
        "中止事由": susp_note or "无",
        "届满日": {
            "按中断中止计算": final_expire.isoformat(),
            "二十年最长保护期": max_expire.isoformat(),
            "**实际届满日**": effective.isoformat(),
            "受最长保护期限制": capped,
        },
        "基准日": today.isoformat(),
        "剩余天数": remaining,
        "状态": status,
        "提示": notes,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
