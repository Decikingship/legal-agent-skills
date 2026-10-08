#!/usr/bin/env python3
"""
不利判例四层比对

拿到一份不利判例，按**争议焦点 / 关键事实 / 法律关系 / 适用法律版本**四层，
与本案逐层比对，产出可用于庭上抗辩的区分点清单。

区分论证的成败在于**找到实质差异，而不是硬挑毛病**。
硬挑差异（"当事人名称不同""金额不同"）在庭上会被一句"这不影响法律适用"打回，
反而显得己方理屈。所以脚本只做结构化比对与差异定位，
**哪些差异构成实质区分，是法律判断，由律师认定**。

用法：
    counter_adverse.py --case-code "（2023）苏04民终2962号" --our-case our.json
    counter_adverse.py --from-search search_out.json --our-case our.json --top 5
"""

import argparse
import json
import os
import re
import sys
import urllib.request

BASE_URL = os.environ.get(
    "ZHIHE_BASE_URL",
    "https://your-legal-api.example.com").rstrip("/")
ZHIHE_TOKEN = os.environ.get("ZHIHE_TOKEN", "")
DETAIL = "https://www.zhiexa.com/fuxi/judgment-documents/detail?id={}"
EP_V2 = "/case/vector2"

# 四层比对维度，顺序即重要性。争议焦点不同则整个援引不成立，是最强的区分点。
LAYERS = [
    ("争议焦点", "焦点不同则该判例根本不构成类案，是最强区分点"),
    ("关键事实", "事实差异须指向法律评价的不同，而非单纯细节不同"),
    ("法律关系", "法律关系性质不同则请求权基础不同，适用规则随之不同"),
    ("适用法律版本", "适用旧法的判例，其规则可能已被修订取代"),
]

MILESTONE_NOTE = [
    ("20210101", "民法典施行", r"合同|违约|物权|担保|侵权|人格权|婚姻|继承"),
    ("20240701", "公司法修订施行", r"股东出资|股权转让|法人人格否认|减资|抽逃出资"),
    ("20211101", "个人信息保护法施行", r"个人信息|数据出境|人脸"),
]


def auth_headers():
    h = {"Content-Type": "application/json"}
    if ZHIHE_TOKEN:
        h["Authorization"] = ZHIHE_TOKEN if ZHIHE_TOKEN.lower().startswith("bearer ") \
            else f"Bearer {ZHIHE_TOKEN}"
    return h


def post(body, timeout=90):
    req = urllib.request.Request(
        f"{BASE_URL}{EP_V2}", data=json.dumps(body).encode("utf-8"),
        headers=auth_headers(), method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = json.loads(r.read().decode("utf-8"))
    if isinstance(raw, dict) and raw.get("detail"):
        raise RuntimeError(json.dumps(raw["detail"], ensure_ascii=False)[:200])
    return raw


def fetch_by_code(code):
    """走 v2 关键词模式——v1 按案号查拿不到 result_type。"""
    raw = post({"search_mode": "keyword", "keywords": code,
                "limit": 10, "instruct_limit": 0})
    for it in raw.get("results") or []:
        if (it.get("case_id") or "").replace(" ", "") == code.replace(" ", "") and it.get("id"):
            return it
    hits = [x for x in (raw.get("results") or []) if x.get("id")]
    return hits[0] if hits else None


def norm(t):
    return re.sub(r"[\s，。、；：\"'（）()《》【】]", "", t or "")


def sim(a, b):
    a, b = norm(a), norm(b)
    if len(a) < 4 or len(b) < 4:
        return 0.0
    ga = {a[i:i + 3] for i in range(len(a) - 2)}
    gb = {b[i:i + 3] for i in range(len(b) - 2)}
    return len(ga & gb) / min(len(ga), len(gb))


def law_version_note(item):
    jd = (item.get("judgment_date") or "").replace("-", "")
    txt = (item.get("court_opinion") or "") + (item.get("cpyz") or "")
    out = []
    for date, why, pat in MILESTONE_NOTE:
        if jd and jd < date and re.search(pat, txt[:2000]):
            out.append(f"裁判日期 {item.get('judgment_date')} 早于{why}（{date}），"
                       f"该判例适用的是修订前的法律，**规则可能已被取代**——"
                       f"这是最干净的区分点，无需纠缠事实差异")
    return out


def build_compare(item, our):
    op = item.get("court_opinion") or item.get("cpyz") or item.get("cply") or ""
    rows = []
    for layer, why in LAYERS:
        ours = (our or {}).get(layer)
        if layer == "适用法律版本":
            theirs = item.get("judgment_date")
            notes = law_version_note(item)
            rows.append({
                "维度": layer, "为何重要": why,
                "本案": ours or "〔未提供〕",
                "该判例": f"裁判日期 {theirs}",
                "自动信号": notes or ["未检出法律版本差异"],
                "待律师判断": "该判例适用的法律版本与本案是否一致",
            })
            continue
        s = round(sim(ours, op), 3) if ours else None
        rows.append({
            "维度": layer, "为何重要": why,
            "本案": ours or "〔未提供，无法比对〕",
            "该判例（取自裁判理由）": op[:500] if layer == "争议焦点" else None,
            "与裁判理由重合度": s,
            "自动信号": (
                f"重合度 {s:.0%}，表述接近，**难以从该维度区分**" if s and s >= 0.35 else
                f"重合度 {s:.0%}，存在差异，值得从该维度展开" if s is not None else
                "本案该维度未提供，无法比对"),
            "待律师判断": f"两案在「{layer}」上的差异是否影响法律评价",
        })
    return rows


def analyze(item, our):
    proc = item.get("procedure") or ""
    ref = item.get("reference_level") or ""
    rt = str(item.get("result_type") or "")
    weak = []

    # 攻击该判例本身的效力——往往比区分事实更有效
    if ("一审" in proc or "初审" in proc) and not any(
            k in proc for k in ("二审", "再审", "终审")):
        weak.append("该判例为**一审裁判**，终局性未核实。"
                    "可先用 core-audit-case-citation 反查是否被改判——"
                    "若已被改判，对方援引直接失效，不必再谈区分")
    if "基层" in (item.get("court_level") or "") and not ref:
        weak.append("该判例为**基层法院**普通裁判，参照效力有限。"
                    "可援引更高层级或指导性案例对冲")
    if ref in {"律师点评案例", "法官精析评案", "经典案例", "最高人民法院民商事裁判观点"}:
        weak.append(f"该条参照级别为「{ref}」，属**评析或编选材料，不是裁判文书**。"
                    "其中观点不得作为裁判依据——这是最直接的反驳")
    if rt in {"调解", "撤诉", "终止审理", "中止审理"}:
        weak.append(f"该案以「{rt}」结案，**未形成实体裁判观点**，不能作为类案援引")
    if rt in {"改判", "发回重审"}:
        weak.append(f"该案结果为「{rt}」。若对方引用的是被改判的下级观点，"
                    f"应指出其已被推翻；若引用的是改判理由，则须正面回应")
    return weak


def main():
    p = argparse.ArgumentParser(description="不利判例四层比对",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_argument_group("不利判例来源（二选一）")
    g.add_argument("--case-code", dest="case_code", nargs="+", help="案号，可多个")
    g.add_argument("--from-search", dest="from_search",
                   help="从 search_similar_cases.py 输出的「不利判例」桶中取")
    p.add_argument("--our-case", dest="our_case",
                   help='本案要素 JSON：{"争议焦点":"…","关键事实":"…","法律关系":"…","适用法律版本":"…"}')
    p.add_argument("--top", type=int, default=5, help="从检索结果取前 N 条")
    args = p.parse_args()

    our = {}
    if args.our_case:
        our = json.load(open(args.our_case, encoding="utf-8"))
    else:
        print("提示：未提供 --our-case，只能给出该判例自身的可攻击点，"
              "无法做四层比对", file=sys.stderr)

    items = []
    if args.case_code:
        for c in args.case_code:
            try:
                it = fetch_by_code(c)
            except Exception as e:
                print(json.dumps({"案号": c, "错误": str(e)}, ensure_ascii=False))
                continue
            if it:
                items.append(it)
            else:
                items.append({"case_id": c, "_missing": True})
    elif args.from_search:
        data = json.load(open(args.from_search, encoding="utf-8"))
        seen = set()
        for iss in data.get("焦点", []):
            for r in iss.get("不利判例", []):
                if r.get("blocking") or r.get("case_id") in seen:
                    continue
                seen.add(r.get("case_id"))
                items.append(r)
                if len(items) >= args.top:
                    break
            if len(items) >= args.top:
                break
    else:
        p.error("需提供 --case-code 或 --from-search")

    out = []
    for it in items:
        if it.get("_missing"):
            out.append({"案号": it.get("case_id"), "状态": "未核实到",
                        "说明": "案例库中未检索到。不得据此断言系编造，"
                                "但在核实前对方援引缺乏可核查的出处，可要求其提供文书原件"})
            continue
        out.append({
            "案号": it.get("case_id"),
            "cite": it.get("cite") or (DETAIL.format(it["id"]) if it.get("id") else None),
            "法院": it.get("court_name"), "审级": it.get("court_level"),
            "程序": it.get("procedure"), "裁判日期": it.get("judgment_date"),
            "参照级别": it.get("reference_level"), "结果": it.get("result_type"),
            "一、攻击该判例本身": analyze(it, our) or ["未发现明显可攻击点，须从实质区分入手"],
            "二、四层比对": build_compare(it, our),
        })

    print(json.dumps({
        "区分论证顺序": [
            "① 先攻击判例本身——一审是否被改判、是否评析材料、是否调解结案。"
            "**这类反驳成立则对方援引直接失效，成本最低**",
            "② 再攻击法律版本——适用旧法的判例规则可能已被取代，干净利落",
            "③ 最后才谈事实与焦点区分——这一层最耗时，也最容易被对方回击",
        ],
        "红线": [
            "**区分要找实质差异，不要硬挑毛病。**"
            "「当事人名称不同」「金额不同」这类在庭上会被一句「不影响法律适用」打回，"
            "反而显得己方理屈",
            "哪些差异构成实质区分是**法律判断**，本工具只做结构化比对与差异定位，不下结论",
            "若确实无法区分，如实告知律师——**硬编区分点比承认不利更危险**",
        ],
        "不利判例": out,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
