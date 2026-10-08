#!/usr/bin/env python3
"""
对手背景调查

回答起诉前最该问但多数人不问的一个问题：**赢了能不能拿到钱。**

打一场官司要投入一到两年，最坏的结果不是败诉，是胜诉后发现对方没有可供执行的财产。
本脚本从裁判文书库中提取对手的涉诉画像与**履行能力信号**：

  涉诉画像 —— 多少案子、什么案由、当原告还是被告、结果如何、时间趋势
  履行能力 —— **执行案件数量、终本案件、被查封冻结的记录**（最硬的信号）
  争议偏好 —— 是否好讼、是否常被起诉、是否倾向调解或撤诉

⚠️ **数据来源的根本局限（必须在报告中披露）**：
  裁判文书库不等于全部涉诉记录。未起诉的纠纷、调解结案未上网的、
  依法不公开的（涉隐私、商业秘密、未成年人）都不在库中。
  **查不到不等于没有。**

用法：
    opponent_bg.py --name "某某科技有限公司" --alias "某某科技" --limit 60
    opponent_bg.py --name "张三" --name "某某公司" --related
"""

import argparse
import json
import os
import re
import sys
import urllib.request
from collections import Counter, defaultdict

BASE_URL = os.environ.get(
    "ZHIHE_BASE_URL",
    "https://your-legal-api.example.com").rstrip("/")
ZHIHE_TOKEN = os.environ.get("ZHIHE_TOKEN", "")
DETAIL = "https://www.zhiexa.com/fuxi/judgment-documents/detail?id={}"
EP2 = "/case/vector2"

# 执行类案件：履行能力最硬的信号
EXEC_RE = re.compile(r"执行|执\d|执异|执复|执恢|执保")
# 终本：法院认定无财产可供执行
TERMINATED = {"终结本次执行程序", "终结执行"}
# 失信与限高的文书特征
CREDIT_RE = re.compile(r"失信被执行人|限制消费|限制高消费|纳入失信")
# 破产：最严重信号
BANKRUPT_RE = re.compile(r"破产|重整|清算|破申|强清")
# 程序分类
PROC_KIND = [
    (r"执行", "执行案件"), (r"破产|强制清算", "破产清算"),
    (r"一审|初审", "一审"), (r"二审", "二审"),
    (r"再审|审判监督", "再审"), (r"保全|财产保全", "保全"),
    (r"行政", "行政案件"), (r"刑事", "刑事案件"),
]
# 我方视角：对手是原告还是被告
PLAINTIFF_MARK = re.compile(r"^(原告|上诉人|申请人|申请执行人|再审申请人)")
DEFENDANT_MARK = re.compile(r"(被告|被上诉人|被申请人|被执行人)")


def auth_headers():
    h = {"Content-Type": "application/json"}
    if ZHIHE_TOKEN:
        h["Authorization"] = ZHIHE_TOKEN if ZHIHE_TOKEN.lower().startswith("bearer ") \
            else f"Bearer {ZHIHE_TOKEN}"
    return h


def post(body, timeout=120):
    req = urllib.request.Request(
        f"{BASE_URL}{EP2}", data=json.dumps(body).encode("utf-8"),
        headers=auth_headers(), method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = json.loads(r.read().decode("utf-8"))
    if isinstance(raw, dict) and raw.get("detail"):
        raise RuntimeError(json.dumps(raw["detail"], ensure_ascii=False)[:250])
    return raw


def proc_kind(proc, case_id):
    t = (proc or "") + (case_id or "")
    for pat, name in PROC_KIND:
        if re.search(pat, t):
            return name
    return "其他"


def role_of(parties, names):
    """判断对手在该案中是原告方还是被告方——反映其是否好讼。"""
    if not parties:
        return "不明"
    txt = parties if isinstance(parties, str) else "\n".join(parties)
    for seg in re.split(r"[\n；;]", txt):
        if not any(n in seg for n in names):
            continue
        if PLAINTIFF_MARK.search(seg.strip()):
            return "原告方"
        if DEFENDANT_MARK.search(seg.strip()):
            return "被告方"
    return "不明"


def search(names, limit, date_start):
    seen, pool, trace = set(), [], []
    for n in names:
        body = {"search_mode": "keyword", "keywords": n,
                "limit": limit, "instruct_limit": 0}
        if date_start:
            body["judgment_date_start"] = date_start
        try:
            raw = post(body)
        except Exception as e:
            trace.append(f"[{n}] 检索失败：{e}")
            continue
        hits = [h for h in (raw.get("results") or []) if h.get("id")]
        new = [h for h in hits if h["id"] not in seen]
        for h in new:
            seen.add(h["id"])
            h["_kw"] = n
        pool += new
        trace.append(f"[{n}] 命中 {len(hits)} 条，新增 {len(new)} 条")
    return pool, trace


def analyze(pool, names):
    rows = []
    for h in pool:
        cid = h.get("case_id") or ""
        proc = h.get("procedure") or ""
        kind = proc_kind(proc, cid)
        txt = (h.get("court_opinion") or "") + (h.get("summary") or "")
        rows.append({
            "cite": f"[{cid}]({DETAIL.format(h['id'])})",
            "case_id": cid, "法院": h.get("court_name"),
            "审级": h.get("court_level"), "程序": proc, "类型": kind,
            "裁判日期": h.get("judgment_date"), "结果": h.get("result_type"),
            "对手角色": role_of(h.get("parties"), names),
            "_失信信号": bool(CREDIT_RE.search(txt)),
            "_破产信号": bool(BANKRUPT_RE.search(cid + proc)),
        })

    execs = [r for r in rows if r["类型"] == "执行案件"]
    terminated = [r for r in execs if str(r["结果"]) in TERMINATED]
    credit = [r for r in rows if r["_失信信号"]]
    bankrupt = [r for r in rows if r["_破产信号"]]

    # 履行能力评级：只给信号强弱，不给结论
    signals = []
    if bankrupt:
        signals.append(("🔴 高", f"检出 {len(bankrupt)} 条破产/清算相关记录，"
                                 f"**债权实现存在根本风险**，须核实企业存续状态"))
    if terminated:
        signals.append(("🔴 高", f"检出 {len(terminated)} 件**终结本次执行**案件——"
                                 f"法院已认定其无财产可供执行。这是履行能力最硬的负面信号"))
    if credit:
        signals.append(("🟡 中", f"检出 {len(credit)} 条涉失信被执行人或限制消费的记载"))
    if execs and not terminated:
        signals.append(("🟡 中", f"检出 {len(execs)} 件执行案件（未见终本）——"
                                 f"存在被强制执行的历史，但未见无财产认定"))
    if not execs and not bankrupt:
        signals.append(("🔵 低", "**未检出执行案件与破产记录**。"
                                 "这是相对正面的信号，但**不等于有履行能力**——"
                                 "未被起诉、已履行完毕、案件未上网都会导致查不到"))

    years = Counter(r["裁判日期"][:4] for r in rows if r["裁判日期"])
    roles = Counter(r["对手角色"] for r in rows)
    recent = sorted((y for y in years if y), reverse=True)[:3]
    trend = "近三年 " + "、".join(f"{y}年{years[y]}件" for y in recent) if recent else "无日期数据"

    prof = {
        "案件总数": len(rows),
        "类型分布": dict(Counter(r["类型"] for r in rows).most_common()),
        "结果分布": dict(Counter(str(r["结果"]) for r in rows).most_common(8)),
        "审级分布": dict(Counter(r["审级"] or "未知" for r in rows).most_common()),
        "年份分布": dict(sorted(years.items())),
        "时间趋势": trend,
        "诉讼角色": dict(roles),
    }
    if roles.get("原告方", 0) >= 3 and roles["原告方"] > roles.get("被告方", 0):
        prof["诉讼偏好"] = ("主动起诉多于被诉，**对手较为好讼**——"
                            "谈判中可能倾向于以诉讼施压，且应诉经验较丰富")
    elif roles.get("被告方", 0) >= 3 and roles["被告方"] > roles.get("原告方", 0):
        prof["诉讼偏好"] = ("被诉多于主动起诉，须核查被诉事由是否集中于同类问题——"
                            "**同类纠纷反复出现往往指向其经营模式本身的问题**")

    return rows, prof, signals, {
        "执行案件": len(execs), "终本案件": len(terminated),
        "失信/限高记载": len(credit), "破产清算": len(bankrupt),
        "执行案件明细": [{"cite": r["cite"], "结果": r["结果"], "日期": r["裁判日期"]}
                         for r in execs[:10]],
    }


def main():
    p = argparse.ArgumentParser(description="对手背景调查",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--name", action="append", required=True,
                   help="对手名称，可重复。公司用全称，个人用姓名")
    p.add_argument("--alias", action="append", default=[],
                   help="别称、简称、曾用名，可重复。**改过名的主体必须补上**")
    p.add_argument("--limit", type=int, default=60, help="每个名称的检索条数")
    p.add_argument("--date-start", dest="date_start", help="裁判日期起 YYYYMMDD")
    p.add_argument("--related", action="store_true",
                   help="额外提示关联主体排查方向（脚本不自动查关联企业）")
    args = p.parse_args()

    names = args.name + args.alias
    pool, trace = search(names, args.limit, args.date_start)

    if not pool:
        print(json.dumps({
            "对手": names, "检索痕迹": trace, "结果": "未检出涉诉记录",
            "⚠️不得据此认定": [
                "**查不到 ≠ 没有涉诉。** 裁判文书库不等于全部涉诉记录",
                "可能原因：名称不准确或有别称、纠纷未进入诉讼、"
                "调解结案未上网、依法不公开的案件、案件较新尚未上网",
                "**下一步**：核对工商登记的准确全称与曾用名后重查；"
                "并通过中国执行信息公开网、信用中国等公开渠道交叉核验",
            ],
        }, ensure_ascii=False, indent=2))
        return

    rows, prof, signals, exec_info = analyze(pool, names)

    notes = [
        "⚠️ **数据来源的根本局限**：裁判文书库不等于全部涉诉记录。"
        "未起诉的纠纷、调解结案未上网的、依法不公开的（涉隐私、商业秘密、未成年人）"
        "都不在库中。**查不到不等于没有**",
        "⚠️ **同名风险**：关键词检索可能命中同名的其他主体。"
        "引用具体案件前**必须核对当事人的完整信息**（住所地、统一社会信用代码）",
        "**本报告不替代公开渠道核查。** 被执行人、失信名单、限制消费令应通过"
        "中国执行信息公开网核实；行政处罚、经营异常应通过信用中国与国家企业信用信息公示系统核实",
        "**履行能力信号是概率性的，不是结论。** 未检出执行案件不等于有偿付能力，"
        "检出终本也不等于完全无财产——终本后发现新财产可恢复执行",
    ]
    if args.related:
        notes.append("**关联主体排查方向**（脚本不自动查）：法定代表人及其控制的其他企业、"
                     "股东与实际控制人、同一住所地的关联公司、曾用名与变更记录。"
                     "**转移财产往往发生在关联主体之间**")

    print(json.dumps({
        "对手": {"名称": args.name, "别称": args.alias or "无"},
        "检索痕迹": trace,
        "一、涉诉画像": prof,
        "二、履行能力信号": {
            "统计": {k: v for k, v in exec_info.items() if k != "执行案件明细"},
            "评级与依据": [{"等级": lv, "说明": txt} for lv, txt in signals],
            "执行案件明细": exec_info["执行案件明细"] or "无",
        },
        "三、案件清单": sorted(rows, key=lambda r: r["裁判日期"] or "", reverse=True)[:40],
        "提示": notes,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
