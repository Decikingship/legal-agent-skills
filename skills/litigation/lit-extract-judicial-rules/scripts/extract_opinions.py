#!/usr/bin/env python3
"""
裁判观点提取与聚类

吃 search_similar_cases.py 的输出，把散在几十份判决里的「本院认为」聚成若干组，
每组对应一条候选裁判规则，附支持案例数与位阶构成。

规则归纳本身是语义工作，由模型完成。本脚本做三件确定性的事：
  1. 只从 court_opinion / cpyz / cply 取材，**绝不碰 complaint**（那是当事人主张）
  2. 剔除 blocking 条目（评析材料、调解书、无实体裁判）
  3. 按表述相似度聚类，把 39 条观点收敛成 5–8 组，模型不必逐条读

用法：
    extract_opinions.py --input search_out.json
    extract_opinions.py --input search_out.json --threshold 0.28 --min-cluster 2
"""

import argparse
import json
import re
import sys
from collections import Counter

# 取材白名单。complaint 是原告诉称，是当事人主张，**永不进入观点语料**。
OPINION_FIELDS = ["cpyz", "court_opinion", "cply"]
FIELD_LABEL = {"cpyz": "裁判要旨", "court_opinion": "本院认为", "cply": "裁判理由"}

REF_ORDER = ["最高法指导性案例", "最高检指导性案例", "公报案例", "官方典型案例",
             "入库案例", "参考案例", "上海金融法院精选案例"]
LEVEL_ORDER = ["最高法院", "高级法院", "中级法院", "专门法院", "基层法院"]

# 程序性与合同效力认定套话。
# 教训：初版只剔了程序套话，结果 21 条观点聚出 13 组——因为几乎每份判决的
# "本院认为"都以「系真实意思表示，合法有效，应按约履行」开头，
# 这段占了前三分之一，把真正的争点论述稀释掉了。
BOILERPLATE = re.compile(
    r"(本院(经审查|经审理)?认为[，,：:]?|依照《[^》]{2,40}》[^。]{0,40}之?规定|"
    r"综上所述|据此|判决如下|上诉人[的之]?上诉(请求)?(理由)?(不能成立|无事实和法律依据)|"
    r"一审判决(认定事实清楚|适用法律正确)[，,]?(应予维持)?|"
    r"本案(的)?(主要)?争议焦点(为|是|在于)?|经审理查明|案件受理费[^。]{0,40}元|"
    r"(依法成立的)?合同[^。]{0,10}(受法律保护|自成立时生效)|"
    r"[^。]{0,30}(系|是)(双方)?当事人(的)?真实意思表示[^。]{0,60}(合法有效|应为合法有效)|"
    r"(内容)?不违反法律[、及]?行政法规的强制性规定|"
    r"双方(均)?应(按|依)约(定)?履行|对双方具有法律约束力|"
    r"再审申请(不能成立|理由如下)|理由是[：:])")

# 疑问词与虚词，从争议焦点标题提取关键词时剔除
STOPWORD = re.compile(
    r"(是否|如何|应否|能否|可否|什么|哪些|为何|由谁|以何为|的|了|与|和|及|或|"
    r"问题|认定|判断|确定|争议|焦点|本案)")


def normalize(t):
    return re.sub(u"[\\s\u3000\uff0c\u3002\u3001\uff1b\uff1a\u201c\u201d\"'\uff08\uff09()\u300a\u300b\u3010\u3011\\d]", "", t or "")


def sim(a, b):
    a, b = normalize(a), normalize(b)
    if len(a) < 4 or len(b) < 4:
        return 0.0
    ga = {a[i:i + 3] for i in range(len(a) - 2)}
    gb = {b[i:i + 3] for i in range(len(b) - 2)}
    inter = len(ga & gb)
    return inter / min(len(ga), len(gb)) if min(len(ga), len(gb)) else 0.0


def focus_terms(title):
    """从争议焦点标题抽出用于定位的关键词。

    「约定违约金是否过分高于实际损失、应否调减」
      → ['约定违约金', '过分高于实际损失', '调减']
    """
    if not title:
        return []
    parts = [x.strip() for x in re.split(r"[、，,；;/\s]+", STOPWORD.sub(" ", title))
             if len(x.strip()) >= 2]
    # 中文复合词中心语在末尾：「约定违约金」在判决里通常写作「违约金」，
    # 「过分高于实际损失」写作「损失」。补尾词回退，否则关键词命中率很低。
    out = list(parts)
    for t in parts:
        if len(t) >= 4:
            out.extend([t[-3:], t[-2:]])
    seen, uniq = set(), []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def focus_sentences(text, terms, cap=600):
    """只保留含焦点关键词的句子作为聚类依据。

    不含关键词的段落（合同效力认定、程序交代）会把相似度稀释，
    使真正论述同一争点的判决聚不到一起。
    """
    if not terms:
        return text[:cap]
    sents = [x for x in re.split(r"(?<=[。；！？])", text) if x.strip()]
    hit = [s_ for s_ in sents if any(t in s_ for t in terms)]
    return ("".join(hit) or text)[:cap]


def pick_text(rec, max_chars):
    """按 裁判要旨 > 本院认为 > 裁判理由 取材。"""
    for f in OPINION_FIELDS:
        v = rec.get(f)
        if v and len(normalize(v)) >= 20:
            clean = BOILERPLATE.sub("", v).strip()
            return FIELD_LABEL[f], (clean or v)[:max_chars]
    return None, None


def rank_key(rec):
    ref = rec.get("reference_level") or ""
    r = REF_ORDER.index(ref) if ref in REF_ORDER else 50
    lvl = rec.get("court_level") or ""
    l = next((i for i, x in enumerate(LEVEL_ORDER) if x in lvl), 9)
    return (r, l)


def cluster(items, threshold, min_cluster):
    """贪心聚类：按位阶从高到低取种子，相似的归入同组。

    高位阶案例先成种子——指导性案例的表述应当成为该组的代表，
    而不是被一堆基层判决的表述淹没。
    """
    items = sorted(items, key=rank_key)
    clusters, used = [], set()
    for i, a in enumerate(items):
        if i in used:
            continue
        group = [a]
        used.add(i)
        for j in range(i + 1, len(items)):
            if j in used:
                continue
            if sim(a["_key"], items[j]["_key"]) >= threshold:
                group.append(items[j])
                used.add(j)
        clusters.append(group)
    clusters.sort(key=lambda g: (-len(g), rank_key(g[0])))
    return clusters


def summarize(group):
    refs = Counter(r.get("reference_level") or "普通裁判" for r in group)
    lvls = Counter(r.get("court_level") or "未知" for r in group)
    rts = Counter(str(r.get("result_type") or "未标注") for r in group)
    top = group[0]
    return {
        "支持案例数": len(group),
        "位阶构成": dict(refs),
        "审级构成": dict(lvls),
        "结果构成": dict(rts),
        "代表案例": {
            "cite": top.get("cite"), "case_id": top.get("case_id"),
            "法院": top.get("court_name"), "审级": top.get("court_level"),
            "参照级别": top.get("reference_level"), "裁判日期": top.get("judgment_date"),
            "取材字段": top["_src"], "观点": top["_text"],
            "焦点相关段落": top.get("_key"),
        },
        "其余案例": [
            {"cite": r.get("cite"), "审级": r.get("court_level"),
             "参照级别": r.get("reference_level"), "取材字段": r["_src"],
             "观点": r["_text"][:300]}
            for r in group[1:]
        ],
    }


def main():
    p = argparse.ArgumentParser(description="裁判观点提取与聚类")
    p.add_argument("--input", required=True, help="search_similar_cases.py 的输出 JSON")
    p.add_argument("--threshold", type=float, default=0.25,
                   help="聚类相似度阈值，默认0.30。调高则组更细，调低则更粗")
    p.add_argument("--min-cluster", dest="min_cluster", type=int, default=1,
                   help="低于该支持数的组标记为孤例")
    p.add_argument("--max-chars", dest="max_chars", type=int, default=700)
    p.add_argument("--keywords", help="手工指定焦点关键词（空格分隔），覆盖自动抽取")
    p.add_argument("--include-adverse", dest="include_adverse", action="store_true",
                   help="把不利判例一并纳入聚类（默认分开处理）")
    args = p.parse_args()

    try:
        data = json.load(open(args.input, encoding="utf-8"))
    except Exception as e:
        sys.exit(f"错误：输入文件读取失败 {e}")

    out_issues, notes = [], []
    complaint_seen = 0

    for iss in data.get("焦点", []):
        terms = args.keywords.split() if args.keywords else focus_terms(iss.get("争议焦点"))
        buckets = ["正向"] + (["不利判例"] if args.include_adverse else [])
        pool, dropped_block, no_text = [], 0, 0
        for b in buckets:
            for rec in iss.get(b, []):
                if rec.get("blocking"):
                    dropped_block += 1
                    continue
                if any(k.startswith("complaint") for k in rec):
                    complaint_seen += 1
                src, txt = pick_text(rec, args.max_chars)
                if not txt:
                    no_text += 1
                    continue
                r = dict(rec)
                r["_src"], r["_text"], r["_bucket"] = src, txt, b
                # 聚类依据只取含焦点关键词的句子，全文另存供阅读
                r["_key"] = focus_sentences(txt, terms)
                pool.append(r)

        groups = cluster(pool, args.threshold, args.min_cluster)
        rules = []
        for g in groups:
            s = summarize(g)
            s["_孤例"] = len(g) < max(args.min_cluster, 2)
            rules.append(s)

        adverse_pool = []
        if not args.include_adverse:
            for rec in iss.get("不利判例", []):
                if rec.get("blocking"):
                    continue
                src, txt = pick_text(rec, args.max_chars)
                if txt:
                    adverse_pool.append({
                        "cite": rec.get("cite"), "审级": rec.get("court_level"),
                        "参照级别": rec.get("reference_level"),
                        "结果": rec.get("result_type"),
                        "取材字段": src, "观点": txt[:500]})

        out_issues.append({
            "id": iss.get("id"), "争议焦点": iss.get("争议焦点"),
            "焦点关键词": terms,
            "入池观点数": len(pool),
            "剔除": {"不可作依据": dropped_block, "无可用观点文本": no_text},
            "候选裁判规则": rules,
            "不利判例观点": adverse_pool,
        })
        if dropped_block:
            notes.append(f"[{iss.get('id')}] 剔除 {dropped_block} 条不可作裁判依据的条目"
                         f"（评析材料/非裁判文书/无实体裁判）")

    notes.insert(0, "取材仅限 裁判要旨 / 本院认为 / 裁判理由。"
                    "**complaint（原告诉称）是当事人主张，已排除在语料之外**")
    if complaint_seen:
        notes.append(f"检索结果中 {complaint_seen} 条含 complaint 字段，"
                     f"已确认未进入观点语料——写作时同样不得引作法院观点")
    notes.append("聚类只是把表述相近的观点归拢，**规则归纳仍须逐组阅读后自行提炼**；"
                 "组内表述相似不等于裁判规则相同，须核对适用条件与事实前提")
    notes.append("标记为「孤例」的规则支持案例过少，不得表述为通行规则，"
                 "应标注【争议】或作为参考观点呈现")

    print(json.dumps({
        "聚类参数": {"阈值": args.threshold, "含不利判例": args.include_adverse},
        "提示": notes, "焦点": out_issues,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
