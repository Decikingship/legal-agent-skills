#!/usr/bin/env python3
"""
观点—依据一致性审查

前两个校验 skill 查的是「引用真不真」：法条存不存在、案号对不对。
本脚本查的是**引用全都真实准确，但结论超出了它们能支撑的范围**——
这类问题最难发现，因为每一条依据单独看都没错。

脚本负责能确定性判断的部分：
  切分论断 → 绑定作用域内的依据 → 标记裸断言、绝对化、以偏概全、确定性缺失
射程判断（这条依据到底支不支持这个结论）无法用规则识别，
脚本把「论断—依据」成对呈现，交由模型逐对语义比对——这是本工具的主要价值：
把散落全文的论证关系整理成可逐条核对的清单。

用法：
    audit_claims.py --file opinion.md
    audit_claims.py --text "……" --strict
"""

import argparse
import json
import re
import sys

# 结论性论断的标志词。出现这些词的句子承载法律判断，必须有依据支撑。
CONCLUSION_MARK = re.compile(
    r"(因此|故|综上|由此|据此|可见|应当认定|可以认定|应认定为|构成|不构成|"
    r"有权|无权|应予支持|不予支持|不能支持|应当承担|无需承担|不承担|"
    r"有效|无效|可撤销|已过时效|未过时效|胜诉|败诉|应当赔偿|无须赔偿|"
    r"属于|不属于|违反了|未违反|可以主张|不能主张|存在.{0,6}风险|建议)")

# 绝对化表述。法律结论几乎不存在确定性，这类措辞是过度承诺。
ABSOLUTE = re.compile(
    r"(必然|必定|一定会|肯定会|绝对|毫无疑问|百分之百|100%|不可能|"
    r"必胜|稳赢|铁定|无论如何都|任何情况下都|必将)")

# 以偏概全。少量案例推出普遍规律。
OVERGENERAL = re.compile(
    r"(普遍认为|一律|均认为|都会|无一例外|所有法院|各地法院都|司法实践中都|"
    r"历来如此|从来都是|一贯认为)")

# 确定性标注（见 output-formats.md 三档）
CERTAINTY = re.compile(r"【(明确|通说|争议)】")

# 依据标记
LAW_CITE = re.compile(r"《[^》]{2,60}》(?:\s*第\s*[一二三四五六七八九十百千零〇\d]{1,12}\s*条)?")
CASE_CITE = re.compile(r"[（(]\s*\d{4}\s*[）)]\s*[\u4e00-\u9fa5A-Za-z0-9]{1,14}?\d{1,7}\s*号")
LINKED = re.compile(r"\[[^\]]+\]\(https?://[^)]+\)")
# 空泛援引：说了"根据法律规定"却不指明是哪条
VAGUE = re.compile(
    r"(根据相关法律法规|依据相关规定|按照法律规定|根据我国法律|依照有关规定|"
    r"根据司法解释|依据相关判例|参考类似案例)(?!.{0,30}《)")

# 数额、比例、期限的具体断言，须有出处
NUMERIC = re.compile(r"(\d+(?:\.\d+)?\s*%|百分之[一二三四五六七八九十]+|"
                     r"\d+\s*(?:年|个月|日|天)|\d[\d,，]{3,}\s*元)")

SENT_SPLIT = re.compile(r"(?<=[。；！？])|(?<=\n)")


def split_paragraphs(text):
    return [p for p in re.split(r"\n\s*\n", text) if p.strip()]


def split_sentences(par):
    return [s.strip() for s in SENT_SPLIT.split(par) if s and s.strip()]


def collect_authorities(text):
    return {
        "laws": sorted(set(LAW_CITE.findall(text))),
        "cases": sorted(set(CASE_CITE.findall(text))),
        "linked": len(LINKED.findall(text)),
    }


def analyze(text, args):
    claims = []
    for pi, par in enumerate(split_paragraphs(text)):
        par_auth = collect_authorities(par)
        sents = split_sentences(par)
        for si, s in enumerate(sents):
            # 结论性论断必查；此外，绝对化、以偏概全、空泛援引出现在任何句子里
            # 都是问题——它们常藏在陈述句而非结论句中，只查结论句会漏。
            is_conclusion = bool(CONCLUSION_MARK.search(s))
            has_wording_issue = bool(ABSOLUTE.search(s) or OVERGENERAL.search(s)
                                     or VAGUE.search(s))
            if not (is_conclusion or has_wording_issue):
                continue
            # 作用域：本句 + 同段前后各 window 句
            lo = max(0, si - args.window)
            hi = min(len(sents), si + args.window + 1)
            scope = "".join(sents[lo:hi])
            auth = collect_authorities(scope)

            flags, severity = [], "无"
            has_auth = bool(auth["laws"] or auth["cases"])

            if not has_auth and is_conclusion:
                if par_auth["laws"] or par_auth["cases"]:
                    flags.append({
                        "type": "依据距离过远", "level": "中",
                        "note": "本句邻近范围内无引注，依据出现在同段较远处。"
                                "论断与依据分离，读者难以核对对应关系，建议就近引注"})
                else:
                    flags.append({
                        "type": "裸断言", "level": "高",
                        "note": "该结论性论断全段无任何法条或案例支撑。"
                                "**须补充依据，或改写为附条件表述，或删除**"})

            if VAGUE.search(scope):
                flags.append({
                    "type": "空泛援引", "level": "高",
                    "note": "出现「根据相关法律法规」「依据相关规定」这类表述却未指明具体条文。"
                            "这是最典型的无依据伪装成有依据，必须落实到具体条款"})

            m = ABSOLUTE.search(s)
            if m:
                flags.append({
                    "type": "绝对化表述", "level": "高",
                    "note": f"出现「{m.group(0)}」。法律结论几乎不存在确定性，"
                            f"绝对化措辞构成过度承诺，且一旦落空责任在己方。"
                            f"改为「较大可能」「倾向于」等概率表述"})

            m = OVERGENERAL.search(s)
            if m:
                flags.append({
                    "type": "以偏概全", "level": "中",
                    "note": f"出现「{m.group(0)}」。须核对支撑样本量与地域范围；"
                            f"样本不足以支撑普遍性结论的，改为「检索到的X件案例中多数……」"})

            nums = NUMERIC.findall(s)
            if nums and not has_auth:
                flags.append({
                    "type": "数值无出处", "level": "高",
                    "note": f"出现具体数值 {nums[:3]} 但邻近无引注。"
                            f"金额、比例、期限一旦无据，最容易被对方直接击穿"})

            if args.strict and is_conclusion and not CERTAINTY.search(scope) and has_auth:
                flags.append({
                    "type": "未标注确定性", "level": "低",
                    "note": "未标注【明确】【通说】【争议】。"
                            "把争议问题写成明确问题，是律师最怕的那类错误"})

            for f in flags:
                if f["level"] == "高":
                    severity = "高"
                elif f["level"] == "中" and severity != "高":
                    severity = "中"
                elif f["level"] == "低" and severity == "无":
                    severity = "低"

            claims.append({
                "段落": pi + 1,
                "类型": "结论性论断" if is_conclusion else "措辞问题",
                "论断": s if len(s) <= 220 else s[:220] + "……",
                "作用域内依据": {"法条": auth["laws"][:6], "案例": auth["cases"][:6],
                                 "已带链接": auth["linked"]},
                "问题": flags,
                "severity": severity,
                "需语义核对": has_auth,
                "核对要求": ("逐条比对：该依据是否直接支持本论断？"
                             "是否存在扩张解释、条件遗漏、方向相反？") if has_auth else None,
            })
    return claims


def main():
    p = argparse.ArgumentParser(description="观点—依据一致性审查",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_argument_group("输入（二选一）")
    g.add_argument("--text", help="待审查文本")
    g.add_argument("--file", help="待审查文本文件路径")
    p.add_argument("--window", type=int, default=2,
                   help="依据作用域：论断句前后各N句，默认2")
    p.add_argument("--strict", action="store_true",
                   help="严格模式：额外检查确定性标注（对外正式文书建议开启）")

    args = p.parse_args()
    text = args.text
    if args.file:
        text = open(args.file, encoding="utf-8").read()
    if not text:
        p.error("需提供 --text 或 --file")

    claims = analyze(text, args)
    all_auth = collect_authorities(text)

    order = {"高": 0, "中": 1, "低": 2, "无": 3}
    claims.sort(key=lambda c: order.get(c["severity"], 9))

    naked = sum(1 for c in claims if any(f["type"] == "裸断言" for f in c["问题"]))
    n_concl = sum(1 for c in claims if c["类型"] == "结论性论断")
    summary = {
        "命中条目": len(claims),
        "其中结论性论断": n_concl,
        "高危": sum(1 for c in claims if c["severity"] == "高"),
        "中危": sum(1 for c in claims if c["severity"] == "中"),
        "无标记": sum(1 for c in claims if c["severity"] == "无"),
        "裸断言": naked,
        "全文引注": {"法条": len(all_auth["laws"]), "案例": len(all_auth["cases"]),
                     "带链接引注": all_auth["linked"]},
    }
    if n_concl:
        summary["结论引注覆盖率"] = f"{(n_concl - naked) / n_concl:.0%}"

    notes = [
        "本工具只做结构性检查。**「这条依据是否真的支持这个结论」必须逐对语义核对**，"
        "工具无法代劳",
        "标记为「需语义核对」的，须调取依据原文与论断并列比对，"
        "重点看：扩张解释、条件遗漏、方向相反",
        "未被标记不等于论证成立，只等于结构上有引注",
    ]
    if summary["高危"]:
        notes.insert(0, f"⚠️ {summary['高危']} 处高危，其中裸断言 {naked} 处，"
                        f"修正前不得对外出具")
    if all_auth["laws"] and not all_auth["linked"]:
        notes.append("⚠️ 全文引注均未带超链接。平台要求法规名与案号包装为超链接，"
                     "且无 lawId/id 的引用禁止出现")

    print(json.dumps({"summary": summary, "notes": notes, "claims": claims},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
