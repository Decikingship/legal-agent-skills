#!/usr/bin/env python3
"""
法律文件脱敏

律师向外分享案例、做培训材料、投标时提交范例，都要脱敏。
**脱漏一处就可能构成泄露当事人隐私或违反保密义务**，这是交付类工具里唯一
出错会直接产生法律责任的一个。

设计原则：
  1. **宁可多脱不可漏脱。** 误脱一个无关信息只是可读性下降，漏脱一个身份证号是事故
  2. **一致性替换。** 同一实体在全文替换为同一代号，否则文书读不通
     （「张三」一处变「甲」另一处变「某某」，前后就对不上）
  3. **保留结构信息。** 金额、日期、法律关系不脱——脱了案例就失去价值
  4. **人工复核是必需环节。** 正则覆盖不了所有情形，脚本输出**待确认清单**

用法：
    anonymize.py --file doc.txt --entity "张三:原告" "上海某某公司:被告"
    anonymize.py --file doc.txt --auto --report
"""

import argparse
import json
import re
import sys
from collections import OrderedDict

# ── 强制脱敏项：命中即替换，不容商量 ──────────────────────────
PATTERNS = [
    ("身份证号", re.compile(r"[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])"
                           r"(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]"), "「身份证号已脱敏」"),
    ("手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "「手机号已脱敏」"),
    ("银行卡号", re.compile(r"(?<!\d)\d{16,19}(?!\d)"), "「银行账号已脱敏」"),
    ("统一社会信用代码", re.compile(r"(?<![0-9A-Z])[0-9A-HJ-NPQRTUWXY]{2}\d{6}"
                                    r"[0-9A-HJ-NPQRTUWXY]{10}(?![0-9A-Z])"),
     "「统一社会信用代码已脱敏」"),
    ("车牌号", re.compile(r"[京津冀晋蒙辽吉黑沪苏浙皖闽赣鲁豫鄂湘粤桂琼渝川贵云藏陕甘青宁新]"
                         r"[A-Z][A-Z0-9]{5}[A-Z0-9挂学警港澳]?"), "「车牌号已脱敏」"),
    ("电子邮箱", re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "「邮箱已脱敏」"),
    ("固定电话", re.compile(r"(?<!\d)0\d{2,3}-?\d{7,8}(?!\d)"), "「电话已脱敏」"),
    ("详细地址", re.compile(r"[\u4e00-\u9fa5]{2,8}(?:省|自治区|市)"
                          r"[\u4e00-\u9fa5]{2,8}(?:市|区|县)"
                          r"[\u4e00-\u9fa5\d]{2,20}(?:路|街|道|巷|号|弄|村|镇)"
                          r"[\u4e00-\u9fa5\d\-]{0,20}"), "「详细地址已脱敏」"),
]

# ── 待确认项：不自动替换，但必须提请人工核对 ──────────────────
REVIEW_HINTS = [
    ("案号", re.compile(r"[（(]\s*\d{4}\s*[）)][\u4e00-\u9fa5A-Za-z0-9]{1,14}?\d{1,7}\s*号"),
     "案号可反查到当事人。对外分享的案例通常应脱敏；引用公开裁判文书时可保留"),
    ("公司名称", re.compile(r"[\u4e00-\u9fa5（）()]{2,30}?"
                           r"(?:有限公司|股份公司|合伙企业|事务所|集团|银行|支行|分行)"),
     "公司名称须逐一确认是否脱敏——**脚本不自动替换，避免误伤法条中的机构名**"),
    ("法院名称", re.compile(r"[\u4e00-\u9fa5]{2,12}(?:人民法院|仲裁委员会|仲裁院)"),
     "法院名称通常可保留（不指向具体当事人），但会缩小可识别范围，敏感案件建议一并脱敏"),
    ("疑似人名", re.compile(r"(?:原告|被告|申请人|被申请人|第三人|上诉人|被上诉人|"
                           r"申请执行人|被执行人|甲方|乙方|丙方)[：:是为]?\s*"
                           r"([\u4e00-\u9fa5]{2,4})(?=[，,。、；：\s（(]|$)"),
     "按当事人称谓提取的疑似人名。**人名识别不可能完备**，须通读确认"),
    ("金额", re.compile(r"\d[\d,，]*(?:\.\d+)?\s*(?:万?元|万)"),
     "金额通常**保留**——脱了案例就失去参考价值。但特殊案件中金额本身可能指向特定主体"),
]

# 不应被误脱的法定机构与常见词（地址正则易误伤）
WHITELIST = re.compile(r"(最高人民法院|人民检察院|国务院|全国人民代表大会|"
                       r"市场监督管理总局|人力资源和社会保障|住房和城乡建设)")


def anonymize(text, entities, do_auto):
    """entities: {原文: 代号}，一致性替换。"""
    hits = OrderedDict()
    out = text

    # ① 指定实体优先，长的先替换，避免「上海某某科技有限公司」被「上海某某」截断
    for src in sorted(entities, key=len, reverse=True):
        n = out.count(src)
        if n:
            out = out.replace(src, f"「{entities[src]}」")
            hits.setdefault("指定实体", []).append({"原文": src, "代号": entities[src], "次数": n})

    # ② 强制脱敏项
    if do_auto:
        for name, pat, repl in PATTERNS:
            found = []
            def _sub(m):
                if WHITELIST.search(m.group(0)):
                    return m.group(0)
                found.append(m.group(0))
                return repl
            out = pat.sub(_sub, out)
            if found:
                hits.setdefault("强制脱敏", []).append(
                    {"类型": name, "次数": len(found),
                     "样例": [f"{x[:4]}***" for x in found[:3]]})
    return out, hits


def collect_review(text, entities):
    """待确认项：不替换，只列出供人工判断。"""
    out = []
    for name, pat, note in REVIEW_HINTS:
        vals = []
        for m in pat.finditer(text):
            v = m.group(1) if m.groups() else m.group(0)
            v = v.strip()
            if not v or WHITELIST.search(v):
                continue
            if any(v in k or k in v for k in entities):
                continue          # 已在指定实体中处理
            if v not in vals:
                vals.append(v)
        if vals:
            out.append({"类型": name, "数量": len(vals),
                        "值": vals[:20], "说明": note,
                        "已截断": len(vals) > 20})
    return out


def main():
    p = argparse.ArgumentParser(description="法律文件脱敏",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_argument_group("输入")
    g.add_argument("--file", help="待脱敏文件")
    g.add_argument("--text", help="待脱敏文本")
    p.add_argument("--entity", nargs="*", default=[],
                   help='指定实体替换，格式 "原文:代号"，如 "张三:原告" "某某公司:被告"')
    p.add_argument("--auto", action="store_true",
                   help="启用强制脱敏项（身份证、手机、银行卡、地址等）。**建议始终开启**")
    p.add_argument("--report", action="store_true", help="只输出核查报告，不输出脱敏正文")
    p.add_argument("--out", help="脱敏结果输出文件")
    args = p.parse_args()

    text = args.text
    if args.file:
        text = open(args.file, encoding="utf-8").read()
    if not text:
        p.error("需提供 --file 或 --text")

    entities = {}
    for e in args.entity:
        if ":" not in e and "：" not in e:
            sys.exit(f"错误：实体格式应为 「原文:代号」，收到「{e}」")
        src, code = re.split(r"[:：]", e, 1)
        entities[src.strip()] = code.strip()

    result, hits = anonymize(text, entities, args.auto)
    review = collect_review(result, entities)

    notes = [
        "**宁可多脱不可漏脱。** 误脱一个无关信息只是可读性下降，"
        "漏脱一个身份证号是事故",
        "**待确认清单必须人工过一遍。** 正则覆盖不了所有情形——"
        "人名、别称、绰号、特定事件描述都可能反向识别到当事人",
        "**一致性替换**：同一实体全文用同一代号，否则文书读不通",
        "金额、日期、法律关系**默认保留**——脱了案例就失去参考价值；"
        "但特殊案件中这些也可能指向特定主体，须自行判断",
        "⚠️ 脱敏后必须**通读全文**再对外发布。"
        "脚本处理的是模式化信息，语境中的可识别线索（「本市唯一一家XX企业」）只能靠人读出来",
    ]
    if not args.auto:
        notes.insert(0, "⚠️ **未启用 --auto**，身份证、手机号、银行卡等强制脱敏项未处理")
    if not entities:
        notes.insert(0, "⚠️ 未指定实体替换。当事人名称须用 --entity 逐一指定，"
                        "**脚本不自动替换人名与公司名**——自动替换易误伤法条与机构名")

    out = {"脱敏统计": hits or "无命中",
           "⚠️待人工确认": review or "无",
           "提示": notes}
    if not args.report:
        out["脱敏结果"] = result
    if args.out:
        open(args.out, "w", encoding="utf-8").write(result)
        out["已写入"] = args.out
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
