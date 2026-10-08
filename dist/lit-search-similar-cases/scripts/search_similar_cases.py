#!/usr/bin/env python3
"""
类案检索编排

不是「再调一次搜案」——基础 skill 已能做单次检索。本脚本做的是编排：
  把案件拆成多个争议焦点 → 每个焦点分维度 + 正反双向各检索一次
  → 合并去重 → 按参照级别与审级重排 → 算裁判尺度分布 → 逐条打质量标记

一个争议焦点两次检索，五个焦点就是十次调用。交给模型逐次发起既慢又容易漏检
（尤其是不利判例——那是最容易"忘记"检索的一路）。脚本把它变成确定性流程。

用法：
    search_similar_cases.py --plan plan.json
    search_similar_cases.py --plan plan.json --no-adverse   # 跳过不利判例（不建议）

plan.json 结构见 references/plan-schema.md
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict

BASE_URL = os.environ.get(
    "ZHIHE_BASE_URL",
    "https://your-legal-api.example.com").rstrip("/")
ZHIHE_TOKEN = os.environ.get("ZHIHE_TOKEN", "")
DETAIL_URL = "https://www.zhiexa.com/fuxi/judgment-documents/detail?id={}"
EP_V2 = "/case/vector2"


def auth_headers():
    h = {"Content-Type": "application/json"}
    if ZHIHE_TOKEN:
        h["Authorization"] = ZHIHE_TOKEN if ZHIHE_TOKEN.lower().startswith("bearer ") \
            else f"Bearer {ZHIHE_TOKEN}"
    return h


# 参照级别呈现顺序（《类案检索指导意见》四级位阶的落地）
REF_ORDER = ["最高法指导性案例", "最高检指导性案例", "公报案例", "官方典型案例",
             "入库案例", "参考案例", "上海金融法院精选案例"]
# 评析与编选材料——不是法院在个案中的裁判说理
REF_COMMENTARY = {"最高人民法院民商事裁判观点", "法官精析评案", "律师点评案例", "经典案例"}
LEVEL_ORDER = ["最高法院", "高级法院", "中级法院", "专门法院", "基层法院"]

RT_WIN = {"全部支持", "部分支持"}
RT_LOSE = {"驳回全部诉讼请求"}
RT_PROCEDURAL = {"驳回起诉", "不予受理", "不予受理/立案", "驳回自诉/不予受理"}
RT_NO_REASONING = {"调解", "撤诉", "终止审理", "中止审理"}
RT_REVERSED = {"改判", "发回重审", "提审/指令审理", "量刑改判"}

NON_JUDGMENT_DOC = {"调解书", "通知书", "令", "案例评析"}
FINAL_STAGE = ("二审", "再审", "终审", "复核")

# 修法节点：裁判日期早于此，其适用的法律已变更
# 修法节点触发词必须是**该领域专属概念**。
# 教训：初版用「公司」触发公司法节点，结果买卖合同纠纷被全部误标——
# 商事判决书里当事人几乎必然是公司。假阳性比漏检更伤信任，宁可收紧。
MILESTONES = [
    (r"股东出资|认缴出资|出资期限|股权转让|法人人格否认|人格混同|减资|"
     r"股东会决议|董事会决议|公司解散|公司清算|抽逃出资|股东资格",
     "20240701", "公司法 2024-07-01 修订施行"),
    (r"个人信息|敏感个人信息|数据出境|告知同意|人脸识别|自动化决策",
     "20211101", "个人信息保护法 2021-11-01 施行"),
    (r"证券虚假陈述|信息披露义务|重大性认定|诱多|诱空|投资者损失因果关系",
     "20200301", "证券法 2020-03-01 修订施行"),
    (r"行政处罚|从旧兼从轻|一事不再罚|首违不罚",
     "20210715", "行政处罚法 2021-07-15 修订施行"),
]
# 民法典节点特殊处理：合同、侵权类几乎全覆盖，逐条标注等于没标注。
# 改为在报告层统一提示——凡裁判日期早于 2021-01-01 的，适用的是旧法。
CIVIL_CODE_DATE = "20210101"
# 专门法院管辖领域——限定 1-4 审级会把它们整体排除
SPECIALIZED = [
    (r"知识产权|专利|商标|著作权|商业秘密", "知识产权法院"),
    (r"金融|证券|期货|信托|票据|保险", "金融法院"),
    (r"海事|海商|船舶|港口|提单", "海事法院"),
    (r"网络|电商|平台|直播|数据|个人信息", "互联网法院"),
]


def post(body, timeout=120):
    req = urllib.request.Request(
        f"{BASE_URL}{EP_V2}", data=json.dumps(body).encode("utf-8"),
        headers=auth_headers(), method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = json.loads(r.read().decode("utf-8"))
    if isinstance(raw, dict) and raw.get("detail"):
        raise RuntimeError(f"请求参数被拒：{json.dumps(raw['detail'], ensure_ascii=False)[:300]}")
    return raw


def build_body(issue, plan, adverse=False):
    focus = issue.get("adverse_focus") if adverse else issue.get("focus")
    if not focus:
        return None
    w = plan.get("weights") or {}
    body = {
        "search_mode": "mvector",
        "summary_vector": focus,
        "summary_weight": w.get("summary", 0.2),
        "focus_vector": focus,
        "focus_weight": w.get("focus", 0.6),
        "limit": plan.get("limit", 20),
        "instruct_limit": plan.get("instruct_limit", 5),
    }
    if issue.get("discovery") and not adverse:
        body["discovery_vector"] = issue["discovery"]
        body["discovery_weight"] = w.get("discovery", 0.25)
    if issue.get("claim") and not adverse:
        body["claim_vector"] = issue["claim"]
        body["claim_weight"] = w.get("claim", 0.15)
    for k in ("court_name", "court_level", "court_area",
              "judgment_date_start", "judgment_date_end"):
        if plan.get(k):
            body[k] = plan[k]
    if plan.get("result_type"):
        body["result_type"] = plan["result_type"]
    if plan.get("min_score"):
        body["minScore"] = plan["min_score"]
    return body


def rkal_filter(items, plan, trace):
    """入库案例二次筛选。

    服务端过滤器对入库案例不生效（G-6），且入库案例**不返回 judgment_date**（G-14），
    所以日期维度补不了，只能按法院名筛。这一点必须在报告中说明。
    """
    area = plan.get("court_area")
    level = plan.get("court_level")
    if not (area or level):
        return items
    kept, dropped, no_date = [], 0, 0
    for it in items:
        if it.get("source") != "rkal":
            kept.append(it)
            continue
        cname = it.get("court_name") or ""
        clevel = it.get("court_level") or ""
        if area and not any(a.rstrip("省市自治区") in cname for a in area.split()):
            dropped += 1
            continue
        if level and clevel and not any(x in clevel for x in level.split()):
            dropped += 1
            continue
        if not it.get("judgment_date"):
            no_date += 1
        kept.append(it)
    if dropped:
        trace.append(f"入库案例按法院二次筛选剔除 {dropped} 条（服务端过滤器对其不生效，G-6）")
    if no_date:
        trace.append(f"{no_date} 条入库案例无裁判日期字段（G-14），**未受日期范围限制**，须在报告中说明")
    return kept


def annotate(it, plan):
    """逐条打质量标记。返回 (flags, blocking)：blocking 为 True 表示不宜作为裁判依据。"""
    flags, blocking = [], False
    ref = it.get("reference_level") or ""
    doc = it.get("document_type") or ""
    rt = str(it.get("result_type") or "")
    proc = it.get("procedure") or ""
    jd = (it.get("judgment_date") or "").replace("-", "")

    if ref in REF_COMMENTARY:
        flags.append(f"【{ref}】评析/编选材料，非法院在个案中的裁判说理，"
                     f"不得表述为「法院认为」")
        blocking = True
    elif ref in REF_ORDER[:4]:
        flags.append(f"【{ref}】权威性高，应置于论证前列")

    if any(x in doc for x in NON_JUDGMENT_DOC):
        flags.append(f"文书类型为{doc}，非裁判文书")
        blocking = True
    if rt in RT_NO_REASONING:
        flags.append(f"裁判结果为「{rt}」，无实体裁判说理，不得用于提炼规则")
        blocking = True

    is_first = ("一审" in proc or "初审" in proc) and not any(k in proc for k in FINAL_STAGE)
    if is_first:
        flags.append("一审裁判，终局性未核实：引用前须用 core-audit-case-citation 反查是否被改判")

    probe = (it.get("court_opinion") or "") + (it.get("cpyz") or "")
    if jd and jd < CIVIL_CODE_DATE:
        flags.append(f"裁判日期 {it.get('judgment_date')} 早于民法典施行日 2021-01-01，"
                     f"适用的是合同法/物权法/侵权责任法等旧法，引用须标注")
    for pat, ms, why in MILESTONES:
        if jd and jd < ms and re.search(pat, probe[:1500]):
            flags.append(f"裁判日期 {it.get('judgment_date')} 早于{why}，适用修订前法律，"
                         f"引用须标注并确认规则未实质变化")
            break

    if "基层" in (it.get("court_level") or "") and ref not in REF_ORDER:
        flags.append("基层法院裁判，参考效力有限")
    return flags, blocking


def rank_key(it):
    ref = it.get("reference_level") or ""
    r = REF_ORDER.index(ref) if ref in REF_ORDER else (99 if ref in REF_COMMENTARY else 50)
    lvl = it.get("court_level") or ""
    l = next((i for i, x in enumerate(LEVEL_ORDER) if x in lvl), 9)
    return (r, l, -(it.get("score") or 0))


def slim(it, plan):
    flags, blocking = annotate(it, plan)
    cid = it.get("case_id") or ""
    rec = {
        "case_id": cid,
        "cite": f"[{cid}]({DETAIL_URL.format(it['id'])})" if it.get("id") else None,
        "court_name": it.get("court_name"), "court_level": it.get("court_level"),
        "procedure": it.get("procedure"), "judgment_date": it.get("judgment_date"),
        "reference_level": it.get("reference_level"), "result_type": it.get("result_type"),
        "document_type": it.get("document_type"), "source": it.get("source"),
        "score": round(it.get("score") or 0, 4),
        "flags": flags, "blocking": blocking,
    }
    mx = plan.get("max_field_chars", 1200)
    for f in ("court_opinion", "cpyz", "cply", "jbaq", "summary", "discovery"):
        v = it.get(f)
        if isinstance(v, list):
            v = "\n".join(v)
        if v:
            rec[f] = v[:mx] + ("……〔截断〕" if len(v) > mx else "")
    # complaint 是当事人主张，单独标记以防被当成法院观点
    if it.get("complaint"):
        rec["complaint_当事人主张勿作法院观点"] = it["complaint"][:400]
    return rec


def stats(items, biased=True):
    """检索样本的结果分布。维度只限法院层级、地域、年份、案由——不得涉及审判人员。

    ⚠️ 方法论要点：语义检索得到的**不是随机样本**。
    按「支持调减」的语义去找，命中的自然多是支持调减的案例。
    因此这里刻意**不叫「支持率」**——那会被读成「该类案件的总体胜诉率」，
    律师据此告诉当事人「97% 能赢」，是有实际损害的误导。
    """
    rts = [str(i.get("result_type") or "") for i in items if i.get("result_type")]
    c = Counter(rts)
    win = sum(v for k, v in c.items() if k in RT_WIN)
    lose = sum(v for k, v in c.items() if k in RT_LOSE)
    proc = sum(v for k, v in c.items() if k in RT_PROCEDURAL)
    denom = win + lose
    out = {
        "结果类型分布": dict(c.most_common()),
        "审级分布": dict(Counter(i.get("court_level") or "未知" for i in items)),
        "年份分布": dict(sorted(Counter(
            (i.get("judgment_date") or "")[:4] or "未知" for i in items).items())),
        "参照级别分布": dict(Counter(i.get("reference_level") or "普通裁判" for i in items)),
    }
    if denom:
        out["本次样本结果构成"] = {
            "口径": "(全部支持+部分支持) / (全部支持+部分支持+驳回全部诉请)",
            "分子": win, "分母": denom, "比率": f"{win / denom:.0%}",
            "程序性裁定": f"驳回起诉/不予受理 {proc} 件，已排除出分母",
            "⚠️禁止表述为支持率或胜诉率": (
                "本样本由语义检索得出，**不是随机样本**——按某一方立场的语义检索，"
                "命中的自然偏向该方。该比率只能描述「本次检索到的这批案例中结果如何」，"
                "**不得表述为该类案件的支持率、胜诉率或获赔概率**，"
                "更不得作为向当事人预测结果的依据"),
        }
        if denom < 20:
            out["本次样本结果构成"]["⚠️样本量"] = (
                f"仅 {denom} 件，样本过小，任何比率均无统计意义，只可作个案参考")
    else:
        out["本次样本结果构成"] = "无可用于实体统计的结果类型"

    reversed_n = sum(1 for i in items if str(i.get("result_type") or "") in RT_REVERSED)
    if reversed_n:
        out["含改判/发回重审"] = (
            f"{reversed_n} 件。这类案例的价值在于揭示下级裁判的错误点，"
            f"论证时应引用其改判理由，而非把被推翻的下级观点当作规则")
    return out


def check_specialized(plan, trace):
    if not plan.get("court_level") or "专门" in plan["court_level"]:
        return
    probe = " ".join(
        [plan.get("case_brief") or ""] +
        [i.get("title", "") + i.get("focus", "")[:200] for i in plan.get("issues", [])])
    for pat, court in SPECIALIZED:
        if re.search(pat, probe):
            trace.append(
                f"⚠️ 检索内容涉及{court}管辖领域，但 court_level 未含「专门法院」。"
                f"{court}属独立审级（code 9），当前条件会将其整体排除。"
                f"建议改为 \"{plan['court_level']} 专门法院\" 或不限审级")
            break


def run(plan, args):
    trace, issues_out = [], []
    check_specialized(plan, trace)

    for issue in plan.get("issues", []):
        iid = issue.get("id") or issue.get("title", "")[:8]
        for name, txt in (("focus", issue.get("focus")),
                          ("adverse_focus", issue.get("adverse_focus"))):
            if txt and len(txt) < 100:
                trace.append(f"[{iid}] {name} 仅 {len(txt)} 字，低于建议的 100 字，"
                             f"语义向量过短会显著降低召回质量")
            if txt and re.search(r"案例|问题|分析", txt):
                trace.append(f"[{iid}] {name} 含「案例/问题/分析」等元词汇，会污染语义向量")

        buckets = {}
        for label, adverse in (("正向", False), ("不利判例", True)):
            if adverse and (args.no_adverse or not issue.get("adverse_focus")):
                continue
            body = build_body(issue, plan, adverse)
            if not body:
                continue
            try:
                raw = post(body)
            except RuntimeError as e:
                trace.append(f"[{iid}/{label}] 请求被拒：{e}")
                continue
            except Exception as e:
                trace.append(f"[{iid}/{label}] 请求失败：{e}")
                continue
            items = [x for x in (raw.get("results") or []) if x.get("id")]
            trace.append(f"[{iid}/{label}] 命中 {len(items)} 条")
            items = rkal_filter(items, plan, trace)
            buckets[label] = items

        seen, merged = set(), []
        for label in ("正向", "不利判例"):
            for it in buckets.get(label, []):
                if it["id"] in seen:
                    continue
                seen.add(it["id"])
                it["_bucket"] = label
                merged.append(it)
        merged.sort(key=rank_key)

        grouped = defaultdict(list)
        for it in merged:
            grouped[it["_bucket"]].append(slim(it, plan))

        issues_out.append({
            "id": iid, "争议焦点": issue.get("title"),
            "检索词": {"正向": (issue.get("focus") or "")[:120] + "…",
                       "不利判例": (issue.get("adverse_focus") or "未检索")[:120] + "…"},
            "命中合计": len(merged),
            "可作裁判依据": sum(1 for it in merged if not annotate(it, plan)[1]),
            "正向": grouped.get("正向", []),
            "不利判例": grouped.get("不利判例", []),
            "裁判尺度统计": stats(merged),
        })

    if args.no_adverse:
        trace.insert(0, "⚠️ 已跳过不利判例检索。庭审中被对方拿出不利判例，"
                        "损失远大于报告里多两页有利案例的收益")
    trace.append("统计维度仅限法院层级、地域、年份、案由——**不得以审判人员为维度**")

    return {
        "检索方案": {
            "案件概要": (plan.get("case_brief") or "")[:200],
            "管辖地": plan.get("court_area"), "审级": plan.get("court_level"),
            "裁判日期起": plan.get("judgment_date_start"),
            "争议焦点数": len(plan.get("issues", [])),
            "权重": plan.get("weights") or {"focus": 0.6, "discovery": 0.25,
                                            "claim": 0.15, "summary": 0.2},
        },
        "检索痕迹": trace,
        "焦点": issues_out,
    }


def main():
    p = argparse.ArgumentParser(description="类案检索编排",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--plan", required=True, help="检索方案 JSON")
    p.add_argument("--no-adverse", action="store_true",
                   help="跳过不利判例检索（**不建议**）")
    args = p.parse_args()

    try:
        plan = json.load(open(args.plan, encoding="utf-8"))
    except Exception as e:
        sys.exit(f"错误：方案文件读取失败 {e}")
    if not plan.get("issues"):
        sys.exit("错误：方案中 issues 为空。类案检索必须先拆出争议焦点——"
                 "争议焦点不明的检索在 1.3 亿条库里没有意义")

    print(json.dumps(run(plan, args), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
