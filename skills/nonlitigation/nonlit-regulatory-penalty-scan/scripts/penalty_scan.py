#!/usr/bin/env python3
"""
监管处罚风险画像

回答企业最关心的合规问题：**同行因为什么被罚？罚多少？被诉后法院怎么判？**

⚠️ **数据来源的根本局限，必须在报告中披露：**
  案例库是**裁判文书库**，不是行政处罚决定书库。这里能查到的只有
  「被处罚人不服、提起行政诉讼」的那部分处罚——绝大多数处罚未被起诉，不在库中。
  因此本工具产出的是**处罚争议的画像**，不是处罚全貌，
  更不能用来推算「被罚概率」。

三个易错点已内置处理：
  1. `驳回起诉` 是程序性裁定（起诉不符合受理条件），**不等于处罚被维持**
  2. 民事案件会混入行政处罚检索结果，须按 procedure 过滤
  3. 罚款金额散落在多个字段，且常出现「罚款人民币伍万元」这类中文写法

用法：
    penalty_scan.py --plan plan.json
    penalty_scan.py --scene "网络食品交易平台未审查入网经营者许可证" --area 上海市
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
EP = "/case/vector2"

# 行政案件才是处罚争议。民事案件会混进来（如平台与商家的合同纠纷提到处罚），须剔除。
ADMIN_PROC = re.compile(r"行政")
# 结果类型的三分：处罚被维持 / 处罚被否定 / 程序性未进入实体
RT_UPHELD = {"驳回全部诉讼请求", "维持原判"}        # 原告（被处罚人）败诉 = 处罚站得住
RT_OVERTURNED = {"全部支持", "部分支持", "改判"}     # 原告胜诉 = 处罚被撤销或变更
RT_PROCEDURAL = {"驳回起诉", "不予受理", "不予受理/立案", "撤诉", "终止审理", "中止审理"}

# 行政非诉执行审查（案号代字「行审」）：行政机关申请法院强制执行处罚决定。
# 这类案件的价值高于行政诉讼——它对应的是**企业未起诉、处罚已生效**的情形，
# 更接近真实执法水平，正好补上「库里只有被诉处罚」这一局限的一部分。
NONLITIGATION_ENFORCE = re.compile(r"行政非诉|非诉执行|行审")

CN_NUM = {"零": 0, "〇": 0, "一": 1, "壹": 1, "二": 2, "贰": 2, "两": 2,
          "三": 3, "叁": 3, "四": 4, "肆": 4, "五": 5, "伍": 5, "六": 6, "陆": 6,
          "七": 7, "柒": 7, "八": 8, "捌": 8, "九": 9, "玖": 9}
CN_UNIT = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}
FINE_RE = re.compile(
    r"罚款[^。；\n]{0,24}?([\d,，.]{2,15}|[零〇一壹二贰两三叁四肆五伍六陆七柒八捌九玖十拾百佰千仟万萬亿]{1,12})\s*(万元|元)")
CONFISCATE = re.compile(r"没收(违法所得|非法财物)[^。；\n]{0,20}?([\d,，.]{2,15})\s*(万元|元)")
REVOKE = re.compile(r"(吊销|撤销)[^。；\n]{0,12}?(许可证|执照|资质|资格)")
SUSPEND = re.compile(r"责令(停产停业|停止(经营|生产|营业)|歇业)")


def auth_headers():
    h = {"Content-Type": "application/json"}
    if ZHIHE_TOKEN:
        h["Authorization"] = ZHIHE_TOKEN if ZHIHE_TOKEN.lower().startswith("bearer ") \
            else f"Bearer {ZHIHE_TOKEN}"
    return h


def post(body, timeout=120):
    req = urllib.request.Request(
        f"{BASE_URL}{EP}", data=json.dumps(body).encode("utf-8"),
        headers=auth_headers(), method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = json.loads(r.read().decode("utf-8"))
    if isinstance(raw, dict) and raw.get("detail"):
        raise RuntimeError(json.dumps(raw["detail"], ensure_ascii=False)[:250])
    return raw


def cn2num(s):
    """中文数字转阿拉伯。「伍万」「壹佰贰拾」这类金额写法在判决书里很常见。"""
    s = s.strip()
    if re.fullmatch(r"[\d,，.]+", s):
        try:
            return float(s.replace(",", "").replace("，", ""))
        except ValueError:
            return None
    total = section = num = 0
    for ch in s:
        if ch in CN_NUM:
            num = CN_NUM[ch]
        elif ch in CN_UNIT:
            section += (num or 1) * CN_UNIT[ch]
            num = 0
        elif ch in ("万", "萬"):
            total += (section + num) * 10000
            section = num = 0
        elif ch == "亿":
            total += (section + num) * 100000000
            section = num = 0
        else:
            return None
    return total + section + num


def extract_measures(text):
    """抽取处罚种类与幅度。"""
    out = {"罚款": [], "没收": [], "吊销撤销": False, "责令停业": False}
    for m in FINE_RE.finditer(text or ""):
        v = cn2num(m.group(1))
        if v is None:
            continue
        if m.group(2) in ("万元",):
            v *= 10000
        if 100 <= v <= 5e8:          # 过滤条号、年份等误命中
            out["罚款"].append(v)
    for m in CONFISCATE.finditer(text or ""):
        v = cn2num(m.group(2))
        if v and 100 <= v <= 5e8:
            out["没收"].append(v * (10000 if m.group(3) == "万元" else 1))
    out["吊销撤销"] = bool(REVOKE.search(text or ""))
    out["责令停业"] = bool(SUSPEND.search(text or ""))
    return out


def classify(rt, procedure="", case_id=""):
    rt = str(rt or "")
    if NONLITIGATION_ENFORCE.search(procedure or "") or "行审" in (case_id or ""):
        return "非诉执行审查（处罚已生效，企业未起诉）"
    if rt in RT_UPHELD:
        return "处罚被维持"
    if rt in RT_OVERTURNED:
        return "处罚被撤销或变更"
    if rt in RT_PROCEDURAL:
        return "未进入实体审查"
    return "其他"


def run(plan):
    trace, pool, seen = [], [], set()
    for scene in plan["scenes"]:
        body = {
            "search_mode": "mvector",
            "summary_vector": scene, "summary_weight": 1.0,
            "limit": plan.get("limit", 25), "instruct_limit": 0,
        }
        for k in ("court_area", "court_level", "judgment_date_start", "judgment_date_end"):
            if plan.get(k):
                body[k] = plan[k]
        try:
            raw = post(body)
        except Exception as e:
            trace.append(f"[{scene[:24]}…] 检索失败：{e}")
            continue
        hits = raw.get("results") or []
        admin = [h for h in hits if ADMIN_PROC.search(h.get("procedure") or "")]
        trace.append(f"[{scene[:24]}…] 命中 {len(hits)}，其中行政案件 {len(admin)}"
                     f"（剔除民事等非处罚争议 {len(hits) - len(admin)}）")
        for h in admin:
            if not h.get("id") or h["id"] in seen:
                continue
            seen.add(h["id"])
            h["_scene"] = scene
            pool.append(h)

    rows = []
    for h in pool:
        txt = (h.get("court_opinion") or "") + (h.get("summary") or "")
        me = extract_measures(txt)
        rows.append({
            "cite": f"[{h.get('case_id')}]({DETAIL.format(h['id'])})",
            "case_id": h.get("case_id"), "法院": h.get("court_name"),
            "审级": h.get("court_level"), "程序": h.get("procedure"),
            "裁判日期": h.get("judgment_date"), "结果": h.get("result_type"),
            "处罚是否成立": classify(h.get("result_type"), h.get("procedure"),
                                     h.get("case_id")),
            "罚款(元)": me["罚款"], "没收(元)": me["没收"],
            "吊销撤销": me["吊销撤销"], "责令停业": me["责令停业"],
            "对应场景": h["_scene"][:40],
            "本院认为": (h.get("court_opinion") or "")[:500],
        })

    cls = Counter(r["处罚是否成立"] for r in rows)
    upheld, overturned = cls.get("处罚被维持", 0), cls.get("处罚被撤销或变更", 0)
    fines = sorted(v for r in rows for v in r["罚款(元)"])

    stat = {
        "有效样本": len(rows),
        "结果构成": dict(Counter(str(r["结果"]) for r in rows).most_common()),
        "处罚争议结论": dict(cls),
        "按年份": dict(sorted(Counter((r["裁判日期"] or "")[:4] or "未知" for r in rows).items())),
        "按法院": dict(Counter(r["法院"] or "未知" for r in rows).most_common(8)),
        "处罚种类": {
            "涉罚款": sum(1 for r in rows if r["罚款(元)"]),
            "涉没收": sum(1 for r in rows if r["没收(元)"]),
            "涉吊销撤销": sum(1 for r in rows if r["吊销撤销"]),
            "涉责令停业": sum(1 for r in rows if r["责令停业"]),
        },
    }
    if fines:
        stat["罚款幅度"] = {
            "样本数": len(fines),
            "区间": f"{fines[0]:,.0f} – {fines[-1]:,.0f} 元",
            "中位数": f"{fines[len(fines) // 2]:,.0f} 元",
            "⚠️": "金额由判决正文正则抽取，可能混入非本案罚款数额（如援引的其他案件、"
                   "法条中的处罚幅度），**采用前须逐条核对上下文**",
        }
    enforce = cls.get("非诉执行审查（处罚已生效，企业未起诉）", 0)
    if enforce:
        stat["非诉执行审查"] = {
            "件数": enforce,
            "含义": "行政机关申请法院强制执行处罚决定，对应**企业未起诉、处罚已生效**的情形。"
                    "这类样本比行政诉讼更接近真实执法水平，罚款幅度参考价值更高",
        }
    denom = upheld + overturned
    if denom:
        stat["处罚维持比例"] = {
            "口径": "处罚被维持 /（被维持 + 被撤销或变更）",
            "值": f"{upheld / denom:.0%}",
            "分子分母": f"{upheld} / {denom}",
            "⚠️程序性排除": f"「未进入实体审查」{cls.get('未进入实体审查', 0)} 件"
                            f"（驳回起诉、不予受理、撤诉等）**已排除出分母**——"
                            f"这类裁定不代表处罚被维持",
        }
        # 样本量必须按**该比例自身的分母**判断，不能看总样本数。
        # 教训：总样本 41 件看着够，但进入实体审查的只有 5 件，
        # 「维持率 100%」是 5/5 算出来的，毫无统计意义却不会触发总量警告。
        if denom < 20:
            stat["处罚维持比例"]["⚠️样本量"] = (
                f"该比例的分母仅 {denom} 件（总样本 {len(rows)} 件中多数未进入实体审查），"
                f"**比率无统计意义，不得表述为「处罚维持率」**，只可作个案参考")
    if len(rows) < 20:
        stat["⚠️样本量"] = f"总样本仅 {len(rows)} 件，任何比率均无统计意义"

    return {
        "扫描范围": {
            "场景数": len(plan["scenes"]), "地域": plan.get("court_area") or "全国",
            "裁判日期起": plan.get("judgment_date_start"),
        },
        "检索痕迹": trace,
        "统计": stat,
        "案件": sorted(rows, key=lambda r: (r["裁判日期"] or ""), reverse=True),
        "⚠️数据来源的根本局限": [
            "**案例库是裁判文书库，不是行政处罚决定书库。**"
            "这里只能查到「被处罚人不服并提起行政诉讼」的那部分处罚——"
            "绝大多数处罚未被起诉，不在库中",
            "因此本报告是**处罚争议的画像**，不是处罚全貌，"
            "**不能用来推算被罚概率或执法密度**",
            "被诉的处罚往往是争议较大、金额较高或企业有把握推翻的，"
            "存在明显的选择偏差，罚款幅度会系统性偏离实际执法水平",
            "「驳回起诉」是程序性裁定（不符合受理条件），**不等于处罚被维持**，"
            "已排除出维持比例的分母",
            "要了解真实执法情况，应查阅监管部门公开的行政处罚决定书"
            "（信用中国、各级市场监管部门网站）",
        ],
    }


def main():
    p = argparse.ArgumentParser(description="监管处罚风险画像",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--plan", help="方案 JSON")
    p.add_argument("--scene", nargs="+", help="违规场景描述，可多个（≥100字为宜）")
    p.add_argument("--area", dest="court_area", help="法院地域")
    p.add_argument("--date-start", dest="judgment_date_start", default="20210101")
    p.add_argument("--limit", type=int, default=25)
    args = p.parse_args()

    if args.plan:
        plan = json.load(open(args.plan, encoding="utf-8"))
    elif args.scene:
        plan = {"scenes": args.scene, "court_area": args.court_area,
                "judgment_date_start": args.judgment_date_start, "limit": args.limit}
    else:
        p.error("需提供 --plan 或 --scene")

    if not plan.get("scenes"):
        sys.exit("错误：scenes 为空。须给出具体违规场景——"
                 "「食品行业被罚情况」这种粒度检不出有效结果。")

    print(json.dumps(run(plan), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
