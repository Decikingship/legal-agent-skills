#!/usr/bin/env python3
"""
法条引用批量核验

从一段文本（代理词、法律意见、他人书状、模型输出）中提取全部法条引用，
逐条核验：法规是否存在 → 是否现行有效 → 条文号是否存在 → 转述内容是否与原文相符。

设计要点：
  - 第 4 类错误（法规对、条号对、内容是编的）最隐蔽，人工核对成本最高，
    本脚本用条文原文与引用表述的字符重合度做初筛，把可疑项挑出来交人复核。
  - 简称还原表只用于「找得到」，效力判断一律以接口返回的 timeliness 为准，
    不以本地表为准——本地表会过期，接口不会。
  - 旧法条文号与新法**不存在简单对应关系**，脚本绝不做序号换算，
    只提示需要重新语义定位。凭记忆换算条号是本领域最危险的操作之一。

用法：
    verify_citations.py --text "……《合同法》第一百一十四条……"
    verify_citations.py --file brief.txt --behavior-date 20190315
    verify_citations.py --citation "中华人民共和国民法典" "第五百八十五条"
"""

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

import os

# 服务地址与令牌走环境变量，便于内网/公网、测试/生产切换。
# 开源版默认地址为占位地址，使用前请配置 ZHIHE_BASE_URL。
# ZHIHE_TOKEN 不设置时不发 Authorization 头。
# 需要走公网网关时，设 ZHIHE_BASE_URL 与 ZHIHE_TOKEN 两个变量即可，脚本无需改动。
BASE_URL = os.environ.get(
    "ZHIHE_BASE_URL",
    "https://your-legal-api.example.com").rstrip("/")
ZHIHE_TOKEN = os.environ.get("ZHIHE_TOKEN", "")


def auth_headers():
    h = {"Content-Type": "application/json"}
    if ZHIHE_TOKEN:
        h["Authorization"] = ZHIHE_TOKEN if ZHIHE_TOKEN.lower().startswith("bearer ") \
            else f"Bearer {ZHIHE_TOKEN}"
    return h

DETAIL_URL = "https://www.zhiexa.com/fuxi/legislation/detail?id={}"
EP_VERIFY = "/law/verify"
EP_VECTOR = "/law/vector"

TIMELINESS_CURRENT = "1"

# 民法典 2021-01-01 施行时同时废止的九部法律。
# 引用这些法律名称的，除非明确针对 2021 年前的行为，否则一律是错误引用。
SUPERSEDED_BY_CIVIL_CODE = {
    "中华人民共和国合同法", "中华人民共和国物权法", "中华人民共和国担保法",
    "中华人民共和国婚姻法", "中华人民共和国继承法", "中华人民共和国收养法",
    "中华人民共和国民法通则", "中华人民共和国民法总则", "中华人民共和国侵权责任法",
}
CIVIL_CODE = "中华人民共和国民法典"
CIVIL_CODE_DATE = "20210101"

# 简称 → 全称。仅用于把检索词还原到能命中的形态，不承担效力判断。
ALIASES = {
    "民法典": CIVIL_CODE, "合同法": "中华人民共和国合同法",
    "物权法": "中华人民共和国物权法", "婚姻法": "中华人民共和国婚姻法",
    "继承法": "中华人民共和国继承法", "担保法": "中华人民共和国担保法",
    "侵权责任法": "中华人民共和国侵权责任法", "民法通则": "中华人民共和国民法通则",
    "民法总则": "中华人民共和国民法总则", "收养法": "中华人民共和国收养法",
    "公司法": "中华人民共和国公司法", "证券法": "中华人民共和国证券法",
    "劳动法": "中华人民共和国劳动法", "劳动合同法": "中华人民共和国劳动合同法",
    "刑法": "中华人民共和国刑法", "刑诉法": "中华人民共和国刑事诉讼法",
    "刑事诉讼法": "中华人民共和国刑事诉讼法",
    "民诉法": "中华人民共和国民事诉讼法", "民事诉讼法": "中华人民共和国民事诉讼法",
    "行政诉讼法": "中华人民共和国行政诉讼法",
    "行政处罚法": "中华人民共和国行政处罚法",
    "行政许可法": "中华人民共和国行政许可法",
    "专利法": "中华人民共和国专利法", "商标法": "中华人民共和国商标法",
    "著作权法": "中华人民共和国著作权法",
    "反不正当竞争法": "中华人民共和国反不正当竞争法",
    "反垄断法": "中华人民共和国反垄断法",
    "个人信息保护法": "中华人民共和国个人信息保护法",
    "个保法": "中华人民共和国个人信息保护法",
    "数据安全法": "中华人民共和国数据安全法",
    "网络安全法": "中华人民共和国网络安全法",
    "消费者权益保护法": "中华人民共和国消费者权益保护法",
    "消保法": "中华人民共和国消费者权益保护法",
    "食品安全法": "中华人民共和国食品安全法",
    "土地管理法": "中华人民共和国土地管理法",
    "城乡规划法": "中华人民共和国城乡规划法",
    "建筑法": "中华人民共和国建筑法",
    "招标投标法": "中华人民共和国招标投标法",
    "企业破产法": "中华人民共和国企业破产法",
    "破产法": "中华人民共和国企业破产法",
    "保险法": "中华人民共和国保险法",
    "票据法": "中华人民共和国票据法",
    "税收征收管理法": "中华人民共和国税收征收管理法",
    "九民纪要": "全国法院民商事审判工作会议纪要",
    "劳动争议调解仲裁法": "中华人民共和国劳动争议调解仲裁法",
}

# 实测发现：law/verify 返回的 lawName 带 ES 高亮标签（<font color="red">…</font>），
# 直接输出到文书里就是一串 HTML。
HTML_TAG = re.compile(r"<[^>]{1,80}>")
# 实测发现：verify 端点会把「XX法（修订草案）」标为「现行有效」，且可能排在正式法之前。
# 取 hits[0] 会拿到草案——这是必须在客户端拦掉的。
DRAFT_RE = re.compile(r"草案|征求意见稿|送审稿|试行稿|讨论稿")

CN_NUM = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
          "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

# 《法规名》第X条第X款第X项
CITE_RE = re.compile(
    r"《\s*([^》]{2,60}?)\s*》"
    r"(?:\s*[（(][^）)]{0,30}[）)])?"
    r"\s*(?:第\s*([一二三四五六七八九十百千零〇\d]{1,12})\s*条)?"
    r"\s*(?:第\s*([一二三四五六七八九十\d]{1,4})\s*款)?"
    r"\s*(?:第\s*([一二三四五六七八九十\d]{1,4})\s*项)?"
)
# 无书名号的常见写法：民法典第585条
BARE_RE = re.compile(
    r"(?<!《)(" + "|".join(sorted(ALIASES, key=len, reverse=True)) + r")"
    r"\s*第\s*([一二三四五六七八九十百千零〇\d]{1,12})\s*条"
)
# 引用后紧跟的转述内容："……规定，XXXX。" / "：“XXXX”"
QUOTE_RE = re.compile(r"[规定明确]{2}[，：:]?\s*[“\"]?([^。；\n]{8,200})")


def cn2int(s):
    """中文数字转整数，支持到万位；已是阿拉伯数字则直接返回。"""
    s = (s or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    total, section, number = 0, 0, 0
    units = {"十": 10, "百": 100, "千": 1000}
    for ch in s:
        if ch in CN_NUM:
            number = CN_NUM[ch]
        elif ch in units:
            section += (number or 1) * units[ch]
            number = 0
        else:
            return None
    return total + section + number


def int2cn(n):
    """整数转中文条号，用于以规范形态呈现。"""
    if n is None:
        return ""
    if n == 0:
        return "零"
    digits = "零一二三四五六七八九"
    units = ["", "十", "百", "千"]
    s, i = "", 0
    while n > 0:
        d = n % 10
        if d:
            s = digits[d] + units[i] + s
        elif s and not s.startswith("零"):
            s = "零" + s
        n //= 10
        i += 1
    s = re.sub(r"^一十", "十", s)
    return s


def normalize(text):
    return re.sub(r"[\s，。、；：“”\"'（）()《》]", "", text or "")


_REV_TAIL = re.compile(r"[(（][^（）()]{0,40}(?:修订|修正|改正)[^（）()]{0,20}[)）]\s*$")
_YEAR_TAIL = re.compile(r"[(（][0-9]{4}.{0,10}[)）]\s*$")


def norm_title(name):
    """法规标题比对键：去 HTML / 书名号 / 空白 / 结尾修订后缀。与服务端 search_law 一致。"""
    t = HTML_TAG.sub("", name or "")
    t = t.strip().strip("《》").strip()
    t = _REV_TAIL.sub("", t).strip()
    t = _YEAR_TAIL.sub("", t).strip()
    return re.sub(r"\s+", "", t)



def similarity(a, b):
    """字符二元组重合度。用于「转述内容是否确实来自该条」的初筛。"""
    a, b = normalize(a), normalize(b)
    if not a or not b:
        return 0.0
    ga = {a[i:i + 2] for i in range(len(a) - 1)} or {a}
    gb = {b[i:i + 2] for i in range(len(b) - 1)} or {b}
    return len(ga & gb) / len(ga) if ga else 0.0


def expand(name):
    n = (name or "").strip().strip("《》")
    n = re.sub(r"[（(][^）)]*[）)]\s*$", "", n).strip()
    return ALIASES.get(n, n)


def extract(text):
    """提取引用。同一法规同一条号只保留一次，但保留各自的上下文。"""
    found, seen = [], set()
    for m in CITE_RE.finditer(text):
        raw_name, art, para, item = m.groups()
        name = expand(raw_name)
        key = (name, art or "", para or "")
        if key in seen:
            continue
        seen.add(key)
        tail = text[m.end(): m.end() + 260]
        q = QUOTE_RE.search(tail)
        found.append({
            "raw": m.group(0).strip(), "law_name_raw": raw_name.strip(),
            "law_name": name, "article_cn": art, "article_no": cn2int(art),
            "paragraph": para, "item": item,
            "quoted": q.group(1).strip() if q else None,
        })
    for m in BARE_RE.finditer(text):
        alias, art = m.groups()
        name = expand(alias)
        key = (name, art, "")
        if key in seen:
            continue
        seen.add(key)
        tail = text[m.end(): m.end() + 260]
        q = QUOTE_RE.search(tail)
        found.append({
            "raw": m.group(0).strip(), "law_name_raw": alias,
            "law_name": name, "article_cn": art, "article_no": cn2int(art),
            "paragraph": None, "item": None,
            "quoted": q.group(1).strip() if q else None,
        })
    return found


def post(path, body, timeout=90):
    req = urllib.request.Request(
        f"{BASE_URL}{path}", data=json.dumps(body).encode("utf-8"),
        headers=auth_headers(), method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = json.loads(r.read().decode("utf-8"))
    if str(raw.get("code", "200")) != "200":
        raise RuntimeError(f"上游错误 code={raw.get('code')} msg={raw.get('msg')}")
    return raw


def get(path, params, timeout=60):
    url = f"{BASE_URL}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers=auth_headers())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = json.loads(r.read().decode("utf-8"))
    # law 路由吞异常返回 HTTP 200 + code=500，不显式检查会被误读为「无结果」
    if str(raw.get("code", "200")) != "200":
        raise RuntimeError(f"上游错误 code={raw.get('code')} msg={raw.get('msg')}")
    return raw


def clean_name(n):
    return HTML_TAG.sub("", n or "").strip()


def rank_hits(query, hits):
    """命中排序：标题精确对齐 > 非草案 > 现行有效。

    实测教训：verify 端点返回的第一条未必是目标法规——查「中华人民共和国公司法」
    返回的首条是「公司法（修订草案）」，且时效性标为现行有效。取 hits[0] 会拿到草案。
    """
    qk = norm_title(clean_name(query))

    def score(h):
        name = clean_name(h.get("lawName"))
        exact = norm_title(name) == qk
        draft = bool(DRAFT_RE.search(name))
        current = "现行有效" in (h.get("timeliness") or "")
        return (0 if exact else 1, 1 if draft else 0, 0 if current else 1,
                -len(h.get("implementYearMonthDate") or ""))

    return sorted(hits, key=score)


def verify_laws(names, source="auto"):
    out = {}
    for i in range(0, len(names), 50):
        chunk = names[i:i + 50]
        raw = post(EP_VERIFY, {"queries": chunk, "rows": 5,
                               "lawstatexlsFacet": TIMELINESS_CURRENT, "source": source})
        for item in raw.get("items") or []:
            q = item.get("query")
            hits = []
            for h in ((item.get("data") or {}).get("result") or []):
                if not h.get("lawId"):
                    continue
                h["lawName"] = clean_name(h.get("lawName"))
                hits.append(h)
            out[q] = rank_hits(q, hits)
    return out


def fetch_article(law_id, article_cn):
    raw = get(EP_VECTOR, {"lawId": law_id, "rawnumber": article_cn, "rows": 5})
    return [r for r in ((raw.get("data") or {}).get("result") or []) if r.get("lawId")]


def judge(c, hits, args):
    """给单条引用定性。"""
    v = {**c, "verdict": None, "severity": None, "detail": [], "lawId": None, "link": None,
         "official_name": None, "timeliness": None, "implement_date": None,
         "article_text": None, "similarity": None}

    if c["law_name"] in SUPERSEDED_BY_CIVIL_CODE:
        behavior = args.behavior_date
        if behavior and behavior < CIVIL_CODE_DATE:
            v["verdict"] = "时间效力·需双轨"
            v["severity"] = "中"
            v["detail"].append(
                f"《{c['law_name']}》已于 2021-01-01 被《{CIVIL_CODE}》取代，"
                f"但行为发生于 {behavior}，属民法典施行前，应适用行为时法。"
                f"须按《民法典时间效力规定》做新旧法双轨说明，不得单引现行法")
        else:
            v["verdict"] = "已废止·被民法典取代"
            v["severity"] = "高"
            v["detail"].append(
                f"《{c['law_name']}》已于 2021-01-01 废止，由《{CIVIL_CODE}》取代。"
                f"⚠️ 旧法条文序号与民法典**不存在简单对应关系**，"
                f"禁止凭记忆换算条号，必须以条文内容重新语义检索定位")
        return v

    if not hits:
        v["verdict"] = "未核实到"
        v["severity"] = "高"
        v["detail"].append(
            "主库与兜底源均未检索到该法规。可能是名称有误、简称未还原、"
            "或该法规并不存在。**不得引用，也不得凭记忆补充其内容**")
        return v

    h = hits[0]
    hname = h.get("lawName") or ""

    if DRAFT_RE.search(hname):
        v["verdict"] = "仅命中草案"
        v["severity"] = "高"
        v["detail"].append(
            f"命中的是《{hname}》，属草案/征求意见稿，**不是生效法律**。"
            f"（注意：草案在库中可能被标为「现行有效」，指的是该草案文件本身存在，"
            f"不代表其内容具有法律效力。）未找到同名正式法律，请核对名称")
        return v

    if norm_title(hname) != norm_title(c["law_name"]):
        v["detail"].append(
            f"⚠️ 标题未完全对齐：查询「{c['law_name']}」，命中《{hname}》。"
            f"请确认是否为所指法规——库中存在大量标题含该法规名的司法解释与工作文件")

    v.update({"lawId": h.get("lawId"), "official_name": hname,
              "timeliness": h.get("timeliness"),
              "implement_date": h.get("implementYearMonthDate"),
              "link": DETAIL_URL.format(h["lawId"])})

    tl = h.get("timeliness") or ""
    if tl and "现行有效" not in tl:
        v["verdict"] = f"效力异常·{tl}"
        v["severity"] = "高"
        v["detail"].append(f"该法规当前时效性为「{tl}」，非现行有效，不得作为现行依据引用")
        return v

    if h.get("hisgroup"):
        v["detail"].append(
            "该法规存在历史版本。若所涉行为发生在最近一次修订之前，"
            "须做行为时法与现行法双轨判断（可用 --behavior-date 触发提示）")
        if args.behavior_date and h.get("implementYearMonthDate") \
                and args.behavior_date < h["implementYearMonthDate"]:
            v["verdict"] = "时间效力·需双轨"
            v["severity"] = "中"
            v["detail"].append(
                f"行为发生于 {args.behavior_date}，早于本版施行日 "
                f"{h['implementYearMonthDate']}，应适用行为时版本")
            return v

    if not c["article_cn"]:
        v["verdict"] = "法规有效（未指明条文）"
        v["severity"] = "低"
        return v

    try:
        arts = fetch_article(h["lawId"], c["article_cn"])
    except Exception as e:
        v["verdict"] = "条文核验失败"
        v["severity"] = "中"
        v["detail"].append(f"条文检索出错：{e}。**这是接口错误，不等于该条不存在**，请重试")
        return v

    if not arts:
        v["verdict"] = "条文号不存在"
        v["severity"] = "高"
        v["detail"].append(
            f"《{h.get('lawName')}》中未检索到第{c['article_cn']}条。"
            f"该法规真实存在但条号可能引错，或条号已因修订变动")
        return v

    art = arts[0]
    v["article_text"] = art.get("content") or ""

    if c["quoted"]:
        sim = similarity(c["quoted"], v["article_text"])
        v["similarity"] = round(sim, 3)
        if sim < args.sim_threshold:
            v["verdict"] = "内容与原文不符"
            v["severity"] = "高"
            v["detail"].append(
                f"引用处的表述与该条原文重合度仅 {sim:.0%}（阈值 {args.sim_threshold:.0%}）。"
                f"可能是条号张冠李戴，或转述内容系杜撰。**须逐字比对后再采用**")
            return v
        v["detail"].append(f"转述内容与原文重合度 {sim:.0%}，初筛通过（仍建议人工核对要件表述）")

    v["verdict"] = "有效且条文匹配"
    v["severity"] = "无"
    return v


def main():
    p = argparse.ArgumentParser(description="法条引用批量核验",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_argument_group("输入（三选一）")
    src.add_argument("--text", help="待核验文本")
    src.add_argument("--file", help="待核验文本文件路径")
    src.add_argument("--citation", nargs="+", metavar="ARG",
                     help='直接指定：--citation "中华人民共和国民法典" "第五百八十五条"')

    p.add_argument("--behavior-date", dest="behavior_date",
                   help="所涉行为发生日期 YYYYMMDD。给了才能判断时间效力问题")
    p.add_argument("--sim-threshold", dest="sim_threshold", type=float, default=0.35,
                   help="转述内容与原文的重合度阈值，低于此值判为不符，默认0.35")
    p.add_argument("--source", default="auto", choices=["auto", "primary", "fallback"],
                   help="核查数据源。存疑时用 primary 与 fallback 各跑一次交叉验证")
    p.add_argument("--raw", action="store_true", help="附带条文原文全文")

    args = p.parse_args()

    if args.citation:
        name = expand(args.citation[0])
        art = args.citation[1] if len(args.citation) > 1 else None
        art = re.sub(r"^第|条$", "", art) if art else None
        cites = [{"raw": " ".join(args.citation), "law_name_raw": args.citation[0],
                  "law_name": name, "article_cn": art, "article_no": cn2int(art),
                  "paragraph": None, "item": None, "quoted": None}]
    else:
        text = args.text
        if args.file:
            text = open(args.file, encoding="utf-8").read()
        if not text:
            p.error("需提供 --text、--file 或 --citation 之一")
        cites = extract(text)

    if not cites:
        print(json.dumps({"count": 0, "citations": [],
                          "note": "未从文本中识别到法条引用。请确认引用是否使用了《》书名号，"
                                  "或改用 --citation 直接指定"},
                         ensure_ascii=False, indent=2))
        return

    names = sorted({c["law_name"] for c in cites
                    if c["law_name"] not in SUPERSEDED_BY_CIVIL_CODE})
    try:
        hitmap = verify_laws(names, args.source) if names else {}
    except RuntimeError as e:
        print(f"上游内部错误：{e}", file=sys.stderr)
        sys.exit(3)
    except Exception as e:
        print(f"请求失败：{e}", file=sys.stderr)
        sys.exit(1)

    results = [judge(c, hitmap.get(c["law_name"], []), args) for c in cites]
    if not args.raw:
        for r in results:
            if r.get("article_text") and len(r["article_text"]) > 300:
                r["article_text"] = r["article_text"][:300] + "……〔截断，用 --raw 看全文〕"

    order = {"高": 0, "中": 1, "低": 2, "无": 3}
    results.sort(key=lambda r: order.get(r["severity"], 9))
    summary = {
        "总数": len(results),
        "高危": sum(1 for r in results if r["severity"] == "高"),
        "中危": sum(1 for r in results if r["severity"] == "中"),
        "通过": sum(1 for r in results if r["severity"] == "无"),
    }
    notes = ["核验基于检索时点的数据，法规可能随时更新",
             "内容重合度仅为初筛，最终以条文原文逐字比对为准"]
    if summary["高危"]:
        notes.insert(0, f"⚠️ {summary['高危']} 条高危引用，修正前不得对外出具")

    print(json.dumps({"summary": summary, "notes": notes, "citations": results},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
