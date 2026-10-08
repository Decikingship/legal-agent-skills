#!/usr/bin/env python3
"""
案件事实时间轴

时间轴不是把事情按顺序排一遍就完了。它的价值在于**暴露问题**：
  哪个关键事实没有证据支撑、哪两份证据的时间对不上、
  哪段时间空白得可疑、哪个节点触发了时效或期间的起算。

本脚本做四类确定性检查：
  1. **无证据支撑的事实** —— 时间轴上最危险的是"我方陈述"独木支撑的关键节点
  2. **时间冲突** —— 同一事实在不同证据中日期不一致，对方一定会打这里
  3. **时间空白** —— 长时间无动作往往对应时效风险或对方的抗辩点
  4. **法律节点** —— 自动标出可能触发时效起算、中断、期间计算的事件
"""
import argparse, json, re, sys
from datetime import date, datetime, timedelta

# 触发法律后果的事件类型
LEGAL_TRIGGER = [
    (r"签订|签署|成立", "合同成立", "可能涉及成立与生效的区分、印章效力"),
    (r"交付|交货|移交|验收", "履行行为", "关系履行期限、质量异议期起算"),
    (r"付款|支付|转账|收款", "支付行为", "关系债务是否清偿、部分清偿是否构成时效中断"),
    (r"违约|逾期|未按|拒绝|停止", "违约事实", "**可能是诉讼时效起算点**（知道权利受损之日）"),
    (r"催告|催收|发函|通知|要求.{0,6}履行", "催告行为", "**构成诉讼时效中断**，须有送达凭证"),
    (r"对账|确认|签收|盖章确认|还款计划", "承认债务", "**构成诉讼时效中断**，证明力较强"),
    (r"解除|终止|退货", "合同解除", "关系解除权行使期限、解除是否生效"),
    (r"起诉|立案|申请仲裁|报案", "诉讼行为", "**构成诉讼时效中断**"),
    (r"鉴定|评估|检测", "鉴定行为", "关系鉴定意见的效力与质证"),
    (r"知道|发现|得知|收到.{0,6}通知", "知悉时点", "**关系诉讼时效起算日的认定**"),
]


def d(s):
    s = re.sub(r"\D", "", str(s))
    if len(s) == 8:
        return date(int(s[:4]), int(s[4:6]), int(s[6:]))
    if len(s) == 6:
        return date(int(s[:4]), int(s[4:6]), 1)
    if len(s) == 4:
        return date(int(s), 1, 1)
    raise SystemExit(f"错误：日期格式应为 YYYYMMDD/YYYYMM/YYYY，收到「{s}」")


def triggers(desc):
    return [{"类型": t, "法律意义": n} for pat, t, n in LEGAL_TRIGGER if re.search(pat, desc)]


def main():
    p = argparse.ArgumentParser(description="案件事实时间轴",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--plan", help="事件 JSON 文件")
    # 用 append 而非 nargs="*"：重复出现 --event 时 nargs 会覆盖前值，只留最后一条。
    p.add_argument("--event", action="append", default=[],
                   help='事件，格式 "YYYYMMDD|事实描述|证据名称（可空）"。'
                        '可重复使用 --event 添加多条')
    p.add_argument("--gap-days", dest="gap_days", type=int, default=180,
                   help="超过该天数的空白视为需关注，默认180")
    args = p.parse_args()

    raw = []
    if args.plan:
        raw = json.load(open(args.plan, encoding="utf-8")).get("events", [])
    for e in args.event:
        parts = e.split("|")
        if len(parts) < 2:
            sys.exit(f"错误：事件格式应为 日期|事实|证据，收到「{e}」")
        raw.append({"date": parts[0], "fact": parts[1],
                    "evidence": parts[2] if len(parts) > 2 else ""})
    if not raw:
        sys.exit("错误：未提供任何事件")

    events = []
    for r in raw:
        dt = d(r["date"])
        ev = (r.get("evidence") or "").strip()
        events.append({
            "日期": dt.isoformat(), "_d": dt,
            "事实": r["fact"],
            "证据": ev or None,
            "法律节点": triggers(r["fact"]),
            "争议": r.get("disputed", False),
        })
    events.sort(key=lambda x: x["_d"])

    # ① 无证据支撑
    unsupported = [e for e in events if not e["证据"]]
    # ② 时间冲突：同一事实描述出现多个日期
    bykey = {}
    for e in events:
        k = re.sub(r"[\s，。、；：]", "", e["事实"])[:14]
        bykey.setdefault(k, []).append(e)
    conflicts = [{"事实": k, "出现日期": [x["日期"] for x in v],
                  "证据": [x["证据"] for x in v]}
                 for k, v in bykey.items() if len({x["日期"] for x in v}) > 1]
    # ③ 时间空白
    gaps = []
    for a, b in zip(events, events[1:]):
        n = (b["_d"] - a["_d"]).days
        if n >= args.gap_days:
            gaps.append({"起": a["日期"], "止": b["日期"], "天数": n,
                         "前一事件": a["事实"][:30], "后一事件": b["事实"][:30]})
    # ④ 时效相关节点
    limitation_nodes = [
        {"日期": e["日期"], "事实": e["事实"][:40],
         "类型": [t["类型"] for t in e["法律节点"]],
         "意义": [t["法律意义"] for t in e["法律节点"]]}
        for e in events if e["法律节点"]
    ]

    for e in events:
        e.pop("_d")

    notes = [
        "**时间轴的价值在于暴露问题，不在于把事情排一遍。** "
        "排完之后要问：哪个关键节点没有证据、哪两处时间对不上、哪段空白可疑",
        "**无证据支撑的关键事实是最大风险。** 仅凭我方陈述支撑的节点，"
        "对方否认时无法证明——这类节点应优先补证据或调整主张",
        "标出的法律节点须与「诉讼时效分析」交叉核对："
        "催告、对账、还款是否有送达或签收凭证，直接决定中断能否成立",
    ]
    if unsupported:
        notes.insert(0, f"⚠️ {len(unsupported)} 个事实**无证据支撑**，须逐一确认是否属于"
                        f"不争事实、可否补证、或应否从主张中删除")
    if conflicts:
        notes.insert(0, f"⚠️ {len(conflicts)} 处**时间冲突**——同一事实对应多个日期。"
                        f"**对方一定会打这里**，须在开庭前统一口径并说明差异原因")
    if gaps:
        notes.append(f"{len(gaps)} 段超过 {args.gap_days} 天的空白期。"
                     f"长时间无动作常对应时效风险，也常被对方用作「权利人怠于行使权利」的论据")

    print(json.dumps({
        "事件数": len(events),
        "时间跨度": f"{events[0]['日期']} – {events[-1]['日期']}",
        "时间轴": events,
        "问题": {
            "无证据支撑": [{"日期": e["日期"], "事实": e["事实"]} for e in unsupported],
            "时间冲突": conflicts or "无",
            "时间空白": gaps or "无",
        },
        "法律节点": limitation_nodes or "未识别到",
        "提示": notes,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
