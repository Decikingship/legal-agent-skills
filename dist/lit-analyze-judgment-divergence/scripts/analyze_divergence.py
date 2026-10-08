#!/usr/bin/env python3
"""
裁判尺度分歧分析

吃 search_similar_cases.py 的输出，按**法院层级 / 地域 / 年份**分组统计结果分布，
找出同类争点下结论不一致的地方，并抽取判决中的数值尺度（调减比例、利率、期限等）。

三条方法论红线，写死在脚本里：
  1. **分组维度绝不包含审判人员**。数据里有 judges 字段，但以法官为维度做倾向统计
     既违职业伦理，也有司法数据商用的合规风险。
  2. **不出"支持率"**。语义检索样本非随机样本，比率只能描述本次检索到的这批案例。
  3. **样本 <20 不做统计表述**，只作个案参考。

用法：
    analyze_divergence.py --input search_out.json
    analyze_divergence.py --input search_out.json --extract-numbers 违约金 调减
"""

import argparse
import json
import re
import sys
from collections import Counter, defaultdict

RT_WIN = {"全部支持", "部分支持"}
RT_LOSE = {"驳回全部诉讼请求"}
RT_PROCEDURAL = {"驳回起诉", "不予受理", "不予受理/立案", "驳回自诉/不予受理"}
RT_REVERSED = {"改判", "发回重审", "提审/指令审理", "量刑改判"}
RT_UPHOLD = {"维持原判"}
RT_NO_REASONING = {"调解", "撤诉", "终止审理", "中止审理"}

# 禁止作为分组维度的字段（合规红线）
FORBIDDEN_DIM = {"judges", "judge", "审判人员", "承办法官"}

PCT = re.compile(r"(?:百分之([一二三四五六七八九十百零〇]+)|(\d+(?:\.\d+)?)\s*%)")
MONEY = re.compile(r"(\d[\d,，]{2,})\s*元")
CN = {"零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
      "六": 6, "七": 7, "八": 8, "九": 9}


def cn2int(s):
    total = section = num = 0
    for ch in s:
        if ch in CN:
            num = CN[ch]
        elif ch == "十":
            section += (num or 1) * 10
            num = 0
        elif ch == "百":
            section += (num or 1) * 100
            num = 0
    return total + section + num


def pct_values(text, near_terms):
    """抽取靠近关键词的百分比数值——远处的百分比多是无关数字。"""
    out = []
    for m in PCT.finditer(text or ""):
        ctx = text[max(0, m.start() - 40): m.end() + 40]
        if near_terms and not any(t in ctx for t in near_terms):
            continue
        v = cn2int(m.group(1)) if m.group(1) else float(m.group(2))
        if 0 < v <= 100:
            out.append((v, ctx.strip()))
    return out


def dist(items, key):
    return dict(Counter((it.get(key) or "未知") for it in items).most_common())


def group_stats(items):
    c = Counter(str(i.get("result_type") or "未标注") for i in items)
    win = sum(v for k, v in c.items() if k in RT_WIN)
    lose = sum(v for k, v in c.items() if k in RT_LOSE)
    up = sum(v for k, v in c.items() if k in RT_UPHOLD)
    rev = sum(v for k, v in c.items() if k in RT_REVERSED)
    g = {"件数": len(items), "结果分布": dict(c.most_common()),
         "实体支持": win, "实体驳回": lose}
    if up or rev:
        # 二审「维持原判/改判」不是实体支持或驳回，单列避免被读成「无数据」
        g["上诉审"] = {"维持原判": up, "改判等": rev}
    if not (win or lose) and (up or rev):
        g["说明"] = "本组均为上诉审裁判，无一审实体结果，故实体支持/驳回为 0 属正常"
    return g


def divergence_score(groups):
    """分歧度：各组内「支持占比」的极差。极差越大，说明尺度越不统一。"""
    ratios = []
    for name, g in groups.items():
        d = g["实体支持"] + g["实体驳回"]
        if d >= 3:
            ratios.append((name, g["实体支持"] / d))
    if len(ratios) < 2:
        return None
    ratios.sort(key=lambda x: x[1])
    lo, hi = ratios[0], ratios[-1]
    return {
        "最低组": {"维度": lo[0], "支持占比": f"{lo[1]:.0%}"},
        "最高组": {"维度": hi[0], "支持占比": f"{hi[1]:.0%}"},
        "极差": f"{(hi[1] - lo[1]):.0%}",
        "说明": "极差反映本次样本内不同分组的结论离散程度，"
                "**不等于该类案件在各地的真实裁判差异**——样本有选择偏差",
    }


def analyze_issue(iss, near_terms, min_sample):
    items = [r for r in iss.get("正向", []) + iss.get("不利判例", [])
             if not r.get("blocking")]
    n = len(items)
    out = {"id": iss.get("id"), "争议焦点": iss.get("争议焦点"), "有效样本": n}

    if n == 0:
        out["结论"] = "无有效样本"
        return out

    by_level = defaultdict(list)
    by_year = defaultdict(list)
    by_court = defaultdict(list)
    for it in items:
        by_level[it.get("court_level") or "未知"].append(it)
        by_year[(it.get("judgment_date") or "")[:4] or "未知"].append(it)
        by_court[it.get("court_name") or "未知"].append(it)

    out["按审级"] = {k: group_stats(v) for k, v in sorted(by_level.items())}
    out["按年份"] = {k: group_stats(v) for k, v in sorted(by_year.items())}
    out["按法院"] = {k: group_stats(v) for k, v in
                     sorted(by_court.items(), key=lambda x: -len(x[1]))[:12]}

    out["审级间分歧"] = divergence_score(out["按审级"])
    out["年际变化"] = divergence_score(out["按年份"])

    # 上诉审推翻情况——最硬的分歧信号
    rev = [i for i in items if str(i.get("result_type") or "") in RT_REVERSED]
    up = [i for i in items if str(i.get("result_type") or "") in RT_UPHOLD]
    out["上诉审结论"] = {
        "改判/发回/提审": len(rev), "维持原判": len(up),
        "改判案例": [{"cite": i.get("cite"), "法院": i.get("court_name"),
                      "结果": i.get("result_type")} for i in rev[:8]],
        "解读": ("存在改判案例，说明下级法院在该争点上的处理曾被上级纠正。"
                 "论证时应引用改判理由本身，**不得把被推翻的下级观点当作规则**")
        if rev else "本次样本中未见改判案例",
    }

    # 数值尺度分布
    vals = []
    for it in items:
        txt = (it.get("court_opinion") or "") + (it.get("cpyz") or "")
        for v, ctx in pct_values(txt, near_terms):
            vals.append({"值": v, "cite": it.get("cite"),
                         "审级": it.get("court_level"), "上下文": ctx[:110]})
    if vals:
        # 按上下文性质粗分类。不分类直接算中位数是误导——
        # 约定违约金比例、司法解释30%标准、日利率、年利率上限是四种东西，
        # 混在一起算出的"中位数 5.65%"没有任何含义。
        cats = defaultdict(list)
        for x in vals:
            ctx = x["上下文"]
            if re.search(r"日\s*(利率|万分|千分)|每日|逐日", ctx):
                cats["日利率/日万分比"].append(x)
            elif re.search(r"年利率|LPR|贷款市场报价利率|同业拆借", ctx):
                cats["年利率/LPR"].append(x)
            elif re.search(r"超过造成的损失|以.{0,6}为标准适当减少|司法解释|第二十九条|第六十五条", ctx):
                cats["调减标准（司法解释）"].append(x)
            elif re.search(r"约定|合同总[价额款]|总价款|按.{0,8}支付.{0,6}违约金", ctx):
                cats["约定违约金比例"].append(x)
            else:
                cats["未分类"].append(x)
        out["数值尺度"] = {
            "样本数": len(vals),
            "分类": {
                k: {"件数": len(v),
                    "区间": f"{min(x['值'] for x in v)}% – {max(x['值'] for x in v)}%",
                    "取值": sorted({x["值"] for x in v})[:12]}
                for k, v in cats.items()
            },
            "明细": vals[:20],
            "⚠️不给中位数": (
                "各类数值性质不同（约定比例 / 司法解释调减标准 / 日利率 / 年利率上限），"
                "**混合计算中位数没有含义**。须先按性质分类，"
                "在同类内且样本充足时才可谈集中趋势"),
            "⚠️核对要求": "数值由正文正则抽取，分类为粗判，"
                          "**采用前必须逐条核对上下文**；样本有选择偏差，区间不代表全部裁判尺度",
        }

    if n < min_sample:
        out["⚠️样本量"] = (f"有效样本仅 {n} 件，低于 {min_sample} 件，"
                           f"**任何比率与分布均无统计意义**，只可作个案参考，"
                           f"不得表述为该类案件的裁判尺度")
    return out


def main():
    p = argparse.ArgumentParser(description="裁判尺度分歧分析")
    p.add_argument("--input", required=True, help="search_similar_cases.py 的输出 JSON")
    p.add_argument("--extract-numbers", dest="near_terms", nargs="*", default=["违约金", "调减", "损失"],
                   help="抽取百分比时要求上下文出现的关键词")
    p.add_argument("--min-sample", dest="min_sample", type=int, default=20,
                   help="低于该样本量则标注统计无意义，默认20")
    args = p.parse_args()

    try:
        data = json.load(open(args.input, encoding="utf-8"))
    except Exception as e:
        sys.exit(f"错误：输入文件读取失败 {e}")

    issues = [analyze_issue(i, args.near_terms, args.min_sample)
              for i in data.get("焦点", [])]

    print(json.dumps({
        "分组维度": ["法院层级", "地域/法院", "年份"],
        "红线": [
            "**分组维度不含审判人员**——以法官为维度做裁判倾向统计既违职业伦理，"
            "也有司法数据商用的合规风险，本脚本不提供该能力",
            "**不出「支持率」**——语义检索样本非随机样本，"
            "任何比率只能描述本次检索到的这批案例，不得表述为该类案件的胜诉率",
            "样本量不足时不做统计表述，只作个案参考",
        ],
        "焦点": issues,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
