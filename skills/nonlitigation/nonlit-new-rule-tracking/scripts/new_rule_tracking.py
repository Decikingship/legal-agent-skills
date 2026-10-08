#!/usr/bin/env python3
"""
新规追踪与影响评估

回答三个问题，缺一不可：**变了什么 / 影响哪些业务 / 要改哪份文件**。
只列出「近期出台了哪些法规」是资讯，不是法律服务——律师和法务要的是后两问。

实测处理的两个问题：
  1. 切片检索会把同一法规的多个条文重复返回（实测一次检索同一部规章出现 6 次），
     新规追踪应**按法规聚合**而非按条文罗列
  2. 已发布未施行的规定最有价值——留出的正是整改窗口期，须单独标出

用法：
    new_rule_tracking.py --topics "网络交易平台经营者的主体责任与监管要求" \
        --implement-from 20250101 --area "全国 上海"
    new_rule_tracking.py --plan plan.json
"""

import argparse
import json
import os
import re
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime

BASE_URL = os.environ.get(
    "ZHIHE_BASE_URL",
    "https://your-legal-api.example.com").rstrip("/")
ZHIHE_TOKEN = os.environ.get("ZHIHE_TOKEN", "")
DETAIL = "https://www.zhiexa.com/fuxi/legislation/detail?id={}"
EP2 = "/law/vector2"

HTML_TAG = re.compile(r"<[^>]{1,80}>")
REV_TAIL = re.compile(r"[(（][^（）()]{0,30}(修订|修正|修改)[^（）()]{0,10}[)）]\s*$")
DRAFT = re.compile(r"草案|征求意见稿|送审稿")
NON_NORMATIVE = re.compile(r"(复函|批复|答复|请示|解读|问答|新闻发布|典型案例)")
# 义务性条文——影响评估要落到这些条上
OBLIGATION = re.compile(r"(应当|必须|不得|禁止)")


def auth_headers():
    h = {"Content-Type": "application/json"}
    if ZHIHE_TOKEN:
        h["Authorization"] = ZHIHE_TOKEN if ZHIHE_TOKEN.lower().startswith("bearer ") \
            else f"Bearer {ZHIHE_TOKEN}"
    return h


def post(body, timeout=90):
    req = urllib.request.Request(
        f"{BASE_URL}{EP2}", data=json.dumps(body).encode("utf-8"),
        headers=auth_headers(), method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = json.loads(r.read().decode("utf-8"))
    if str(raw.get("code", "200")) != "200":
        raise RuntimeError(f"上游错误 code={raw.get('code')} msg={raw.get('msg')}")
    return [x for x in ((raw.get("data") or {}).get("result") or []) if x.get("lawId")]


def clean(t):
    return HTML_TAG.sub("", t or "").strip()


def base_name(n):
    return REV_TAIL.sub("", clean(n)).strip()


def norm_date(d):
    x = re.sub(r"\D", "", str(d or ""))
    return x[:8] if len(x) >= 8 else (x + "01" if len(x) == 6 else None)


def run(plan):
    trace, laws = [], defaultdict(lambda: {"条文": []})
    today = datetime.now().strftime("%Y%m%d")

    for topic in plan["topics"]:
        body = {"vector": topic, "rows": plan.get("rows", 30),
                "lawstatexlsFacet": "1",
                "maxSingleLawItemCount": plan.get("max_per_law", 5)}
        for k_in, k_out in (("implement_from", "implement_date_start"),
                            ("implement_to", "implement_date_end"),
                            ("publish_from", "publish_date_start"),
                            ("publish_to", "publish_date_end")):
            if plan.get(k_in):
                body[k_out] = plan[k_in]
        if plan.get("area"):
            body["areaFacet"] = plan["area"]
        if plan.get("xls"):
            body["xls"] = plan["xls"]
        try:
            hits = post(body)
        except Exception as e:
            trace.append(f"[{topic[:26]}…] 检索失败：{e}")
            continue
        trace.append(f"[{topic[:26]}…] 命中条文 {len(hits)} 条")
        for h in hits:
            name = clean(h.get("lawName"))
            if DRAFT.search(name) or NON_NORMATIVE.search(name):
                continue
            bn = base_name(name)
            e = laws[bn]
            e["法规全称"] = name
            e["lawId"] = h["lawId"]
            e["链接"] = DETAIL.format(h["lawId"])
            e["位阶"] = h.get("xls")
            e["发文机关"] = h.get("issuingOrgan")
            e["发布日"] = norm_date(h.get("releaseYearMonthDate"))
            e["施行日"] = norm_date(h.get("implementYearMonthDate"))
            e["有历史版本"] = bool(h.get("hisgroup"))
            c = h.get("content") or ""
            if OBLIGATION.search(c):
                e["条文"].append({"条号": h.get("rawnumber"), "正文": c[:300]})

    out = []
    for bn, e in laws.items():
        if not e.get("lawId"):
            continue
        imp = e.get("施行日") or ""
        status = ("已发布未施行" if imp > today else "已施行") if imp else "施行日不明"
        days = None
        if imp and imp > today:
            try:
                days = (datetime.strptime(imp, "%Y%m%d")
                        - datetime.strptime(today, "%Y%m%d")).days
            except ValueError:
                days = None
        e2 = {k: v for k, v in e.items() if k != "条文"}
        e2["法规简称"] = bn
        e2["状态"] = status
        if days is not None:
            e2["距施行天数"] = days
            e2["整改窗口"] = f"还有 {days} 天，**这是整改窗口期，过期即需合规**"
        e2["义务性条文"] = e["条文"][:8]
        e2["义务条数"] = len(e["条文"])
        if e.get("有历史版本"):
            e2["⚠️修订提示"] = ("该法规存在历史版本，属**修订**而非新增。"
                                "影响评估须做新旧条文对比——只看新版看不出「变了什么」")
        out.append(e2)

    out.sort(key=lambda x: (0 if x["状态"] == "已发布未施行" else 1,
                            x.get("施行日") or "99999999"))
    pending = [x for x in out if x["状态"] == "已发布未施行"]

    return {
        "追踪范围": {
            "主题数": len(plan["topics"]),
            "施行日期起": plan.get("implement_from"),
            "发布日期起": plan.get("publish_from"),
            "地域": plan.get("area") or "全国",
            "基准日": today,
        },
        "检索痕迹": trace,
        "汇总": {"涉及法规": len(out), "已发布未施行": len(pending),
                 "已施行": len(out) - len(pending)},
        "法规": out,
        "提示": [
            "**新规追踪的产出必须回答三问：变了什么 / 影响哪些业务 / 要改哪份文件。**"
            "只列出「近期出台了哪些法规」是资讯，不是法律服务",
            "「已发布未施行」的最有价值——留出的正是**整改窗口期**，应优先排期",
            "标注「修订」的法规须做**新旧条文对比**：只看新版看不出变化，"
            "而客户要知道的恰恰是哪里变了。可用「智合AI搜法」按 hisgroup 取历史版本",
            "本工具按主题语义检索，**新出台但表述与主题差异大的规定可能漏检**；"
            "重要业务线建议同时订阅主管部门官网与政府公报",
            "地方性法规差异显著，未指定 area 时仅覆盖全国性规定",
        ],
    }


def main():
    p = argparse.ArgumentParser(description="新规追踪与影响评估",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--plan", help="方案 JSON")
    p.add_argument("--topics", nargs="+", help="业务主题，可多个")
    p.add_argument("--implement-from", dest="implement_from", help="施行日起 YYYYMMDD")
    p.add_argument("--implement-to", dest="implement_to")
    p.add_argument("--publish-from", dest="publish_from", help="发布日起 YYYYMMDD")
    p.add_argument("--area")
    p.add_argument("--rows", type=int, default=30)
    args = p.parse_args()

    if args.plan:
        plan = json.load(open(args.plan, encoding="utf-8"))
    elif args.topics:
        plan = {k: v for k, v in vars(args).items() if v is not None and k != "plan"}
    else:
        p.error("需提供 --plan 或 --topics")
    if not plan.get("topics"):
        sys.exit("错误：topics 为空")
    if not any(plan.get(k) for k in ("implement_from", "publish_from",
                                     "implement_to", "publish_to")):
        sys.exit("错误：必须指定日期范围（--implement-from 或 --publish-from），"
                 "否则返回的是全量法规而非新规。")

    print(json.dumps(run(plan), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
