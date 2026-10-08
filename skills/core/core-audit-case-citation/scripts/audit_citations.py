#!/usr/bin/env python3
"""
案例援引恰当性审查

从文本中提取案号，逐件核查援引是否成立：
  案号格式 → 案件是否存在 → 案号代字与审判程序是否自洽 → 终局性（是否被改判）
  → 文书/参照级别是否可作裁判依据 → 引述观点是否确为法院认定 → 适用法律是否已修订

与 core-verify-statute-validity 的分工：那个管法条，这个管案例。
案号不同于法条——接口返回的案号必然真实，所以本脚本的重点不是「存不存在」，
而是「引得对不对」。最要命的三类：
  1. 一审已被二审改判，仍在引其观点
  2. 把「原告诉称」当成「法院认为」（当事人主张≠法院认定）
  3. 把律师点评、法官评析（reference_level 09/10/11）表述成裁判权威

用法：
    audit_citations.py --file brief.txt
    audit_citations.py --text "……（2021）最高法民终123号……"
    audit_citations.py --case-code "（2021）最高法民终123号" "（2020）京01民初456号"
"""

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime

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

DETAIL_URL = "https://www.zhiexa.com/fuxi/judgment-documents/detail?id={}"
EP_CASE = "/case/vector"     # v1：支持 case_code / caseparties，但**不返回 result_type**
EP_CASE2 = "/case/vector2"   # v2：返回 result_type，但只支持向量与关键词检索

# 实测关键结论（决定了本脚本的检索路径）：
#   result_type 只存在于 v2 的返回模型，而 v1 才支持 case_code / caseparties。
#   二者互斥 —— 若走 v1 按案号定位，拿不到 result_type，终局性核查就无从做起。
#   解法：v2 关键词模式（search_mode=keyword）走 ES 全文检索，
#   把案号或当事人姓名当关键词传入，既能精确命中，又能拿到 result_type。
#   附带好处：搜一审案号还会带出引用了该案号的二审、执行文书，正是终局性反查所需。

# 案件类型代字 → 审判程序。案号代字与返回的 procedure 不一致，
# 说明案号抄错或系编造，是很强的信号。
CODE_STAGE = [
    ("民初", "民事一审"), ("民终", "民事二审"), ("民再", "民事再审"),
    ("民申", "民事申请再审审查"), ("民监", "民事审判监督"), ("民辖终", "管辖上诉"),
    ("民特", "民事特别程序"), ("民他", "民事请示"),
    ("知民初", "民事一审"), ("知民终", "民事二审"),
    ("刑初", "刑事一审"), ("刑终", "刑事二审"), ("刑再", "刑事再审"),
    ("刑申", "刑事申诉审查"), ("刑核", "刑事复核"),
    ("行初", "行政一审"), ("行终", "行政二审"), ("行再", "行政再审"),
    ("行申", "行政申请再审审查"), ("行审", "行政非诉审查"),
    ("执异", "执行异议"), ("执复", "执行复议"), ("执恢", "恢复执行"),
    ("执保", "财产保全执行"), ("执", "执行实施"),
    ("破申", "破产申请审查"), ("破", "破产"),
    ("赔", "国家赔偿"), ("协外认", "承认与执行"),
]
FIRST_INSTANCE = {"民事一审", "刑事一审", "行政一审"}
FINAL_STAGE = ("二审", "再审", "终审", "复核")

# result_type：上诉审结论对下级裁判的意义
RT_UPHOLD = {"维持原判"}
RT_REVERSED = {"改判", "发回重审", "提审", "指令审理", "量刑改判"}
RT_NO_REASONING = {"调解", "撤诉", "终止审理", "中止审理"}

# reference_level：08-11 是评析与编选材料，不是法院在个案中的裁判说理
REF_COMMENTARY = {"最高人民法院民商事裁判观点", "法官精析评案", "律师点评案例", "经典案例"}
REF_AUTHORITATIVE = {"最高法指导性案例", "最高检指导性案例", "公报案例", "官方典型案例"}

NON_JUDGMENT_DOC = {"调解书", "通知书", "令", "案例评析"}

# 领域修法节点：裁判日期早于此，其适用的法律已变更
LAW_MILESTONES = [
    (r"公司|股东|出资|股权|减资|清算", "20240701", "公司法 2024-07-01 修订施行"),
    (r"个人信息|数据出境|隐私|人脸", "20211101", "个人信息保护法 2021-11-01 施行"),
    (r"合同|违约|物权|担保|婚姻|继承|侵权|人格权", "20210101", "民法典 2021-01-01 施行"),
    (r"证券|信息披露|虚假陈述", "20200301", "证券法 2020-03-01 修订施行"),
    (r"行政处罚|从旧兼从轻", "20210715", "行政处罚法 2021-07-15 修订施行"),
]

CASE_RE = re.compile(
    r"[（(]\s*(\d{4})\s*[）)]\s*([\u4e00-\u9fa5A-Za-z0-9]{1,14}?)(\d{1,7})\s*号")
# 引用后紧跟的观点转述
OPINION_RE = re.compile(
    r"(法院|本院|该院|二审法院|最高法院)?\s*(?:认为|指出|裁判要旨|裁判观点|认定)"
    r"[，：:]?\s*[“\"]?([^。；\n]{10,300})")


def normalize(t):
    return re.sub(r"[\s，。、；：“”\"'（）()《》]", "", t or "")


def similarity(a, b):
    a, b = normalize(a), normalize(b)
    if not a or not b:
        return 0.0
    ga = {a[i:i + 2] for i in range(len(a) - 1)} or {a}
    gb = {b[i:i + 2] for i in range(len(b) - 1)} or {b}
    return len(ga & gb) / len(ga)


def parse_stage(court_type):
    """从案件类型代字推断审判程序。长代字优先匹配（民辖终 先于 民终）。"""
    for code, stage in sorted(CODE_STAGE, key=lambda x: -len(x[0])):
        if code in court_type:
            return code, stage
    return None, None


def extract(text):
    out, seen = [], set()
    for m in CASE_RE.finditer(text):
        year, mid, seq = m.groups()
        raw = m.group(0)
        norm = f"（{year}）{mid}{seq}号"
        if norm in seen:
            continue
        seen.add(norm)
        # 观点转述只在案号紧邻其后的窗口内识别。
        # 若放宽到前文，列举多个案号时后面的案号会误捞到前一句「法院认为」，
        # 进而被判成「引述与裁判理由不符」——假阳性比漏检更伤信任。
        tail = text[m.end(): m.end() + 400]
        op = OPINION_RE.search(tail)
        if op and op.start() > 30:
            op = None
        out.append({
            "raw": raw.strip(), "case_code": norm, "year": int(year),
            "court_token": mid, "seq": seq,
            "quoted_opinion": op.group(2).strip() if op else None,
            "attributed_to_court": bool(op and op.group(1)) if op else False,
        })
    return out


def post(path, body, timeout=90):
    req = urllib.request.Request(
        f"{BASE_URL}{path}", data=json.dumps(body).encode("utf-8"),
        headers=auth_headers(), method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def lookup(case_codes):
    """按案号定位。逐个走 v2 关键词模式，以取得 result_type。

    不用 v1 的 case_code 批量查——那条路拿不到 result_type，
    而 result_type 是终局性核查的唯一抓手。
    """
    found = {}
    for cc in case_codes:
        try:
            raw = post(EP_CASE2, {"search_mode": "keyword", "keywords": cc,
                                  "limit": 10, "instruct_limit": 0})
        except Exception:
            raw = {}
        hits = raw.get("results") or []
        if not hits:
            # 兜底：v2 关键词未命中时用 v1 精确案号再试一次，
            # 至少确认案件存在（代价是没有 result_type）
            try:
                raw = post(EP_CASE, {"case_code": cc, "limit": 10, "instruct_limit": 0})
                hits = raw.get("results") or []
                for h in hits:
                    h["_no_result_type"] = True
            except Exception:
                hits = []
        for it in hits:
            cid = (it.get("case_id") or "").strip()
            if cid and it.get("id"):
                found.setdefault(normalize(cid), it)
    return found


def reverse_lookup(item):
    """反查上诉审，判断一审是否被推翻（G-2 的正解）。

    两路并用，都走 v2 关键词模式以取得 result_type：
      ① 以一审案号为关键词 —— 二审、执行文书正文会引用一审案号
      ② 以当事人姓名为关键词 —— 覆盖未引用案号的情形
    """
    queries = []
    if item.get("case_id"):
        queries.append(item["case_id"])
    parties = item.get("parties")
    if isinstance(parties, list):
        parties = " ".join(parties[:3])
    parties = (parties or "").strip()
    if parties:
        queries.append(parties[:60])
    if not queries:
        return None, "该案无案号也无当事人信息，无法反查"

    hits, errs = [], []
    for q in queries:
        try:
            raw = post(EP_CASE2, {"search_mode": "keyword", "keywords": q,
                                  "limit": 15, "instruct_limit": 0})
            hits.extend(raw.get("results") or [])
        except Exception as e:
            errs.append(str(e))
    if not hits and errs:
        return None, f"反查请求失败：{errs[0]}"
    raw = {"results": hits}
    ups = []
    for it in raw.get("results") or []:
        proc = it.get("procedure") or ""
        if any(k in proc for k in FINAL_STAGE) and it.get("id") != item.get("id"):
            ups.append(it)
    if not ups:
        return None, "未反查到同当事人的二审/再审裁判"
    return ups, None


def judge(c, item, args):
    v = {**c, "verdict": None, "severity": None, "detail": [], "id": None, "link": None,
         "court_name": None, "court_level": None, "procedure": None,
         "judgment_date": None, "document_type": None, "reference_level": None,
         "result_type": None, "opinion_similarity": None}

    code, expect_stage = parse_stage(c["court_token"])
    this_year = datetime.now().year
    if not (1980 <= c["year"] <= this_year):
        v["verdict"] = "案号格式异常"
        v["severity"] = "高"
        v["detail"].append(f"年份 {c['year']} 不合理。案号可能抄错或系编造")
        return v
    if not code:
        v["detail"].append(
            f"未能从代字「{c['court_token']}」识别案件类型，无法校验审级自洽性")

    if not item:
        v["verdict"] = "未核实到该案号"
        v["severity"] = "高"
        v["detail"].append(
            "案例库中未检索到该案号。**不得引用，不得凭记忆补充其案情或裁判观点**。"
            "注意普通案例存在数据守卫，部分早期案例不可检索，"
            "故不宜直接断言「该案不存在」，但在核实前一律不得援引")
        return v

    v.update({
        "id": item.get("id"), "link": DETAIL_URL.format(item["id"]),
        "court_name": item.get("court_name"), "court_level": item.get("court_level"),
        "procedure": item.get("procedure"), "judgment_date": item.get("judgment_date"),
        "document_type": item.get("document_type"),
        "reference_level": item.get("reference_level"),
        "result_type": item.get("result_type"),
    })

    proc = item.get("procedure") or ""
    doc = item.get("document_type") or ""
    ref = item.get("reference_level") or ""
    rt = item.get("result_type") or ""

    # 案号代字与实际审判程序是否自洽
    if expect_stage and proc and expect_stage not in proc:
        v["verdict"] = "案号与审判程序不符"
        v["severity"] = "高"
        v["detail"].append(
            f"案号代字「{code}」指向{expect_stage}，但该案实际程序为「{proc}」。"
            f"案号可能抄错，或与所述内容并非同一案件")
        return v

    # 评析材料被当成裁判
    if ref in REF_COMMENTARY:
        v["verdict"] = "评析材料非裁判文书"
        v["severity"] = "高"
        v["detail"].append(
            f"该条参照级别为「{ref}」，属评析与编选材料，"
            f"其中观点不是法院在个案中的裁判说理。"
            f"**不得表述为「法院认为」，不得作为裁判依据援引**")
        return v

    if any(x in doc for x in NON_JUDGMENT_DOC):
        v["verdict"] = f"非裁判文书·{doc}"
        v["severity"] = "高"
        v["detail"].append(f"文书类型为{doc}，无裁判说理，不得作为裁判规则依据")
        return v

    if any(x in str(rt) for x in RT_NO_REASONING):
        v["verdict"] = f"无实体裁判·{rt}"
        v["severity"] = "高"
        v["detail"].append(f"裁判结果为「{rt}」，未形成实体裁判观点，不得用于论证")
        return v

    # 观点归属：引述的「法院认为」是否其实是当事人主张
    if c["quoted_opinion"]:
        court_op = item.get("court_opinion") or item.get("cpyz") or item.get("cply") or ""
        complaint = item.get("complaint") or ""
        s_court = similarity(c["quoted_opinion"], court_op) if court_op else 0.0
        s_claim = similarity(c["quoted_opinion"], complaint) if complaint else 0.0
        v["opinion_similarity"] = {"court_opinion": round(s_court, 3),
                                   "complaint": round(s_claim, 3)}
        if s_claim > s_court and s_claim >= args.sim_threshold:
            v["verdict"] = "误将当事人主张作法院观点"
            v["severity"] = "高"
            v["detail"].append(
                f"引述内容与「原告诉称」重合度 {s_claim:.0%}，高于与「本院认为」的 "
                f"{s_court:.0%}。当事人主张不是法院认定，**表述为「法院认为」是实质性错误**")
            return v
        if s_court < args.sim_threshold:
            v["verdict"] = "引述内容与裁判理由不符"
            v["severity"] = "高"
            v["detail"].append(
                f"引述内容与该案裁判理由重合度仅 {s_court:.0%}（阈值 {args.sim_threshold:.0%}）。"
                f"可能是案号张冠李戴，或观点系杜撰。须调取原文逐字比对")
            return v
        v["detail"].append(f"引述内容与裁判理由重合度 {s_court:.0%}，初筛通过")

    # 终局性
    is_first = any(k in proc for k in ("一审", "初审")) and \
        not any(k in proc for k in FINAL_STAGE)
    if is_first:
        if args.check_terminality:
            ups, err = reverse_lookup(item)
            if err:
                v["verdict"] = "一审裁判·终局性未核实"
                v["severity"] = "中"
                v["detail"].append(f"{err}。引用一审裁判前须自行确认其未被改判")
                return v
            reversed_hits = [u for u in ups
                             if any(x in str(u.get("result_type") or "") for x in RT_REVERSED)]
            upheld = [u for u in ups
                      if any(x in str(u.get("result_type") or "") for x in RT_UPHOLD)]
            if reversed_hits:
                u = reversed_hits[0]
                v["verdict"] = "一审已被推翻"
                v["severity"] = "高"
                v["detail"].append(
                    f"反查到同当事人的上诉审 {u.get('case_id')}（{u.get('procedure')}），"
                    f"结果为「{u.get('result_type')}」。**一审观点已被推翻，禁止引用**")
                v["reversal_ref"] = {"case_id": u.get("case_id"),
                                     "link": DETAIL_URL.format(u["id"]) if u.get("id") else None,
                                     "result_type": u.get("result_type")}
                return v
            if upheld:
                v["detail"].append(
                    f"反查到上诉审 {upheld[0].get('case_id')} 结果为「维持原判」，一审生效，可引用")
            else:
                v["verdict"] = "一审裁判·终局性未核实"
                v["severity"] = "中"
                v["detail"].append(
                    "反查到同当事人的上诉审但结果类型不明确，须人工确认")
                return v
        else:
            v["verdict"] = "一审裁判·终局性未核实"
            v["severity"] = "中"
            v["detail"].append("已跳过终局性反查（--no-terminality）")
            return v

    # 适用法律是否已修订
    jd = (item.get("judgment_date") or "").replace("-", "")
    probe = " ".join(filter(None, [c.get("quoted_opinion"),
                                   item.get("court_opinion") or "", item.get("cpyz") or ""]))[:2000]
    for pat, milestone, why in LAW_MILESTONES:
        if jd and jd < milestone and re.search(pat, probe):
            v["verdict"] = "适用法律已修订"
            v["severity"] = "中"
            v["detail"].append(
                f"裁判日期 {item.get('judgment_date')} 早于 {why}，"
                f"该案适用的是修订前的法律。援引时必须标注，"
                f"且须确认修订后规则未发生实质变化，否则其参考价值有限")
            return v

    # 位阶提示
    lvl = item.get("court_level") or ""
    if ref in REF_AUTHORITATIVE:
        v["detail"].append(f"参照级别「{ref}」，权威性高")
    elif "基层" in lvl:
        v["detail"].append(
            "基层法院裁判，参考效力有限。作为主要论据时应补充更高层级的同类裁判")

    v["verdict"] = "援引成立"
    v["severity"] = "无"
    return v


def main():
    p = argparse.ArgumentParser(description="案例援引恰当性审查",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_argument_group("输入（三选一）")
    g.add_argument("--text", help="待审查文本")
    g.add_argument("--file", help="待审查文本文件路径")
    g.add_argument("--case-code", "--case_code", dest="case_code", nargs="+",
                   help="直接指定案号，可多个")

    p.add_argument("--sim-threshold", dest="sim_threshold", type=float, default=0.30,
                   help="引述内容与裁判理由的重合度阈值，默认0.30")
    p.add_argument("--no-terminality", dest="check_terminality", action="store_false",
                   help="跳过一审终局性反查（省调用，但会漏掉「已被改判」这类最严重问题）")
    p.add_argument("--raw", action="store_true", help="附带裁判理由原文")

    args = p.parse_args()

    if args.case_code:
        cites = []
        for cc in args.case_code:
            found = extract(cc)
            cites.extend(found or [{"raw": cc, "case_code": cc, "year": 0,
                                    "court_token": "", "seq": "",
                                    "quoted_opinion": None, "attributed_to_court": False}])
    else:
        text = args.text
        if args.file:
            text = open(args.file, encoding="utf-8").read()
        if not text:
            p.error("需提供 --text、--file 或 --case-code 之一")
        cites = extract(text)

    if not cites:
        print(json.dumps({"count": 0, "citations": [],
                          "note": "未从文本中识别到案号。标准格式为（YYYY）法院代字+类型代字+序号+号，"
                                  "如（2021）最高法民终123号。可改用 --case-code 直接指定"},
                         ensure_ascii=False, indent=2))
        return

    try:
        found = lookup([c["case_code"] for c in cites])
    except Exception as e:
        print(f"请求失败：{e}", file=sys.stderr)
        sys.exit(1)

    results = [judge(c, found.get(normalize(c["case_code"])), args) for c in cites]

    if not args.raw:
        for r in results:
            r.pop("court_opinion", None)

    order = {"高": 0, "中": 1, "低": 2, "无": 3}
    results.sort(key=lambda r: order.get(r["severity"], 9))
    summary = {
        "总数": len(results),
        "高危": sum(1 for r in results if r["severity"] == "高"),
        "中危": sum(1 for r in results if r["severity"] == "中"),
        "通过": sum(1 for r in results if r["severity"] == "无"),
    }
    notes = ["审查基于检索时点数据",
             "重合度仅为初筛，最终以裁判文书原文逐字比对为准",
             "本 skill 只审查援引是否成立，不判断该案例与本案是否可比——后者属类案检索范畴"]
    if summary["高危"]:
        notes.insert(0, f"⚠️ {summary['高危']} 件高危援引，修正前不得对外出具")
    if not args.check_terminality:
        notes.append("⚠️ 已跳过终局性反查，一审裁判是否被改判未核实")

    print(json.dumps({"summary": summary, "notes": notes, "citations": results},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
