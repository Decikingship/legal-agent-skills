#!/usr/bin/env python3
"""
证据清单与举证责任分配

诉讼准备中最容易出事的一环：**不是证据不够，是不知道该由谁证明什么。**
举证责任分错，把本该对方证明的事实揽过来，等于自己给自己加难度；
反过来，以为对方该证明而自己不准备，开庭时无法回应。

本脚本做三件确定性的事：
  1. 按「谁主张谁举证」+ 法定倒置/缓和情形，给出举证责任分配
  2. 按待证事实倒推所需证据种类，标出缺口
  3. 逐份证据打「三性」风险标记（真实性/合法性/关联性）与形式要件提示

**证据的证明力大小、能否被采信，是法官判断，本工具不下结论。**
"""
import argparse, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
RULES = os.path.normpath(os.path.join(HERE, "..", "assets", "burden-rules.json"))

# 证据种类（《民事诉讼法》第六十六条）
EVIDENCE_TYPES = ["当事人的陈述", "书证", "物证", "视听资料", "电子数据",
                  "证人证言", "鉴定意见", "勘验笔录"]
# 形式要件风险
FORM_RISK = [
    (r"复印件|复印|扫描件", "高", "复印件无原件核对的，对方否认时真实性难以认定。**开庭须携原件**"),
    (r"微信|聊天记录|短信|邮件|APP|截图|网页", "高",
     "电子数据须证明来源真实、内容完整、未经篡改。**建议做公证或可信时间戳**，"
     "并保留原始载体（手机、服务器）以备当庭演示"),
    (r"录音|录像|视听", "中",
     "视听资料须说明取得方式与时间地点。**取得方式不得侵害他人合法权益或违反禁止性规定**，"
     "否则可能被排除"),
    (r"证人|证言|情况说明", "中",
     "证人应出庭作证，未出庭的书面证言证明力较弱。**须提前申请证人出庭**，逾期申请可能不予准许"),
    (r"境外|域外|国外|香港|澳门|台湾", "高",
     "域外形成的证据须办理公证认证或履行相应证明手续，**办理周期长，须提前安排**"),
    (r"外文|英文|日文", "中", "外文书证须附中文译本"),
    (r"鉴定|评估|审计", "中",
     "单方委托的鉴定意见对方常不认可，**必要时应申请法院委托鉴定**；"
     "申请鉴定有期限要求，逾期可能丧失权利"),
    (r"原件已灭失|遗失|无法取得", "高",
     "原件灭失的须说明原因并提供其他佐证，单独一份复印件难以支撑待证事实"),
]


def load_rules():
    return json.load(open(RULES, encoding="utf-8"))


def allocate(case_type, facts_to_prove, stance, rules):
    """给每项待证事实分配举证责任。"""
    out = []
    for fact in facts_to_prove:
        hit = None
        for r in rules["倒置与特殊分配"]:
            if re.search(r["匹配"], fact) or re.search(r["匹配"], case_type):
                hit = r
                break
        if hit:
            # 「主张方」类规则的责任归属取决于本案谁在主张，不能机械归到某一方
            if "主张方" in hit["责任方"]:
                mine = None
            else:
                mine = hit["责任方"].startswith(stance) or hit["责任方"] == "双方"
            out.append({
                "待证事实": fact,
                "举证责任方": hit["责任方"],
                "依据": hit["依据"],
                "类型": hit["类型"],
                "是否我方承担": mine,
                "说明": hit["说明"] + ("｜**本案由谁主张该事实，责任即在谁**，须先确认"
                                       if mine is None else ""),
            })
        else:
            out.append({
                "待证事实": fact,
                "举证责任方": "主张方",
                "依据": "谁主张谁举证（《民事诉讼法》第六十七条）",
                "类型": "一般规则",
                "是否我方承担": True,
                "说明": "主张该事实的一方承担举证责任。**若该事实由对方主张，则责任在对方**——"
                        "确认清楚，不要把对方的举证责任揽过来",
            })
    return out


def assess(evidence):
    out = []
    for e in evidence:
        name = e if isinstance(e, str) else e.get("名称", "")
        risks = []
        for pat, lvl, note in FORM_RISK:
            if re.search(pat, name):
                risks.append({"等级": lvl, "提示": note})
        etype = next((t for t in EVIDENCE_TYPES if t in name), None)
        if not etype:
            # 顺序即优先级。教训：初版把「书证」的宽泛特征（记录、证明、账）排在前面，
            # 「考勤记录微信截图」被判成书证——而电子数据的形式要件要求完全不同
            # （须证明来源真实、内容完整、未经篡改），判错会漏掉公证与原始载体的准备。
            if re.search(r"微信|短信|邮件|电子邮件|数据|截图|网页|聊天|APP|系统日志|后台", name):
                etype = "电子数据"
            elif re.search(r"录音|录像|视频|监控", name):
                etype = "视听资料"
            elif re.search(r"证人|证言", name):
                etype = "证人证言"
            elif re.search(r"鉴定|评估报告|审计报告", name):
                etype = "鉴定意见"
            elif re.search(r"勘验|现场笔录", name):
                etype = "勘验笔录"
            elif re.search(r"合同|协议|发票|单据|函|记录|报告|证明|凭证|账|书|通知", name):
                etype = "书证"
        out.append({
            "证据名称": name,
            "推定种类": etype or "待确定",
            "形式风险": risks or [{"等级": "低", "提示": "未识别到明显形式要件风险，仍须核对原件与来源"}],
            "待补": "证明对象、来源、原件是否在手、页码" if isinstance(e, str) else None,
        })
    return out


def main():
    p = argparse.ArgumentParser(description="证据清单与举证责任分配",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--case-type", dest="case_type", required=True)
    p.add_argument("--stance", required=True, choices=["原告", "被告", "第三人"])
    p.add_argument("--facts", nargs="+", required=True, help="待证事实，可多项")
    p.add_argument("--evidence", nargs="*", default=[], help="已有证据名称，可多项")
    args = p.parse_args()

    rules = load_rules()
    alloc = allocate(args.case_type, args.facts, args.stance, rules)
    ours = [a for a in alloc if a["是否我方承担"] is True]
    theirs = [a for a in alloc if a["是否我方承担"] is False]
    undetermined = [a for a in alloc if a["是否我方承担"] is None]
    ev = assess(args.evidence)

    high = [e for e in ev if any(r["等级"] == "高" for r in e["形式风险"])]
    notes = [
        f"立场：**{args.stance}**。举证责任分配随立场变化——"
        f"换立场须重跑，把对方的举证责任揽过来是自己给自己加难度",
        "**证明力大小、能否采信是法官判断**，本工具只做责任分配与形式要件提示，不下结论",
        "举证期限逾期提交的证据，对方不同意质证且理由不成立的可能不予采纳；"
        "**申请证人出庭、申请鉴定、申请调查取证均有期限要求**",
        "「谁主张谁举证」的反面同样重要：**对方主张的事实由对方证明**。"
        "确认哪些事实不需要我方举证，可以省下大量准备工作",
    ]
    if high:
        notes.insert(0, f"⚠️ {len(high)} 份证据存在**高等级形式风险**（复印件、电子数据、域外证据等），"
                        f"须在举证期限前完成原件核对、公证或认证")
    if not args.evidence:
        notes.append("未提供已有证据，仅输出举证责任分配与证据种类需求")

    print(json.dumps({
        "案由": args.case_type, "立场": args.stance,
        "举证责任分配": {
            "我方承担": ours,
            "对方承担": theirs or "本次列明的待证事实均由我方承担",
            "待判断（取决于本案谁主张）": undetermined or "无",
        },
        "证据评估": ev,
        "缺口提示": "请对照「我方承担」的每项待证事实，确认是否有对应证据；"
                    "无对应证据的应通过调查取证、申请法院调取或证据保全解决",
        "提示": notes,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
