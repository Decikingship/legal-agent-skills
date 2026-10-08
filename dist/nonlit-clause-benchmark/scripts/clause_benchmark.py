#!/usr/bin/env python3
"""
条款范本检索与替代方案推荐

给审查意见提供「改成什么」。三路来源：
  1. 内置条款库（assets/clause-bank.json）—— 覆盖 16 类风险点，每类三个立场版本
  2. 官方示范文本 —— 走搜法检索（住建部、市场监管总局等发布的合同示范文本）
  3. 经司法检验的表述 —— 走搜案看哪种写法被法院支持（由模型完成，脚本只给检索建议）

**范本是起点不是终点。** 直接照搬而不结合交易背景改写，是懒惰且危险的：
范本里的期限、比例、责任上限都需要按标的性质与双方地位调整。

用法：
    clause_benchmark.py --from-review review_out.json --stance 乙方
    clause_benchmark.py --rules R05 R14 --stance 平衡
    clause_benchmark.py --missing 不可抗力 通知与送达
    clause_benchmark.py --list
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BANK_PATH = os.path.join(HERE, "..", "assets", "clause-bank.json")

STANCE_KEY = {"甲方": "甲方版", "乙方": "乙方版", "中立": "平衡版", "平衡": "平衡版"}


def load():
    try:
        return json.load(open(BANK_PATH, encoding="utf-8"))
    except Exception as e:
        sys.exit(f"错误：条款库读取失败 {e}")


def pick(bank, rid, stance):
    e = bank.get(rid)
    if not e:
        return None
    key = STANCE_KEY.get(stance, "平衡版")
    out = {
        "规则": rid, "风险点": e["风险点"], "要点": e["要点"],
        "推荐版本": {"立场": key, "文本": e[key]},
        "避坑": e.get("避坑"),
    }
    # 同时给出另外两个版本供谈判时进退
    out["其他版本"] = {k: v for k, v in e.items()
                       if k.endswith("版") and k != key}
    return out


def main():
    p = argparse.ArgumentParser(description="条款范本检索与替代方案推荐",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_argument_group("输入")
    g.add_argument("--from-review", dest="from_review",
                   help="nonlit-review-contract 的输出 JSON，自动匹配命中的风险规则")
    g.add_argument("--rules", nargs="+", help="直接指定规则 ID，如 R05 R14")
    g.add_argument("--missing", nargs="+", help="按缺失条款名取范本")
    p.add_argument("--stance", choices=list(STANCE_KEY),
                   help="立场，决定推荐哪个版本。从 --from-review 读取时可省略")
    p.add_argument("--list", action="store_true", help="列出条款库覆盖的风险点")
    args = p.parse_args()

    bank = load()

    if args.list:
        print(json.dumps({
            "风险点范本": {k: v["风险点"] for k, v in bank.items()
                           if k.startswith("R")},
            "缺失条款范本": list(bank.get("_缺失条款范本", {})),
        }, ensure_ascii=False, indent=2))
        return

    stance = args.stance
    rules = []

    if args.from_review:
        d = json.load(open(args.from_review, encoding="utf-8"))
        stance = stance or d.get("审查立场")
        seen = []
        for c in d.get("条款", []):
            for h in c.get("信号", []):
                if h["规则"] not in seen:
                    seen.append(h["规则"])
        rules = seen
        missing = (d.get("缺失条款检查") or {}).get("疑似缺失") or []
    elif args.rules:
        rules = args.rules
        missing = []
    elif args.missing:
        rules, missing = [], args.missing
    else:
        p.error("需提供 --from-review、--rules 或 --missing")

    if not stance:
        sys.exit("错误：必须指定 --stance。同一风险点，甲方版与乙方版的替代条款方向相反。")

    items = [x for x in (pick(bank, r, stance) for r in rules) if x]
    mb = bank.get("_缺失条款范本", {})
    miss_out = [{"条款": m, "范本": mb[m]} for m in missing if m in mb]
    miss_none = [m for m in missing if m not in mb]

    print(json.dumps({
        "立场": stance,
        "推荐版本": STANCE_KEY[stance],
        "命中风险点": len(items),
        "替代条款": items,
        "缺失条款范本": miss_out,
        "库中无范本的缺失项": miss_none,
        "提示": [
            "**范本是起点不是终点。** 期限、比例、责任上限都须按标的性质与双方地位调整，"
            "直接照搬是懒惰且危险的",
            "同时提供了其他立场的版本——谈判时可据此判断进退空间，"
            "知道对方会怎么改，比只知道自己想怎么改更有用",
            "内置库覆盖常见风险点。**特殊行业、监管强度高的领域**"
            "（金融、医疗、数据出境、建设工程）须另行检索官方示范文本与监管要求",
            "涉及「这么写会不会被支持」的，用「智合AI搜案」看该表述在裁判中的实际处理",
            "官方合同示范文本可用「智合AI搜法」检索（住建部、市场监管总局等发布）",
        ],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
