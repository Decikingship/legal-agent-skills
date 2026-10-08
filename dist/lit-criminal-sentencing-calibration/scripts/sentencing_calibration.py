#!/usr/bin/env python3
"""
刑事量刑校准

输入罪名 + 涉案金额 + 情节，输出同类案件的**实际量刑分布**。
这是通用大模型完全做不了的——它依赖 search3 提供的结构化字段：
  charged_crime（指控罪名）/ convicted_crime（审定罪名）/ involved_amount /
  prison_term_months（**数值刑期**）/ fine_amount / is_final_sentence

三条实测决定的硬规则：
  1. **按 is_final_sentence 分组**。数罪并罚「决定执行的刑」与单罪宣告刑混算，
     会把刑期分布整体拉高——为当事人做量刑预期时，这是有实际损害的错误
  2. **二审案件没有量刑数值**。实测二审的 convicted_crime、prison_term_months
     全为 None，纳入统计会悄悄削减有效样本，须以一审判决为主
  3. **罪名必须用案由树标准全称**。写「合同诈骗」缺个「罪」字，
     服务端静默返回空列表，极易被误读为「无此类案例」

用法：
    sentencing_calibration.py --crime 诈骗罪 --amount-min 700000 --amount-max 900000 \
        --area 浙江省 --scene "<≥100字案情>"
"""

import argparse
import json
import os
import re
import statistics
import sys
import urllib.request
from collections import Counter, defaultdict

BASE_URL = os.environ.get(
    "ZHIHE_BASE_URL",
    "https://your-legal-api.example.com").rstrip("/")
ZHIHE_TOKEN = os.environ.get("ZHIHE_TOKEN", "")
DETAIL = "https://www.zhiexa.com/fuxi/judgment-documents/detail?id={}"
EP3 = "/case/vector3"

FIRST_INSTANCE = re.compile(r"一审|初审")
FINAL_STAGE = re.compile(r"二审|再审|终审|复核")
# 常见从宽从严情节，用于分组比较
CIRCUMSTANCES = [
    ("自首", r"自首"),
    ("坦白", r"坦白|如实供述"),
    ("认罪认罚", r"认罪认罚"),
    ("退赃退赔", r"退(赃|赔|还)|赔偿(被害人|损失)"),
    ("取得谅解", r"谅解"),
    ("立功", r"立功"),
    ("从犯", r"从犯|次要作用|辅助作用"),
    ("累犯", r"累犯"),
    ("主犯", r"主犯|组织|策划|指挥"),
    ("缓刑", r"缓刑"),
]


def auth_headers():
    h = {"Content-Type": "application/json"}
    if ZHIHE_TOKEN:
        h["Authorization"] = ZHIHE_TOKEN if ZHIHE_TOKEN.lower().startswith("bearer ") \
            else f"Bearer {ZHIHE_TOKEN}"
    return h


def post(body, timeout=120):
    req = urllib.request.Request(
        f"{BASE_URL}{EP3}", data=json.dumps(body).encode("utf-8"),
        headers=auth_headers(), method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = json.loads(r.read().decode("utf-8"))
    if isinstance(raw, dict) and raw.get("detail"):
        raise RuntimeError(json.dumps(raw["detail"], ensure_ascii=False)[:250])
    return raw


def months_desc(m):
    if m is None:
        return None
    if m >= 12:
        y, mm = int(m // 12), int(m % 12)
        return f"{y}年" + (f"{mm}个月" if mm else "")
    return f"{int(m)}个月"


def collect(args):
    trace, rows = [], []
    body = {
        "search_mode": "mvector",
        "summary_vector": args.scene, "summary_weight": 1.0,
        "crime": args.crime, "limit": args.limit, "instruct_limit": 0,
    }
    if args.amount_min:
        body["involved_amount_min"] = args.amount_min
    if args.amount_max:
        body["involved_amount_max"] = args.amount_max
    if args.area:
        body["court_area"] = args.area
    if args.date_start:
        body["judgment_date_start"] = args.date_start
    if args.result_type:
        body["result_type"] = args.result_type

    try:
        raw = post(body)
    except RuntimeError as e:
        trace.append(f"请求被拒：{e}")
        return trace, rows
    except Exception as e:
        trace.append(f"请求失败：{e}")
        return trace, rows

    hits = raw.get("results") or []
    trace.append(f"命中 {len(hits)} 件")
    if not hits:
        trace.append(
            f"⚠️ 零结果。**首先怀疑罪名写法**——`--crime「{args.crime}」`"
            f"若无法映射到案由编码，服务端会静默返回空列表（不报错）。"
            f"罪名须用案由树标准全称并带「罪」字；口语转换："
            f"醉驾→危险驾驶罪、非法集资→非法吸收公众存款罪或集资诈骗罪、"
            f"帮信→帮助信息网络犯罪活动罪。确认写法无误后，再考虑放宽金额或地域")
        return trace, rows

    no_term_appeal = 0
    for h in hits:
        proc = h.get("procedure") or ""
        is_first = bool(FIRST_INSTANCE.search(proc)) and not FINAL_STAGE.search(proc)
        txt = (h.get("court_opinion") or "") + (h.get("summary") or "")
        circ = [n for n, pat in CIRCUMSTANCES if re.search(pat, txt)]
        convs = h.get("convictions") or []
        # 按**被告人**统计该人被判几个罪，而非按案件——
        # 一案多被告时，每人各自可能是单罪或数罪。
        per_def = Counter(c.get("defendant") or "" for c in convs)
        for cv in convs:
            term = cv.get("prison_term_months")
            if term is None:
                if not is_first:
                    no_term_appeal += 1
                continue
            rows.append({
                "cite": f"[{h.get('case_id')}]({DETAIL.format(h['id'])})" if h.get("id") else None,
                "case_id": h.get("case_id"), "法院": h.get("court_name"),
                "审级": h.get("court_level"), "程序": proc, "一审": is_first,
                "裁判日期": h.get("judgment_date"), "结果": h.get("result_type"),
                "被告人": cv.get("defendant"),
                "指控罪名": cv.get("charged_crime"), "审定罪名": cv.get("convicted_crime"),
                "涉案金额": cv.get("involved_amount"),
                "刑期月": term, "刑期": months_desc(term),
                "罚金": cv.get("fine_amount"),
                "主刑类型": cv.get("penalty_type"),
                "最终决定刑": cv.get("is_final_sentence"),
                "该被告罪数": per_def.get(cv.get("defendant") or "", 1),
                "情节": circ,
            })
    if no_term_appeal:
        trace.append(
            f"{no_term_appeal} 条二审记录无刑期数值（实测二审的 convicted_crime、"
            f"prison_term_months 全为 None），已排除。**量刑统计以一审判决为主**")
    return trace, rows


def dist(vals):
    if not vals:
        return None
    v = sorted(vals)
    n = len(v)
    return {
        "样本数": n,
        "区间": f"{months_desc(v[0])} – {months_desc(v[-1])}",
        "中位数": months_desc(statistics.median(v)),
        "四分位": f"{months_desc(v[n // 4])} – {months_desc(v[(3 * n) // 4])}" if n >= 4 else None,
        "原始月数": v,
    }


def analyze(rows, args):
    # 分组逻辑（实测修正）：
    #   is_final_sentence=True 只表示「这是最终执行的刑」——
    #   单罪案件的宣告刑本身就是最终执行刑，也是 True。
    #   仅按该字段二分，会把 28 件可比的单罪案例误归入「数罪并罚」，
    #   让使用者以为没有可用样本。真正的判断依据是**该被告被判了几个罪**。
    single = [r for r in rows if r["该被告罪数"] <= 1]
    multi_final = [r for r in rows if r["该被告罪数"] > 1 and r["最终决定刑"] is True]
    multi_part = [r for r in rows if r["该被告罪数"] > 1 and r["最终决定刑"] is not True]

    out = {
        "样本构成": {
            "总记录": len(rows),
            "单罪案件宣告刑": len(single),
            "数罪并罚·决定执行刑": len(multi_final),
            "数罪并罚·分罪宣告刑": len(multi_part),
            "一审记录": sum(1 for r in rows if r["一审"]),
        },
        "⚠️分组依据": (
            "研究**单一罪名**的量刑，应看「单罪案件宣告刑」与「数罪并罚·分罪宣告刑」，"
            "**不要看「决定执行刑」**——后者受其他罪名牵连，会把分布整体拉高，"
            "据此为当事人做量刑预期是有实际损害的错误"),
        "单罪案件宣告刑分布": dist([r["刑期月"] for r in single]),
        "数罪并罚·分罪宣告刑分布": dist([r["刑期月"] for r in multi_part]),
        "数罪并罚·决定执行刑分布": dist([r["刑期月"] for r in multi_final]),
        "本罪可比样本分布": dist([r["刑期月"] for r in single + multi_part]),
    }

    comparable = single + multi_part
    if comparable:
        single = comparable   # 后续统计一律基于「本罪可比样本」
        amt = [r["涉案金额"] for r in single if r["涉案金额"]]
        if amt:
            a = sorted(amt)
            out["涉案金额分布"] = {
                "样本数": len(a),
                "区间": f"{a[0]:,.0f} – {a[-1]:,.0f} 元",
                "中位数": f"{statistics.median(a):,.0f} 元",
            }
        fines = [r["罚金"] for r in single if r["罚金"]]
        if fines:
            f = sorted(fines)
            out["罚金分布"] = {"样本数": len(f),
                               "区间": f"{f[0]:,.0f} – {f[-1]:,.0f} 元",
                               "中位数": f"{statistics.median(f):,.0f} 元"}

        by_circ = defaultdict(list)
        for r in single:
            for c in r["情节"] or ["未识别情节"]:
                by_circ[c].append(r["刑期月"])
        out["按情节分组"] = {
            c: {"件数": len(v), "刑期区间": f"{months_desc(min(v))} – {months_desc(max(v))}",
                "中位数": months_desc(statistics.median(v)),
                "⚠️": "样本不足 5 件，仅供个案参考" if len(v) < 5 else None}
            for c, v in sorted(by_circ.items(), key=lambda x: -len(x[1]))
        }
        out["罪名变化"] = {
            "指控与审定不一致": [
                {"cite": r["cite"], "指控": r["指控罪名"], "审定": r["审定罪名"]}
                for r in single
                if r["指控罪名"] and r["审定罪名"] and r["指控罪名"] != r["审定罪名"]
            ][:8],
            "说明": "指控罪名与审定罪名不一致的案例，揭示的是**定性可争取的空间**——"
                    "对辩护而言，改变定性的价值通常大于在同一罪名内争取从轻",
        }

    n = len(comparable)
    if n < 20:
        out["⚠️样本量"] = (
            f"本罪可比样本仅 {n} 件，**任何区间与中位数均无统计意义**，"
            f"只可作个案参考，不得作为量刑预测的依据")
    return out


def main():
    p = argparse.ArgumentParser(description="刑事量刑校准",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--crime", required=True,
                   help="罪名，**必须用案由树标准全称并带「罪」字**，可空格分隔多个取 OR")
    p.add_argument("--scene", required=True, help="案情描述，≥100字陈述句")
    p.add_argument("--amount-min", dest="amount_min", type=int, help="涉案金额下限（元）")
    p.add_argument("--amount-max", dest="amount_max", type=int, help="涉案金额上限（元）")
    p.add_argument("--area", help="法院地域")
    p.add_argument("--date-start", dest="date_start", default="20200101")
    p.add_argument("--result-type", dest="result_type", nargs="+", type=int,
                   help="裁判结果类型，如 25 有罪判决")
    p.add_argument("--limit", type=int, default=40)
    args = p.parse_args()

    if not args.crime.rstrip().endswith("罪") and " " not in args.crime:
        print(f"提示：罪名「{args.crime}」不以「罪」结尾，映射失败会静默返回空结果",
              file=sys.stderr)
    if len(args.scene) < 100:
        print(f"提示：--scene 仅 {len(args.scene)} 字，低于建议的 100 字，"
              f"语义向量过短会显著降低召回质量", file=sys.stderr)

    trace, rows = collect(args)
    result = {
        "检索条件": {"罪名": args.crime, "金额区间": [args.amount_min, args.amount_max],
                     "地域": args.area or "全国", "裁判日期起": args.date_start},
        "检索痕迹": trace,
    }
    if rows:
        result.update(analyze(rows, args))
        result["案例明细"] = sorted(rows, key=lambda r: r["刑期月"])[:30]
    result["提示"] = [
        "**这是同类案件的统计分布，不是对本案的量刑预测。** "
        "量刑取决于全部情节、证据情况、被告人表现与法院裁量，"
        "把区间中位数说成「大概判几年」是过度承诺",
        "样本来自语义检索，**非随机抽样**，存在选择偏差",
        "**不得以审判人员为维度做任何统计或倾向分析**（合规红线）",
        "涉案金额的认定本身常有争议，金额区间检索假定了金额已确定；"
        "若金额存在争议，应先做金额认定的类案检索",
    ]
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
