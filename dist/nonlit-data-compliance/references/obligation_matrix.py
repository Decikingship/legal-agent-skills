# 由 build_skills.py 从 nonlit-compliance-obligation-matrix/scripts/obligation_matrix.py 注入，请勿直接编辑。
# 修改请改源文件后重新构建。源文件校验和：b635b9b1cd9f
#!/usr/bin/env python3
"""
合规义务矩阵

500 万法规库最有价值的用法：输入行业 + 业务动作，输出**可落地的义务矩阵**。

与「查一下有什么规定」的区别在于组织方式：
  法条是按立法逻辑排列的，合规义务是按**业务动作**发生的。
  企业要的不是「食品安全法有哪些条」，而是「我要上线一个外卖平台，
  在准入、经营、下架、留存四个动作上分别要做什么」。
所以本脚本按业务动作多路检索，再按位阶重组，最后配上罚则。

三个实测决定的设计：
  1. **不用行业分类过滤** —— topicsFacet 实测任何值均返回 0 条，
     行业维度改由业务动作的语义表述承载
  2. **剔除个案答复类文件** —— 复函、批复、请示答复不是普遍适用的义务来源，
     把「劳动部办公厅关于XX问题的复函」写进合规清单是硬伤
  3. **同名法规新旧版本去重** —— 库中并存「XX办法」与「XX办法（2025修订）」

用法：
    obligation_matrix.py --plan plan.json
    obligation_matrix.py --plan plan.json --no-penalty   # 跳过罚则检索
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
EP = "/law/vector"

# 效力位阶展示顺序。实测返回的是细分级别名，不是 001/002 这类码。
# 顺序即优先级，长匹配必须在前。
# 教训：初版把法律部门分类「行政法」排在「行政法规」之前，
# 结果《食品安全法实施条例》被标成"法律"——正则匹配上了「行政法规」的前三字。
LEVEL_RANK = [
    (r"行政法规", "行政法规"),
    (r"地方政府规章", "地方政府规章"),
    (r"部门规章", "部门规章"),
    (r"部门规范性文件", "部门规范性文件"),
    (r"地方规范性文件", "地方规范性文件"),
    (r"(省级|设区的市|经济特区|自治条例|自治州|单行条例).{0,6}(法规|条例)", "地方性法规"),
    (r"司法解释", "司法解释"),
    (r"^宪法$", "宪法"),
    (r"^(民法|商法|经济法|社会法|刑法|诉讼|宪法相关|行政法)", "法律"),
]
RANK_ORDER = ["宪法", "法律", "司法解释", "行政法规", "部门规章",
              "地方性法规", "地方政府规章", "部门规范性文件", "地方规范性文件", "其他"]

# 个案答复与政策性文件：不是普遍适用的义务来源。
# 实测中「劳动部办公厅关于劳动者解除劳动合同有关问题的复函」会混入结果——
# 把它写进合规义务清单是硬伤：那是对特定请示的答复，不具普遍约束力。
NON_NORMATIVE = re.compile(
    r"(复函|批复|答复|请示|函〔|的函$|通知函|意见的通知$|"
    r"指导意见|工作方案|实施方案|典型案例|公告\d{4}年第|新闻发布|解读|问答)")

# 义务性条文标记。按强制程度分级。
OBLIGATION = [
    (r"(必须|禁止)", "强制", "违反通常直接对应行政处罚"),
    (r"不得", "禁止性", "违反通常直接对应行政处罚"),
    (r"应当", "义务性", "多数配有罚则，须核对法律责任章节"),
    (r"(有义务|负责|承担.{0,6}责任)", "责任性", "须结合具体情形判断"),
]
# 授权性、倡导性表述——不是义务，混进清单会让企业做无用功
NON_OBLIGATION = re.compile(r"(可以|鼓励|支持|倡导|提倡|逐步|引导|宜)")

# 法律责任（罚则）条款。语法上含「应当」，但它规定的是**违反义务的后果**，
# 不是义务本身。混进义务列会让清单里出现「应当被罚款」这种荒谬条目。
PENALTY_CLAUSE = re.compile(
    r"^\s*第[^\s]{1,12}条\s*(违反本(法|条例|办法|规定|细则)|有下列(行为|情形)之一的)"
    r"|由.{0,20}(部门|机关|政府).{0,16}(责令改正|给予警告|处以?罚款|没收违法所得|"
    r"吊销|责令停产停业|依法追究刑事责任)")

DRAFT = re.compile(r"草案|征求意见稿|送审稿")
HTML_TAG = re.compile(r"<[^>]{1,80}>")
REV_TAIL = re.compile(r"[(（][^（）()]{0,30}(修订|修正|修改)[^（）()]{0,10}[)）]\s*$")


def auth_headers():
    h = {"Content-Type": "application/json"}
    if ZHIHE_TOKEN:
        h["Authorization"] = ZHIHE_TOKEN if ZHIHE_TOKEN.lower().startswith("bearer ") \
            else f"Bearer {ZHIHE_TOKEN}"
    return h


def get(params, timeout=60):
    url = f"{BASE_URL}{EP}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers=auth_headers())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = json.loads(r.read().decode("utf-8"))
    # law 路由吞异常返回 HTTP 200 + code=500，不显式检查会被误读为「无规定」
    if str(raw.get("code", "200")) != "200":
        raise RuntimeError(f"上游错误 code={raw.get('code')} msg={raw.get('msg')}")
    return [x for x in ((raw.get("data") or {}).get("result") or []) if x.get("lawId")]


def clean(t):
    return HTML_TAG.sub("", t or "").strip()


def rank_of(xls):
    for pat, label in LEVEL_RANK:
        if re.search(pat, xls or ""):
            return label
    return "其他"


def base_name(n):
    """去掉修订后缀，用于同名法规新旧版本去重。"""
    return REV_TAIL.sub("", clean(n)).strip()


def obligation_kind(content):
    """判断条文的义务性质。授权性表述与罚则条款单独标出，避免混入清单。"""
    c = content or ""
    if PENALTY_CLAUSE.search(c):
        return "罚则", "法律责任条款，规定的是违反义务的后果，**不是义务本身**"
    for pat, kind, note in OBLIGATION:
        if re.search(pat, c):
            return kind, note
    if NON_OBLIGATION.search(c):
        return "授权/倡导", "非强制义务，**不应列入合规清单的必办事项**"
    return None, None


def search_action(action, plan, trace):
    """单个业务动作的多路检索。"""
    hits = []
    tmpl = plan.get("query_template") or "{subject}在{action}过程中应当履行的法定义务与禁止性规定"
    subject = plan.get("subject") or "经营者"
    q = tmpl.format(subject=subject, action=action)
    params = {
        "vector": q,
        "rows": plan.get("rows", 20),
        "lawstatexlsFacet": "1",
        "maxSingleLawItemCount": plan.get("max_per_law", 3),
    }
    if plan.get("xls"):
        params["xls"] = plan["xls"]
    if plan.get("area"):
        params["areaFacet"] = plan["area"]
    try:
        hits = get(params)
    except RuntimeError as e:
        trace.append(f"[{action}] 上游错误：{e}")
        return []
    except Exception as e:
        trace.append(f"[{action}] 请求失败：{e}")
        return []
    trace.append(f"[{action}] 命中 {len(hits)} 条")
    return hits


def search_penalty(law_name, trace):
    """针对某部法规检索其法律责任条款，用于填「违反后果」列。"""
    try:
        return get({"vector": f"{law_name}违反规定的法律责任行政处罚罚款责令改正没收违法所得",
                    "lawName": base_name(law_name), "rows": 5,
                    "lawstatexlsFacet": "1"})
    except Exception as e:
        trace.append(f"[罚则/{law_name[:16]}] 检索失败：{e}")
        return []


def build(plan, args):
    trace, actions_out = [], []
    all_laws = {}

    for action in plan.get("actions", []):
        hits = search_action(action, plan, trace)
        # 去重表**每个动作独立**。
        # 教训：初版把它放在动作循环外，同一条义务在第一个动作入池后，
        # 后续动作再命中就被判重复跳过——但同一条义务本来就可能适用于多个业务动作，
        # 结果第四个动作的义务全被吃掉，输出 0 条。
        seen_ver = {}
        rows, inline_penalty = [], []
        dropped_nonnorm = dropped_nonoblig = dropped_draft = 0
        for h in hits:
            name = clean(h.get("lawName"))
            if DRAFT.search(name):
                dropped_draft += 1
                continue
            if NON_NORMATIVE.search(name):
                dropped_nonnorm += 1
                continue
            content = h.get("content") or ""
            kind, kind_note = obligation_kind(content)
            if kind is None:
                continue
            if kind == "授权/倡导":
                dropped_nonoblig += 1
                if not args.include_permissive:
                    continue
            if kind == "罚则":
                inline_penalty.append({
                    "依据": f"[《{base_name(name)}》]({DETAIL.format(h['lawId'])})"
                            f"{h.get('rawnumber') or ''}",
                    "罚则": content[:300]})
                continue

            bn = base_name(name)
            imp = (h.get("implementYearMonthDate") or "")
            key = (bn, h.get("rawnumber"))
            rank = rank_of(h.get("xls"))
            rec = {
                "义务性质": kind, "性质说明": kind_note,
                "位阶": rank,
                "依据": f"[《{bn}》]({DETAIL.format(h['lawId'])}){h.get('rawnumber') or ''}",
                "法规全称": name,
                "条号": h.get("rawnumber"),
                "监管机关": h.get("issuingOrgan") or "未标注",
                "施行日": imp,
                "条文": content[:400],
            }
            # 同名法规同条号的新旧版本去重：新版**替换**旧版。
            # 教训：初版只在旧版更新时 continue，导致旧版先入池、新版后入池，两版并存。
            prev = seen_ver.get(key)
            if prev is None:
                seen_ver[key] = (imp, len(rows))
                rows.append(rec)
            elif imp > prev[0]:
                rows[prev[1]] = rec
                seen_ver[key] = (imp, prev[1])
            all_laws.setdefault(bn, rank)

        rows.sort(key=lambda r: (RANK_ORDER.index(r["位阶"]) if r["位阶"] in RANK_ORDER else 99,
                                 ["强制", "禁止性", "义务性", "责任性", "授权/倡导"].index(r["义务性质"])))
        note = []
        if dropped_nonnorm:
            note.append(f"剔除个案答复/政策文件 {dropped_nonnorm} 条（复函、批复、指导意见等"
                        f"不是普遍适用的义务来源）")
        if dropped_nonoblig:
            note.append(f"剔除授权/倡导性条文 {dropped_nonoblig} 条"
                        f"（「可以」「鼓励」不是必办义务）")
        if dropped_draft:
            note.append(f"剔除草案 {dropped_draft} 条")
        if inline_penalty:
            note.append(f"识别出 {len(inline_penalty)} 条法律责任条款，已移入「对应罚则」")
        actions_out.append({
            "业务动作": action, "义务条数": len(rows),
            "筛除说明": note or ["无剔除"], "义务": rows,
            "对应罚则": inline_penalty[:6],
        })

    penalties = {}
    if not args.no_penalty:
        top = [n for n, r in all_laws.items()
               if r in ("法律", "行政法规", "部门规章")][:args.penalty_top]
        for n in top:
            ps = search_penalty(n, trace)
            items = []
            for p_ in ps:
                c = p_.get("content") or ""
                if re.search(r"(罚款|责令|没收|吊销|停产停业|拘留|降级|撤职|追究刑事责任)", c):
                    items.append({
                        "依据": f"[《{base_name(clean(p_.get('lawName')))}》]"
                                f"({DETAIL.format(p_['lawId'])}){p_.get('rawnumber') or ''}",
                        "罚则": c[:260]})
            if items:
                penalties[n] = items[:4]

    return {
        "适用范围": {
            "主体": plan.get("subject"), "行业": plan.get("industry"),
            "地域": plan.get("area") or "全国",
            "业务动作数": len(plan.get("actions", [])),
        },
        "检索痕迹": trace,
        "义务矩阵": actions_out,
        "违反后果（罚则）": penalties,
        "提示": [
            "**本清单按业务动作组织，不是法条汇编。** 每一条都应能落到具体的岗位与动作上，"
            "落不下去的说明拆解不够细",
            "⚠️ **行业分类过滤实测不可用**（topicsFacet 任何值均返回 0 条），"
            "行业维度由业务动作的语义表述承载。这意味着**行业特殊监管可能漏检**，"
            "强监管行业须另行核对主管部门规章与地方细则",
            "已剔除复函、批复、指导意见等个案答复与政策性文件——它们不具普遍约束力，"
            "但**实务中监管部门可能据此执法**，重大事项仍建议单独查阅并与主管部门确认",
            "「可以」「鼓励」类表述不是义务，已剔除（--include-permissive 可保留）",
            "罚则为按法规名检索所得，**未逐条核对义务条款与罚则条款的对应关系**，"
            "填入矩阵前须人工确认",
            "地方性法规差异显著，未指定 area 时仅覆盖全国性规定",
        ],
    }


def main():
    p = argparse.ArgumentParser(description="合规义务矩阵",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--plan", required=True, help="方案 JSON，见 references/plan-schema.md")
    p.add_argument("--no-penalty", action="store_true", help="跳过罚则检索（省时间）")
    p.add_argument("--penalty-top", type=int, default=6, help="检索罚则的法规数上限")
    p.add_argument("--include-permissive", action="store_true",
                   help="保留「可以/鼓励」类授权性条文")
    args = p.parse_args()

    try:
        plan = json.load(open(args.plan, encoding="utf-8"))
    except Exception as e:
        sys.exit(f"错误：方案文件读取失败 {e}")
    if not plan.get("actions"):
        sys.exit("错误：actions 为空。合规义务必须按**业务动作**拆解——"
                 "「食品行业有什么规定」这种粒度产出的是法条汇编，不是可落地的清单。")

    print(json.dumps(build(plan, args), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
