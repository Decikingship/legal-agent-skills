#!/usr/bin/env python3
"""
管辖权分析

立案前的第一个卡点。管辖错了，轻则移送耽误几个月，重则被驳回起诉、
再起诉时可能已过时效。

本脚本做确定性的规则判断：
  专属管辖识别（**优先级最高，排除协议管辖**）→ 协议管辖效力 →
  特殊地域管辖 → 一般地域管辖 → 级别管辖 → 冲突提示

**不做的事**：不判断具体某个法院是否有管辖权（需要当事人住所地、
合同履行地等事实的精确认定），只输出「按什么规则、可选哪些连接点、
有哪些风险」，事实认定交给律师。

用法：
    jurisdiction.py --case-type 买卖合同纠纷 --plaintiff-domicile 江苏省南京市 \
        --defendant-domicile 广东省深圳市 --performance-place 江苏省苏州市 \
        --agreement "由原告所在地人民法院管辖" --amount 5000000
"""

import argparse
import json
import re
import sys

# ── 专属管辖：法定强制，当事人不得协议变更，也排除一般与特殊地域管辖 ──
EXCLUSIVE = [
    (r"不动产|房屋买卖|房屋租赁|土地|建设用地使用权|相邻关系|物权确认.{0,6}不动产",
     "不动产所在地", "因不动产纠纷提起的诉讼，由不动产所在地法院专属管辖"),
    (r"港口作业", "港口所在地", "因港口作业中发生纠纷提起的诉讼，由港口所在地法院专属管辖"),
    (r"继承", "被继承人死亡时住所地或主要遗产所在地",
     "因继承遗产纠纷提起的诉讼，由被继承人死亡时住所地或者主要遗产所在地法院专属管辖"),
    (r"公司设立|确认股东资格|公司决议|公司解散|股东知情权|公司增资减资",
     "公司住所地", "公司组织类诉讼由公司住所地法院管辖"),
    (r"破产|重整|和解", "债务人住所地", "破产案件由债务人住所地法院管辖"),
]

# ── 特殊地域管辖：与一般地域管辖并列可选 ──
SPECIAL = [
    (r"合同|买卖|承揽|租赁|借款|服务|委托|技术|运输|保管",
     ["被告住所地", "合同履行地"], "合同纠纷由被告住所地或合同履行地法院管辖"),
    (r"保险", ["被告住所地", "保险标的物所在地"], "保险合同纠纷"),
    (r"票据", ["被告住所地", "票据支付地"], "票据纠纷"),
    (r"侵权|人身损害|名誉|隐私|产品责任|知识产权侵权|专利|商标|著作权",
     ["被告住所地", "侵权行为地（含侵权行为实施地、结果发生地）"], "侵权纠纷"),
    (r"网络侵权|信息网络", ["被告住所地", "侵权行为地（信息网络侵权的结果发生地包括被侵权人住所地）"],
     "信息网络侵权"),
    (r"劳动|劳务|工伤|竞业", ["用人单位所在地", "劳动合同履行地"],
     "劳动争议由用人单位所在地或劳动合同履行地法院管辖；**须先经劳动仲裁前置程序**"),
    (r"运输合同|铁路|公路|水上|航空", ["起运地", "到达地", "被告住所地"], "运输合同纠纷"),
    (r"海事|海商|船舶碰撞", ["碰撞发生地", "碰撞船舶最先到达地", "加害船舶被扣留地", "被告住所地"],
     "海事海商纠纷，由海事法院专门管辖"),
]

# ── 专门法院：独立于 1-4 审级序列 ──
SPECIALIZED_COURT = [
    (r"专利|技术秘密|计算机软件|集成电路布图|植物新品种|垄断",
     "知识产权法院或有管辖权的中级法院（技术类知识产权一审）"),
    (r"商标|著作权|不正当竞争", "知识产权法庭或中级法院（视标的额与地区规定）"),
    (r"证券虚假陈述|期货|信托|金融借款|保险|票据|独立保函",
     "金融法院（设立地区）或中级法院"),
    (r"海事|海商|船舶|港口|提单|海上保险", "海事法院"),
    (r"网络购物|网络服务|网络侵权|网络金融借款", "互联网法院（设立地区，须符合其受案范围）"),
    (r"铁路运输", "铁路运输法院"),
]

# ── 协议管辖：效力审查要点 ──
AGREEMENT_CONNECTIONS = ["被告住所地", "合同履行地", "合同签订地", "原告住所地", "标的物所在地"]
INVALID_PATTERNS = [
    (r"或者?向.{0,20}(仲裁|申请仲裁).{0,20}(也可|或).{0,20}(法院|起诉)"
     r"|(法院|起诉).{0,20}(也可|或者?).{0,20}仲裁",
     "或裁或审", "约定既可仲裁又可诉讼的，仲裁协议一般无效，争议解决路径不确定"),
    (r"任何一方所在地|一方所在地|守约方所在地|原告方选择",
     "连接点不确定", "约定不明确、无法确定唯一管辖法院的，协议管辖可能被认定无效"),
]


def check_exclusive(case_type, facts):
    txt = case_type + " " + facts
    hits = []
    for pat, place, note in EXCLUSIVE:
        if re.search(pat, txt):
            hits.append({"连接点": place, "规则": note})
    return hits


def check_special(case_type, facts):
    txt = case_type + " " + facts
    out = []
    for pat, places, note in SPECIAL:
        if re.search(pat, txt):
            out.append({"可选连接点": places, "规则": note})
    return out


def check_specialized(case_type, facts):
    txt = case_type + " " + facts
    return [{"法院类型": c, "触发": pat} for pat, c in SPECIALIZED_COURT
            if re.search(pat, txt)]


def check_agreement(agreement, exclusive_hits):
    if not agreement:
        return None
    r = {"约定原文": agreement, "问题": [], "有效性": "初步可用"}
    if exclusive_hits:
        r["有效性"] = "无效"
        r["问题"].append({
            "类型": "违反专属管辖", "级别": "高",
            "说明": "本案属专属管辖，**协议管辖条款无效**。"
                    "专属管辖是法定强制管辖，当事人约定不能排除"})
        return r
    for pat, name, note in INVALID_PATTERNS:
        if re.search(pat, agreement):
            r["有效性"] = "存疑"
            r["问题"].append({"类型": name, "级别": "高", "说明": note})
    hit = [c for c in AGREEMENT_CONNECTIONS
           if re.search(c.replace("住所地", "(住所地|所在地|注册地)"), agreement)]
    if not hit and not r["问题"]:
        r["问题"].append({
            "类型": "连接点未识别", "级别": "中",
            "说明": "未识别出法定连接点（被告住所地、合同履行地、合同签订地、"
                    "原告住所地、标的物所在地）。协议管辖须与争议有实际联系，"
                    "约定其他地点的可能无效"})
        r["有效性"] = "存疑"
    r["识别到的连接点"] = hit
    r["注意"] = ["协议管辖**不得违反级别管辖与专属管辖**——即便连接点合法，"
                 "约定由基层法院管辖而标的额属中院管辖范围的，该约定同样无效",
                 "格式条款中的管辖约定，未采取合理方式提示对方注意的，"
                 "对方可主张不成为合同内容"]
    return r


def level_hint(amount, case_type, facts):
    """级别管辖只给判断路径，不给具体结论——各地标准不同且会调整。"""
    out = {
        "判断依据": "级别管辖以诉讼标的额为主要标准，**各高院辖区标准不同且会调整**，"
                    "必须核对受诉法院所在地的现行规定，不能套用其他地区经验",
        "标的额": amount,
        "须另行确认": ["受诉法院所在省级辖区的级别管辖标准（含当年调整）",
                       "是否属最高法或高院指定管辖的特殊类型"],
    }
    txt = case_type + " " + facts
    if re.search(r"涉外|涉港|涉澳|涉台|外国|境外", txt):
        out["涉外提示"] = ("涉外、涉港澳台案件的级别管辖与地域管辖另有规定，"
                           "部分中院集中管辖，**须单独核对**")
    return out


def main():
    p = argparse.ArgumentParser(description="管辖权分析",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--case-type", dest="case_type", required=True, help="案由或纠纷类型")
    p.add_argument("--facts", default="", help="补充事实，用于识别专属管辖与专门法院")
    p.add_argument("--plaintiff-domicile", dest="pd", help="原告住所地")
    p.add_argument("--defendant-domicile", dest="dd", help="被告住所地")
    p.add_argument("--performance-place", dest="pp", help="合同履行地")
    p.add_argument("--signing-place", dest="sp", help="合同签订地")
    p.add_argument("--property-place", dest="prop", help="不动产/标的物所在地")
    p.add_argument("--tort-place", dest="tp", help="侵权行为地")
    p.add_argument("--agreement", help="协议管辖条款原文")
    p.add_argument("--amount", type=float, help="诉讼标的额（元）")
    args = p.parse_args()

    facts = " ".join(filter(None, [args.facts, args.case_type]))
    exclusive = check_exclusive(args.case_type, facts)
    special = check_special(args.case_type, facts)
    specialized = check_specialized(args.case_type, facts)
    agreement = check_agreement(args.agreement, exclusive)

    known = {"被告住所地": args.dd, "原告住所地": args.pd, "合同履行地": args.pp,
             "合同签订地": args.sp, "不动产/标的物所在地": args.prop,
             "侵权行为地": args.tp}
    known = {k: v for k, v in known.items() if v}

    # 管辖顺序：专属 > 协议（有效时）> 特殊地域/一般地域
    if exclusive:
        order = "**专属管辖优先**。本案存在专属管辖情形，协议管辖与一般地域管辖均被排除"
        options = [{"依据": "专属管辖", "连接点": e["连接点"], "规则": e["规则"]}
                   for e in exclusive]
    elif agreement and agreement["有效性"] == "初步可用":
        order = "协议管辖优先于法定地域管辖（前提：约定有效、未违反级别管辖与专属管辖）"
        options = [{"依据": "协议管辖", "连接点": agreement["识别到的连接点"] or ["见约定原文"],
                    "规则": "按约定确定"}]
        options += [{"依据": "法定（协议无效时的备选）", "连接点": s["可选连接点"],
                     "规则": s["规则"]} for s in special]
    else:
        order = "按法定地域管辖：一般地域管辖（被告住所地）与特殊地域管辖并存时，**原告可选择**"
        options = [{"依据": "特殊地域管辖", "连接点": s["可选连接点"], "规则": s["规则"]}
                   for s in special] or \
                  [{"依据": "一般地域管辖", "连接点": ["被告住所地"],
                    "规则": "对公民提起的诉讼由被告住所地法院管辖，"
                            "住所地与经常居住地不一致的由经常居住地法院管辖"}]

    notes = [
        "**本工具输出的是判断路径与可选连接点，不是具体某个法院。** "
        "连接点对应哪个法院，须结合当事人住所地、合同履行地的**事实认定**确定，"
        "而事实认定常有争议（如「合同履行地」在买卖合同中的认定规则）",
        "级别管辖标准**各高院辖区不同且会调整**，必须核对受诉法院所在地的现行规定",
        "选择管辖法院时，除合法性外还应考虑：审理周期、执行便利、"
        "本地保护倾向、类案裁判尺度——**后者可用「类案检索」按地域对比**",
    ]
    if specialized:
        notes.append("涉及专门法院管辖。**专门法院是独立审级（不在基层/中级/高级序列内）**，"
                     "检索类案时若限定审级会把它们整体排除")
    if re.search(r"劳动|工伤|竞业", args.case_type + facts):
        notes.append("⚠️ 劳动争议**须先经劳动仲裁**，未经仲裁直接起诉将被驳回起诉")
    if not known:
        notes.append("⚠️ 未提供任何连接点信息，无法给出可选法院范围，"
                     "仅输出适用规则")

    print(json.dumps({
        "案由": args.case_type,
        "管辖顺序": order,
        "专属管辖": exclusive or "未识别到专属管辖情形",
        "可选管辖依据": options,
        "协议管辖审查": agreement or "未提供协议管辖条款",
        "专门法院": specialized or "未识别到专门法院管辖情形",
        "级别管辖": level_hint(args.amount, args.case_type, facts),
        "已知连接点": known or "未提供",
        "提示": notes,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
