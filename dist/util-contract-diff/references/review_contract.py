# 由 build_skills.py 从 nonlit-review-contract/scripts/review_contract.py 注入，请勿直接编辑。
# 修改请改源文件后重新构建。源文件校验和：e0027ffd04eb
#!/usr/bin/env python3
"""
合同条款切分与风险信号扫描

**立场决定一切。** 同一条款站甲方、站乙方、站中立，结论可能完全相反：
  「乙方应在收到通知后3日内无条件退货，运费由乙方承担」
    甲方视角 → 有利，但"无条件"可能被认定显失公平，建议加限定以增强可执行性
    乙方视角 → 🔴 高危：期限过短、排除抗辩权、费用单方承担
    中立视角 → 权利义务不对等，有被认定格式条款无效的风险
所以本脚本的每条规则都必须声明它对哪一方不利，输出按 --stance 过滤与调级。

脚本只做确定性的事：切条款、正则扫信号、按立场调级、查缺失条款。
**条款是否真的有问题、怎么改，是法律判断，由模型结合检索结果完成。**

用法：
    review_contract.py --file contract.txt --stance 乙方 --type 买卖合同
    review_contract.py --file contract.txt --stance 中立 --list-types
"""

import argparse
import json
import re
import sys

# ── 立场 ────────────────────────────────────────────────────────────────
STANCES = ["甲方", "乙方", "中立"]
# 条款里常见的当事人称谓 → 归一化为甲/乙
PARTY_ALIAS = {
    "甲方": "甲方", "乙方": "乙方",
    "出卖人": "甲方", "买受人": "乙方", "供方": "甲方", "需方": "乙方",
    "出租人": "甲方", "承租人": "乙方", "发包人": "甲方", "承包人": "乙方",
    "委托人": "甲方", "受托人": "乙方", "许可方": "甲方", "被许可方": "乙方",
}

# ── 风险规则 ─────────────────────────────────────────────────────────────
# subject: 正则命中处的**主语**是受益方(benefit)还是受损方(burden)，"both"=效力瑕疵双方均需注意。
#   教训：初版把句中出现的当事人一律当义务方，结果「甲方…概不负责」被判成"不利于甲方"——
#   免责、有权、管辖这类条款里，主语恰恰是受益方。方向反了，整个立场适配就是错的。
# level: 基准等级（受损方视角）
RULES = [
    # 教训：初版正则写死了语序与紧邻关系，7 条只命中 3 条。
    # 合同文本的语序高度不定——「3日内提出异议」与「异议应于3日内提出」都常见，
    # 「按总价款百分之三十支付违约金」数值也可能在前。故一律写成双向或加大间隔容忍。
    dict(id="R01", subject="benefit", name="单方免责/责任排除",
         pat=r"(概不(负责|承担)|不承担(任何|一切)?责任|免除.{0,10}(全部|一切)?责任|"
             r"不负(任何)?赔偿|无需承担(任何)?(违约)?责任|不承担(任何)?赔偿责任)",
         against="counterparty", level="高",
         note="免除己方责任、加重对方责任的格式条款，可能依《民法典》格式条款规则被认定无效"),
    dict(id="R02", subject="burden", name="无条件/排除抗辩",
         pat=r"(无条件|无理由|不得(再)?(提出)?(任何|一切)(异议|抗辩|主张)|"
             r"视为(验收合格|认可|接受)|放弃(一切|所有|任何).{0,8}(权利|抗辩|追索))",
         against="counterparty", level="高",
         note="排除对方抗辩权或以拟制推定剥夺救济，既有效力风险，也在履行中易生争议"),
    dict(id="R03", subject="burden", name="违约金畸高",
         pat=r"((违约金|滞纳金)[^。；\n]{0,40}?(百分之(三十|四十|五十|六十|七十|八十|九十)|[3-9]\d\s*%)"
             r"|(百分之(三十|四十|五十|六十|七十|八十|九十)|[3-9]\d\s*%)[^。；\n]{0,40}?(违约金|滞纳金)"
             r"|(日|每日)\s*(千分之[一二三四五六七八九]|万分之[五六七八九])"
             r"|[1-9]\d?\s*%\s*/\s*(日|天))",
         against="obligor", level="高",
         note="约定过高可能被请求调减；调减以实际损失为基础，须结合举证责任分配判断"),
    dict(id="R04", subject="benefit", name="责任上限/赔偿封顶",
         pat=r"(赔偿|责任)[^。；\n]{0,24}?(总额|上限|最高|不超过)[^。；\n]{0,24}?"
             r"(合同.{0,4}(总)?(价|额|款)|已(收取|支付|付)的?(货款|款项)|已付款项)",
         against="oblige", level="中",
         note="责任上限对守约方不利；须核对是否涵盖主要风险敞口，间接损失是否被一并排除"),
    dict(id="R05", subject="burden", name="异议/索赔期过短",
         pat=r"((异议|索赔|质量问题|不符)[^。；\n]{0,20}?([一二三两1-3]|[3-7])\s*(个)?(日|天|工作日)内"
             r"|([一二三两1-3]|[3-7])\s*(个)?(日|天|工作日)内[^。；\n]{0,20}?(提出|主张)?(异议|索赔))",
         against="counterparty", level="高",
         note="过短的异议期实质剥夺救济权，可能被认定无效或不予支持；须结合标的性质判断合理性"),
    dict(id="R06", subject="benefit", name="单方变更/解除权",
         pat=r"(有权[^。；\n]{0,20}?(单方|随时|自行)?[^。；\n]{0,8}?(变更|解除|终止|调整|中止)"
             r"|(单方|随时|自行)[^。；\n]{0,8}?(变更|解除|终止|调整)[^。；\n]{0,12}?(合同|价格|条款)"
             r"|保留[^。；\n]{0,10}?(变更|解除|调整)[^。；\n]{0,6}?权)",
         against="counterparty", level="高",
         note="单方形成权条款，须核对行使条件、通知程序与相对方的救济途径"),
    dict(id="R07", subject="benefit", name="付款前置条件过多",
         pat=r"(付款|支付|结算)[^。；\n]{0,30}?(以[^。；\n]{0,20}?为(前提|条件)"
             r"|经[^。；\n]{0,16}?(书面)?(确认|同意|验收|批准)后"
             r"|待[^。；\n]{0,16}?后(再)?(支付|付款|结算))",
         against="oblige", level="中",
         note="收款方须核对前置条件是否可控；条件由对方掌握等于付款时点不确定"),
    dict(id="R08", subject="benefit", name="背靠背付款",
         pat=r"(业主|发包(方|人)|上游|最终用户|甲方的?(客户|业主))[^。；\n]{0,20}?"
             r"(支付|付款|拨款|结算)后[^。；\n]{0,20}?(再|方|才|向)",
         against="oblige", level="高",
         note="背靠背条款效力在建设工程领域存在争议，须检索本地区裁判观点"),
    dict(id="R09", subject="benefit", name="知识产权全部归属一方",
         pat=r"((全部|所有|一切)[^。；\n]{0,12}?(知识产权|著作权|专利权|技术成果|工作成果)"
             r"[^。；\n]{0,16}?(归|属于|由)[^。；\n]{0,10}?(甲方|乙方|委托人|许可方|发包人))",
         against="counterparty", level="中",
         note="须核对是否连带让渡在先知识产权与背景技术，以及是否保留必要的使用许可"),
    dict(id="R10", subject="burden", name="无限期保密/竞业",
         pat=r"((保密(义务|期限?)|竞业(限制|禁止))[^。；\n]{0,28}?"
             r"(永久|无限期|长期有效|不受(期限|时间)限制|合同终止后[^。；\n]{0,8}(仍|继续)长期)"
             r"|(永久|无限期|长期有效)[^。；\n]{0,16}?(保密(义务|期限?)|竞业(限制|禁止)))",
         against="counterparty", level="中",
         note="竞业限制超法定期限或未约定补偿的部分无效；保密期过长执行力存疑"),
    dict(id="R11", subject="benefit", name="管辖约定异常",
         pat=r"((争议|纠纷)[^。；\n]{0,30}?(向|提交|由)[^。；\n]{0,20}?"
             r"(甲方|乙方|原告|一方|本合同签订地)[^。；\n]{0,12}?(所在地|住所地)?[^。；\n]{0,8}?(法院|仲裁))",
         against="counterparty", level="中",
         note="约定己方所在地管辖对对方不利；须核对是否违反专属管辖与级别管辖规定"),
    dict(id="R12", subject="both", name="或裁或审条款",
         pat=r"((可以|得|应)[^。；\n]{0,24}?(向[^。；\n]{0,14}?法院(提起诉讼|起诉)|申请仲裁)"
             r"[^。；\n]{0,16}?(也可以|或者|或)[^。；\n]{0,10}?(申请仲裁|向[^。；\n]{0,14}?法院(提起诉讼|起诉)))",
         against="both", level="高",
         note="约定既可仲裁又可诉讼的，仲裁协议一般无效，将导致争议解决路径不确定"),
    dict(id="R13", subject="both", name="生效条件不明",
         pat=r"(自[^。；\n]{0,20}?(之日|以后|后)起?生效|经[^。；\n]{0,16}?(批准|备案|盖章|签字)后生效)",
         against="both", level="低",
         note="核对生效条件是否可能落空，落空时的处理是否有约定"),
    dict(id="R14", subject="benefit", name="单方决定验收/质量标准",
         pat=r"((验收|检验|质量)[^。；\n]{0,26}?以[^。；\n]{0,14}?(甲方|乙方|一方)"
             r"[^。；\n]{0,14}?(出具的?)?(检验报告|确认|意见|标准|结论)为准"
             r"|(验收|检验)[^。；\n]{0,16}?由[^。；\n]{0,8}?(甲方|乙方)(单方)?决定)",
         against="counterparty", level="高",
         note="把履行认定权交给对方，是最常被忽略的高危条款——争议时几无抗辩空间"),
    dict(id="R15", subject="burden", name="自动续期",
         pat=r"(未[^。；\n]{0,14}?(提出|书面)?(异议|通知|反对)[^。；\n]{0,14}?(自动|视为)(续|延|顺延)"
             r"|自动(续|延)(期|签|展)[^。；\n]{0,10}?(一年|同等期限|相同期限))",
         against="counterparty", level="中",
         note="须核对退出机制与通知期限是否合理，避免被动续约"),
    dict(id="R16", subject="both", name="定金与违约金并用",
         pat=r"(定金[^。；\n]{0,50}?违约金|违约金[^。；\n]{0,50}?定金)",
         against="both", level="中",
         note="定金与违约金不能并用，守约方只能择一主张；并列约定时须明确选择权归属"),
]

# 常见合同类型的必备条款清单（缺失检查）
REQUIRED = {
    "买卖合同": ["标的", "数量", "质量", "价款", "交付", "验收", "违约责任", "争议解决"],
    "租赁合同": ["租赁物", "租赁期限", "租金", "押金", "维修", "转租", "解除", "争议解决"],
    "建设工程": ["工程范围", "工期", "质量标准", "价款与结算", "变更", "竣工验收", "质保", "违约责任", "争议解决"],
    "服务合同": ["服务内容", "服务标准", "费用", "验收", "知识产权", "保密", "违约责任", "争议解决"],
    "劳动合同": ["工作内容", "工作地点", "劳动报酬", "工作时间", "社会保险", "劳动保护", "合同期限"],
    "股权转让": ["转让标的", "转让价款", "支付方式", "交割", "陈述与保证", "过渡期安排", "违约责任", "争议解决"],
    "借款合同": ["借款金额", "借款用途", "利率", "还款期限", "还款方式", "担保", "违约责任"],
    "保密协议": ["保密信息范围", "保密期限", "例外情形", "返还或销毁", "违约责任"],
}

CLAUSE_HEAD = re.compile(
    r"^\s*(第\s*[一二三四五六七八九十百零〇\d]{1,6}\s*[条章]"
    r"|[一二三四五六七八九十]{1,3}\s*[、.．]"
    r"|\d{1,2}(\.\d{1,2}){0,2}\s*[、.．\s]"
    r"|（[一二三四五六七八九十\d]{1,3}）|\([一二三四五六七八九十\d]{1,3}\))")


def split_clauses(text):
    lines = text.split("\n")
    clauses, cur = [], None
    for ln in lines:
        if CLAUSE_HEAD.match(ln) and len(ln.strip()) > 2:
            if cur:
                clauses.append(cur)
            cur = {"标题": ln.strip()[:60], "正文": ln.strip()}
        elif cur:
            cur["正文"] += "\n" + ln.strip()
        elif ln.strip():
            cur = {"标题": "〔前言/未编号〕", "正文": ln.strip()}
    if cur:
        clauses.append(cur)
    for i, c in enumerate(clauses, 1):
        c["序号"] = i
        c["正文"] = c["正文"].strip()
    return clauses


# 义务标记（主语被施加负担）与权利标记（主语获益）
BURDEN_MARK = r"(应当|应|须|需|不得|负有|承担(?!任何责任))"
BENEFIT_MARK = r"(有权|可以|保留.{0,6}权|概不(负责|承担)|不承担|无需承担|免除|不负)"


def infer_party(sentence):
    """找出该句的主体当事人。优先带标记的，其次取最靠前的一次提及。"""
    cands = []
    for alias, side in PARTY_ALIAS.items():
        for m in re.finditer(alias, sentence):
            tail = sentence[m.end(): m.end() + 40]
            if re.search(BENEFIT_MARK, tail) or re.search(BURDEN_MARK, tail):
                cands.append((0, m.start(), side))
            else:
                cands.append((1, m.start(), side))
    if not cands:
        return None
    cands.sort()
    return cands[0][2]


def sentence_of(text, pos):
    """取命中位置所在的句子——按整条条款判断会串到别的句子上。"""
    left = max(text.rfind("。", 0, pos), text.rfind("；", 0, pos),
               text.rfind("\n", 0, pos))
    right = min([x for x in (text.find("。", pos), text.find("；", pos),
                             text.find("\n", pos)) if x != -1] or [len(text)])
    return text[left + 1: right + 1]


def adapt(rule, sentence, stance):
    """按立场判定利害归属并调级。

    先定「谁受益、谁受损」，再看我方站在哪一边。
    """
    if rule["subject"] == "both":
        return rule["level"], "对双方均需注意（效力瑕疵）", rule["note"]

    party = infer_party(sentence)
    if not party:
        return rule["level"], "当事人不明，须人工判断利害归属", rule["note"]

    other = "乙方" if party == "甲方" else "甲方"
    if rule["subject"] == "benefit":
        winner, loser = party, other
    else:
        winner, loser = other, party

    if stance == "中立":
        return rule["level"], f"利于{winner}／不利{loser}", rule["note"]

    if stance == loser:
        return rule["level"], f"⚠️ 不利于我方（{stance}）", rule["note"]

    # 对我方有利：降级但不消音——有利条款也可能因效力瑕疵落空
    return "低", f"有利于我方（{stance}）", (
        rule["note"] + f"｜**站{stance}立场本条有利**，但仍须评估效力风险："
        f"条款若被认定无效或被调减，保护即落空，必要时主动加限定以增强可执行性")


def scan(clauses, stance):
    out = []
    for c in clauses:
        hits = []
        for r in RULES:
            m = re.search(r["pat"], c["正文"])
            if not m:
                continue
            sent = sentence_of(c["正文"], m.start())
            lvl, side, note = adapt(r, sent, stance)
            ctx = c["正文"][max(0, m.start() - 30): m.end() + 30]
            hits.append({
                "规则": r["id"], "信号": r["name"], "等级": lvl,
                "立场判定": side, "命中文本": m.group(0)[:60],
                "上下文": ctx.replace("\n", " ")[:150],
                "要点": note,
                "待确认": "本条是否确有此问题、如何修改，须结合交易背景与检索结果判断",
            })
        if hits:
            order = {"高": 0, "中": 1, "低": 2}
            hits.sort(key=lambda h: order.get(h["等级"], 9))
            out.append({"序号": c["序号"], "标题": c["标题"],
                        "正文": c["正文"][:600], "信号": hits})
    return out


def check_missing(text, ctype):
    if not ctype or ctype not in REQUIRED:
        return None
    missing = [k for k in REQUIRED[ctype] if k not in text]
    return {
        "合同类型": ctype,
        "应有条款": REQUIRED[ctype],
        "疑似缺失": missing,
        "说明": "按关键词粗查，**表述不同也可能已有约定**，须人工核对全文后再下结论。"
                "确属缺失的，按缺失条款的重要性排序补充",
    }


def main():
    p = argparse.ArgumentParser(description="合同条款切分与风险信号扫描",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--file", help="合同文本文件")
    p.add_argument("--text", help="合同文本")
    p.add_argument("--stance", choices=STANCES,
                   help="审查立场。**不给则拒绝执行**——同一条款对不同立场结论相反")
    p.add_argument("--type", dest="ctype", help=f"合同类型，用于缺失条款检查：{list(REQUIRED)}")
    p.add_argument("--list-types", action="store_true", help="列出支持缺失检查的合同类型")
    args = p.parse_args()

    if args.list_types:
        print(json.dumps(REQUIRED, ensure_ascii=False, indent=2))
        return

    if not args.stance:
        sys.exit(
            "错误：必须指定 --stance（甲方/乙方/中立）。\n"
            "同一条款站不同立场结论可能完全相反：\n"
            "  「乙方应在收到通知后3日内无条件退货」\n"
            "    甲方视角 → 有利条款，但需评估效力风险\n"
            "    乙方视角 → 高危：期限过短、排除抗辩权\n"
            "不确认立场就出审查意见，等于给了一份可能反向的意见。")

    text = args.text
    if args.file:
        text = open(args.file, encoding="utf-8").read()
    if not text:
        p.error("需提供 --file 或 --text")

    clauses = split_clauses(text)
    findings = scan(clauses, args.stance)
    cnt = {"高": 0, "中": 0, "低": 0}
    for f in findings:
        for h in f["信号"]:
            cnt[h["等级"]] = cnt.get(h["等级"], 0) + 1

    notes = [
        f"审查立场：**{args.stance}**。所有等级判定已按此立场调整——"
        f"换立场必须重跑，不得沿用",
        "本脚本只做正则信号扫描，**是否确有问题、如何修改属法律判断**，"
        "须结合交易背景、行业惯例与检索结果确定",
        "未命中信号 ≠ 条款没问题。正则覆盖不到的风险（如价格机制不合理、"
        "履行顺序倒置、交叉违约设计）须靠通读发现",
        "修改建议必须给出**可直接替换的条款文本**，只说「建议修改」没有价值",
    ]
    if args.stance == "中立":
        notes.append("中立立场：呈现条款本身的效力风险与权利义务失衡，"
                     "**不为任何一方争取利益**，也不建议单方有利的改法")

    print(json.dumps({
        "审查立场": args.stance,
        "条款总数": len(clauses),
        "命中条款数": len(findings),
        "信号统计": cnt,
        "缺失条款检查": check_missing(text, args.ctype),
        "提示": notes,
        "条款": findings,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
