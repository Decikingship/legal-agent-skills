#!/usr/bin/env python3
"""
行政救济期限与路径

行政案件被驳回起诉，多数不是因为实体没理，而是**期限过了或路径走错**。

本脚本处理三件确定性的事：
  1. **复议前置判断** —— 法定复议前置的事项未经复议直接起诉，必然被驳回起诉
  2. **起诉期限计算** —— 行政诉讼的期限种类多、起算点各异，比民事复杂得多
  3. **告知瑕疵的期限延长** —— 未告知诉权或起诉期限的，期限从知道或应当知道之日起算，
     但最长不超过法定上限。这一条是当事人错过期限后最重要的救济空间

**不做的事**：不判断具体行政行为是否合法，不预测撤销可能性。

用法：
    admin_remedy.py --action-type 行政处罚 --action-date 20260301 --served-date 20260305 \
        --informed-rights --today 20260804
"""

import argparse
import json
import re
from datetime import date, timedelta

# 法定复议前置的常见事项。未经复议直接起诉将被驳回起诉。
REVIEW_FIRST = [
    (r"纳税|税务|征税|税款|滞纳金", "纳税争议",
     "对征税行为不服的，**必须先申请行政复议**，对复议决定不服才能起诉"),
    (r"自然资源|土地|矿产|水流|森林|山岭|草原|荒地|滩涂|海域.{0,6}(确认|所有权|使用权)",
     "自然资源权属确认", "对确认自然资源所有权或使用权的行为不服的，**复议前置**"),
    (r"专利.{0,6}(复审|无效)", "专利复审与无效", "须经国务院专利行政部门复审程序"),
    (r"政府信息公开.{0,10}(不予|不属于|无法提供)", "部分信息公开争议",
     "部分地区与事项规定复议前置，**须核对具体规定**"),
]

# 起诉期限。行政诉讼的期限种类多，起算点各异。
DEADLINES = {
    "直接起诉": {"days": 180, "label": "六个月",
                 "start": "知道或者应当知道作出行政行为之日",
                 "note": "一般起诉期限"},
    "经复议后起诉": {"days": 15, "label": "十五日",
                     "start": "收到复议决定书之日",
                     "note": "对复议决定不服的起诉期限"},
    "复议逾期未决后起诉": {"days": 15, "label": "十五日",
                           "start": "复议期满之日",
                           "note": "复议机关逾期不作决定的，可自复议期满之日起十五日内起诉"},
    "不动产案件最长": {"days": 20 * 365, "label": "二十年",
                       "start": "行政行为作出之日",
                       "note": "涉及不动产的案件，自行政行为作出之日起超过二十年的，法院不受理"},
    "其他案件最长": {"days": 5 * 365, "label": "五年",
                     "start": "行政行为作出之日",
                     "note": "其他案件自行政行为作出之日起超过五年的，法院不受理"},
}
REVIEW_APPLY_DAYS = 60  # 申请行政复议的一般期限：知道之日起六十日


def d(s):
    s = re.sub(r"\D", "", str(s))
    if len(s) != 8:
        raise SystemExit(f"错误：日期格式应为 YYYYMMDD，收到「{s}」")
    return date(int(s[:4]), int(s[4:6]), int(s[6:]))


def check_review_first(action_type, desc):
    txt = f"{action_type} {desc}"
    return [{"事项": name, "说明": note}
            for pat, name, note in REVIEW_FIRST if re.search(pat, txt)]


def main():
    p = argparse.ArgumentParser(description="行政救济期限与路径",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--action-type", dest="action_type", required=True,
                   help="行政行为类型，如 行政处罚 / 行政强制 / 行政许可 / 征收 / 纳税争议")
    p.add_argument("--desc", default="", help="补充描述，用于复议前置识别")
    p.add_argument("--action-date", dest="action_date", required=True,
                   help="行政行为作出之日 YYYYMMDD")
    p.add_argument("--served-date", dest="served_date",
                   help="送达或知道之日 YYYYMMDD，不填则以作出之日计")
    p.add_argument("--informed-rights", dest="informed_rights", action="store_true",
                   help="行政机关**已告知**诉权与起诉期限")
    p.add_argument("--review-decision-date", dest="review_decision_date",
                   help="收到复议决定书之日 YYYYMMDD（已经复议的）")
    p.add_argument("--review-apply-date", dest="review_apply_date",
                   help="申请复议之日 YYYYMMDD，用于计算复议逾期未决的起诉期限")
    p.add_argument("--real-estate", dest="real_estate", action="store_true",
                   help="涉及不动产")
    p.add_argument("--today", help="计算基准日")
    args = p.parse_args()

    today = d(args.today) if args.today else date.today()
    acted = d(args.action_date)
    known = d(args.served_date) if args.served_date else acted

    rf = check_review_first(args.action_type, args.desc)

    paths = []
    # 路径一：直接起诉
    if args.informed_rights:
        base, start_desc = known, "知道或应当知道作出行政行为之日"
        cap_note = None
    else:
        base, start_desc = known, "知道或应当知道行政行为内容之日"
        cap_note = ("⚠️ **行政机关未告知诉权或起诉期限的**，起诉期限从知道或应当知道"
                    "**诉权或起诉期限**之日起计算，但最长不超过法定上限（不动产二十年 / 其他五年）。"
                    "**这是错过期限后最重要的救济空间**——须核实行政决定书是否载明诉权告知")
    e = base + timedelta(days=DEADLINES["直接起诉"]["days"])
    paths.append({
        "路径": "直接提起行政诉讼", "期限": DEADLINES["直接起诉"]["label"],
        "起算": start_desc, "起算日": base.isoformat(), "届满": e.isoformat(),
        "剩余天数": (e - today).days,
        "状态": "已过" if e < today else ("紧急" if (e - today).days <= 30 else "期限内"),
        "备注": cap_note,
    })

    # 路径二：先复议
    re_e = known + timedelta(days=REVIEW_APPLY_DAYS)
    paths.append({
        "路径": "申请行政复议", "期限": "六十日",
        "起算": "知道该行政行为之日", "起算日": known.isoformat(),
        "届满": re_e.isoformat(), "剩余天数": (re_e - today).days,
        "状态": "已过" if re_e < today else ("紧急" if (re_e - today).days <= 15 else "期限内"),
        "备注": "复议不收费、周期短，且复议机关可审查合理性（法院原则上只审合法性）。"
                "**即便不是复议前置事项，复议也常是性价比更高的第一步**",
    })

    # 路径三：经复议后起诉
    if args.review_decision_date:
        rd = d(args.review_decision_date)
        rde = rd + timedelta(days=15)
        paths.append({
            "路径": "对复议决定不服起诉", "期限": "十五日",
            "起算": "收到复议决定书之日", "起算日": rd.isoformat(),
            "届满": rde.isoformat(), "剩余天数": (rde - today).days,
            "状态": "已过" if rde < today else ("紧急" if (rde - today).days <= 5 else "期限内"),
            "备注": "⚠️ **仅十五日，是行政诉讼中最短的起诉期限之一**，极易错过",
        })
    if args.review_apply_date:
        ra = d(args.review_apply_date)
        expire = ra + timedelta(days=60)  # 复议一般审理期限
        rde2 = expire + timedelta(days=15)
        paths.append({
            "路径": "复议逾期未决后起诉", "期限": "十五日",
            "起算": "复议期满之日", "起算日": expire.isoformat(),
            "届满": rde2.isoformat(), "剩余天数": (rde2 - today).days,
            "状态": "已过" if rde2 < today else "期限内",
            "备注": "复议机关逾期不作决定的，可自复议期满之日起十五日内起诉。"
                    "**复议审理期限可能依法延长，须核对具体规定与延长通知**",
        })

    # 最长保护期
    cap = DEADLINES["不动产案件最长" if args.real_estate else "其他案件最长"]
    cap_expire = acted + timedelta(days=cap["days"])
    max_limit = {
        "类型": "不动产案件" if args.real_estate else "其他案件",
        "期限": cap["label"], "起算": "行政行为作出之日", "起算日": acted.isoformat(),
        "届满": cap_expire.isoformat(), "剩余天数": (cap_expire - today).days,
        "说明": cap["note"] + "。**这是硬顶**，告知瑕疵导致的期限延长也不能突破",
    }

    notes = []
    if rf:
        notes.insert(0, f"⚠️ **本案可能属复议前置事项（{rf[0]['事项']}）**。"
                        f"未经复议直接起诉将被**驳回起诉**，务必先核实")
    if not args.informed_rights:
        notes.append("未确认行政机关是否告知诉权与起诉期限。**这一项直接决定起诉期限的起算点**，"
                     "须调取行政决定书原件核实是否载明诉权告知")
    urgent = [x for x in paths if x["状态"] in ("紧急", "已过")]
    if urgent:
        notes.insert(0, f"⚠️ {len(urgent)} 条路径处于**{urgent[0]['状态']}**状态")
    notes += [
        "行政诉讼**原则上只审合法性**（滥用职权、明显不当除外）；"
        "复议机关可一并审查合理性——**觉得处罚过重但程序合法的，复议往往比诉讼更有用**",
        "**起诉不停止执行**为原则。需要停止执行的，应单独申请",
        "行政赔偿可一并提出，也可单独提出，但期限规则不同，须单独核算",
    ]

    print(json.dumps({
        "行政行为": {"类型": args.action_type, "作出日": acted.isoformat(),
                     "知道/送达日": known.isoformat(),
                     "已告知诉权": args.informed_rights},
        "复议前置": rf or "未识别到法定复议前置情形（**仍须核对具体领域规定**）",
        "救济路径与期限": paths,
        "最长起诉期限（硬顶）": max_limit,
        "基准日": today.isoformat(),
        "提示": notes,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
