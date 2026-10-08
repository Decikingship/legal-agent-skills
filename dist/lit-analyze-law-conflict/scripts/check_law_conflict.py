#!/usr/bin/env python3
"""
法律适用冲突分析

批量取回涉案法条的效力级别、施行日期与时效状态，排成位阶—时序表，
标出三类需要人工判断的冲突信号：新旧法、上下位法、特别法与一般法。

**冲突的认定是法律判断，脚本不下结论**，只做两件确定性的事：
  1. 把散落的法条整理成可比对的结构（位阶、施行日、时效、是否有历史版本）
  2. 标出「值得看一眼」的信号——同名法多版本、同事项跨位阶、行为跨修法节点

用法：
    check_law_conflict.py --laws "中华人民共和国民法典:第五百八十五条" \\
        "最高人民法院关于适用《中华人民共和国民法典》合同编通则若干问题的解释:第六十五条" \\
        --behavior-date 20190315
    check_law_conflict.py --from-search search_out.json   # 从类案检索结果里抽法条
"""

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request

BASE_URL = os.environ.get(
    "ZHIHE_BASE_URL",
    "https://your-legal-api.example.com").rstrip("/")
ZHIHE_TOKEN = os.environ.get("ZHIHE_TOKEN", "")
DETAIL = "https://www.zhiexa.com/fuxi/legislation/detail?id={}"
EP_VECTOR = "/law/vector"
EP_VERIFY = "/law/verify"

HTML_TAG = re.compile(r"<[^>]{1,80}>")
DRAFT_RE = re.compile(r"草案|征求意见稿|送审稿|试行稿")
# 效力级别高低。数值越小位阶越高。
LEVEL_RANK = [
    (r"宪法", 0, "宪法"),
    (r"^法律|^国家法律|全国人大", 1, "法律"),
    (r"司法解释", 2, "司法解释"),
    (r"行政法规|国务院", 3, "行政法规"),
    (r"部门规章|部委", 4, "部门规章"),
    (r"地方性法规", 5, "地方性法规"),
    (r"地方政府规章", 6, "地方政府规章"),
    (r"工作文件|规范性文件|纪要|批复|答复", 7, "规范性文件/工作文件"),
]
# 引述型文件：本身可能现行有效，但其中条文是引述的旧法
QUOTING_DOC = re.compile(r"公布|典型案例|工作文件|专栏|汇编|问答|解读")

# 程序法：适用「程序从新」，按**审理时**的法律，不是行为时法。
# 教训：初版把民事诉讼法也按实体法的行为时法规则判，
# 得出「2019 年的行为应适用旧民诉法」——这会误导律师在程序问题上引旧法。
PROCEDURAL_LAW = re.compile(
    r"民事诉讼法|刑事诉讼法|行政诉讼法|仲裁法|民事诉讼证据|"
    r"关于适用《中华人民共和国民事诉讼法》|送达|管辖|执行程序")
# 时间效力衔接规定：本身就是用来解决新旧法适用的，不适用行为时法规则
TRANSITION_RULE = re.compile(r"时间效力|溯及力|新旧法|施行时间|过渡")

CITE_RE = re.compile(r"《([^》]{2,60})》\s*第\s*([一二三四五六七八九十百千零〇\d]{1,12})\s*条")


def auth_headers():
    h = {"Content-Type": "application/json"}
    if ZHIHE_TOKEN:
        h["Authorization"] = ZHIHE_TOKEN if ZHIHE_TOKEN.lower().startswith("bearer ") \
            else f"Bearer {ZHIHE_TOKEN}"
    return h


def get(path, params, timeout=60):
    url = f"{BASE_URL}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers=auth_headers())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = json.loads(r.read().decode("utf-8"))
    if str(raw.get("code", "200")) != "200":
        raise RuntimeError(f"上游错误 code={raw.get('code')} msg={raw.get('msg')}")
    return raw


def post(path, body, timeout=90):
    req = urllib.request.Request(
        f"{BASE_URL}{path}", data=json.dumps(body).encode("utf-8"),
        headers=auth_headers(), method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = json.loads(r.read().decode("utf-8"))
    if str(raw.get("code", "200")) != "200":
        raise RuntimeError(f"上游错误 code={raw.get('code')} msg={raw.get('msg')}")
    return raw


def clean(t):
    return HTML_TAG.sub("", t or "").strip()


def norm_date(d):
    """施行日期格式不统一：有 YYYYMMDD，也有「2021年01月」。归一化后才能比较。"""
    if not d:
        return None
    digits = re.sub(r"\D", "", str(d))
    if len(digits) >= 8:
        return digits[:8]
    if len(digits) == 6:
        return digits + "01"
    if len(digits) == 4:
        return digits + "0101"
    return None


def rank_of(xls, name):
    for pat, r, label in LEVEL_RANK:
        if re.search(pat, xls or "") or re.search(pat, name or ""):
            return r, label
    return 8, xls or "未知"


def fetch(law_name, article):
    """先核实法规，再取条文。"""
    raw = post(EP_VERIFY, {"queries": [law_name], "rows": 5,
                           "lawstatexlsFacet": "1", "source": "auto"})
    hits = []
    for item in raw.get("items") or []:
        for h in ((item.get("data") or {}).get("result") or []):
            if h.get("lawId"):
                h["lawName"] = clean(h.get("lawName"))
                hits.append(h)
    if not hits:
        return None
    key = re.sub(r"\s+", "", law_name.strip("《》"))
    hits.sort(key=lambda h: (
        0 if re.sub(r"\s+", "", h["lawName"]) == key else 1,
        1 if DRAFT_RE.search(h["lawName"]) else 0,
        0 if "现行有效" in (h.get("timeliness") or "") else 1))
    h = hits[0]
    rec = {
        "lawId": h["lawId"], "lawName": h["lawName"],
        "link": DETAIL.format(h["lawId"]),
        "timeliness": h.get("timeliness"), "xls": h.get("xls"),
        "publish": h.get("releaseYearMonthDate"),
        "implement": h.get("implementYearMonthDate"),
        "hisgroup": bool(h.get("hisgroup")),
        "is_draft": bool(DRAFT_RE.search(h["lawName"])),
    }
    rec["rank"], rec["level_label"] = rank_of(rec["xls"], rec["lawName"])
    if article:
        try:
            a = get(EP_VECTOR, {"lawId": h["lawId"], "rawnumber": article, "rows": 3})
            arts = [x for x in ((a.get("data") or {}).get("result") or []) if x.get("lawId")]
            rec["article"] = article
            rec["article_text"] = (arts[0].get("content") or "")[:600] if arts else None
            if not arts:
                rec["article_missing"] = True
        except Exception as e:
            rec["article_error"] = str(e)
    return rec


def detect(recs, behavior_date):
    signals = []

    # ① 引述型文件——本身有效，条文却是引述的旧法
    for r in recs:
        if QUOTING_DOC.search(r["lawName"]) and r.get("article_text"):
            signals.append({
                "类型": "引述型文件", "级别": "高",
                "涉及": r["lawName"],
                "说明": "该文件属公布/汇编/工作文件类，其本身可能现行有效，"
                        "但其中的条文往往是**引述其他法律（可能已废止）**的内容。"
                        "不得把它当作条文出处直接引用，须回溯到原法规核实"})

    # ② 草案
    for r in recs:
        if r.get("is_draft"):
            signals.append({
                "类型": "命中草案", "级别": "高", "涉及": r["lawName"],
                "说明": "草案不是生效法律。库中草案可能被标为「现行有效」，"
                        "指该草案文件存在，不代表内容有法律效力"})

    # ③ 同名法多版本 / 存在历史版本
    for r in recs:
        if r.get("hisgroup"):
            signals.append({
                "类型": "存在历史版本", "级别": "中" if behavior_date else "低",
                "涉及": r["lawName"], "现行施行日": norm_date(r.get("implement")),
                "说明": ("行为发生于 " + behavior_date + "，" if behavior_date else "") +
                        "该法规有历史版本。若行为早于现行版施行日，"
                        "应适用行为时版本，须做新旧法双轨说明"})
        imp = norm_date(r.get("implement"))
        if behavior_date and imp and behavior_date < imp:
            name = r["lawName"]
            if TRANSITION_RULE.search(name):
                continue  # 衔接规定本身就是解决新旧法适用的，不受此规则约束
            if PROCEDURAL_LAW.search(name):
                signals.append({
                    "类型": "程序法·从新适用", "级别": "低",
                    "涉及": f"{name} {r.get('article') or ''}",
                    "说明": f"该法为程序法，适用「程序从新」原则，"
                            f"按**审理时**的法律而非行为时法。"
                            f"行为发生于 {behavior_date} 早于本版施行日 {imp}，"
                            f"但程序问题仍应适用现行版本，**无需做新旧法双轨**"})
                continue
            signals.append({
                "类型": "行为早于现行版施行", "级别": "高",
                "涉及": f"{name} {r.get('article') or ''}",
                "说明": f"行为发生于 {behavior_date}，早于本版施行日 {imp}。"
                        f"实体法适用行为时版本，**直接引用现行条文是错误的**；"
                        f"须检索《XX法时间效力规定》确认有无有利溯及等例外"})

    # ④ 跨位阶——同一事项由不同位阶规范同时调整
    ranks = sorted({r["rank"] for r in recs})
    if len(ranks) > 1:
        by = {}
        for r in recs:
            by.setdefault(r["level_label"], []).append(r["lawName"])
        signals.append({
            "类型": "跨位阶并存", "级别": "中",
            "涉及": by,
            "说明": "同一事项由不同位阶的规范同时调整。适用顺序：上位法优于下位法。"
                    "**若下位法与上位法冲突，应适用上位法**；"
                    "内容一致的，引用时以上位法为主、下位法为辅"})

    # ⑤ 司法解释与法律并存——中国实务的常态，但要注意解释不得超越法律
    if any(r["rank"] == 1 for r in recs) and any(r["rank"] == 2 for r in recs):
        signals.append({
            "类型": "法律与司法解释并存", "级别": "低",
            "说明": "属常态。论证时应先引法律条文确立请求权基础，"
                    "再引司法解释明确适用标准，顺序不宜颠倒"})
    return signals


def main():
    p = argparse.ArgumentParser(description="法律适用冲突分析",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_argument_group("输入（二选一）")
    g.add_argument("--laws", nargs="+", metavar="法规名:第X条",
                   help='格式 "法规全称:第五百八十五条"，条号可省略')
    g.add_argument("--from-search", dest="from_search",
                   help="从 search_similar_cases.py 输出中抽取判决援引的法条")
    p.add_argument("--behavior-date", dest="behavior_date",
                   help="所涉行为发生日期 YYYYMMDD。不给则无法判断时间效力冲突")
    p.add_argument("--top", type=int, default=12, help="从检索结果抽法条时的上限")
    args = p.parse_args()

    pairs = []
    if args.laws:
        for x in args.laws:
            if ":" in x or "：" in x:
                n, a = re.split(r"[:：]", x, 1)
            else:
                n, a = x, None
            pairs.append((n.strip().strip("《》"), (a or "").strip() or None))
    elif args.from_search:
        data = json.load(open(args.from_search, encoding="utf-8"))
        cnt = {}
        for iss in data.get("焦点", []):
            for b in ("正向", "不利判例"):
                for r in iss.get(b, []):
                    txt = (r.get("court_opinion") or "") + (r.get("cpyz") or "")
                    for m in CITE_RE.finditer(txt):
                        k = (m.group(1).strip(), f"第{m.group(2)}条")
                        cnt[k] = cnt.get(k, 0) + 1
        pairs = [k for k, _ in sorted(cnt.items(), key=lambda x: -x[1])[:args.top]]
        if not pairs:
            sys.exit("未从检索结果中抽到法条引用")
    else:
        p.error("需提供 --laws 或 --from-search")

    recs, misses = [], []
    for name, art in pairs:
        try:
            r = fetch(name, art)
        except RuntimeError as e:
            misses.append({"法规": name, "原因": f"上游错误：{e}"})
            continue
        except Exception as e:
            misses.append({"法规": name, "原因": f"请求失败：{e}"})
            continue
        if r:
            recs.append(r)
        else:
            misses.append({"法规": name, "原因": "未核实到，不得引用，不得凭记忆补内容"})

    recs.sort(key=lambda r: (r["rank"], -(int(norm_date(r.get("implement")) or 0))))

    print(json.dumps({
        "位阶时序表": [{
            "位阶": r["level_label"], "法规": r["lawName"],
            "条号": r.get("article"), "时效": r["timeliness"],
            "施行日": r.get("implement"), "有历史版本": r["hisgroup"],
            "链接": r["link"],
            "条文": (r.get("article_text") or "")[:400],
            "条文未命中": r.get("article_missing", False),
        } for r in recs],
        "冲突信号": detect(recs, args.behavior_date),
        "未核实到": misses,
        "提示": [
            "**冲突的最终认定是法律判断，本工具只标出值得看一眼的信号**",
            "适用顺序：上位法优于下位法 → 特别法优于一般法 → 新法优于旧法",
            "同位阶且无法判断的，如实呈现冲突并标注为【争议】，不自行裁断",
            "未核实到的法规一律不得引用，**更不得凭记忆补充其内容**",
        ],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
