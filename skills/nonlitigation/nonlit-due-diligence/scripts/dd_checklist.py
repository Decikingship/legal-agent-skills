#!/usr/bin/env python3
"""
尽职调查清单生成

按交易类型选取模块，输出核查事项与**红旗问题**清单。

**清单是起点不是终点。** 尽调质量的差异不在清单长短，在于对红旗问题的追查深度——
发现「存在代持」只是开始，要查到代持的原因、还原的可行性、有无税务风险才算做完。

用法：
    dd_checklist.py --type 股权收购 --industry 互联网 --extra 数据合规
    dd_checklist.py --list
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.normpath(os.path.join(HERE, "..", "assets", "dd-checklist.json"))
INDUSTRY_HINT = {
    "互联网": ["数据与个人信息合规", "算法与内容合规", "ICP/EDI 等增值电信业务许可"],
    "医疗": ["医疗器械/药品注册与生产许可", "健康医疗数据合规", "医疗广告合规"],
    "金融": ["金融牌照与备案", "反洗钱", "适当性管理", "数据出境"],
    "教育": ["办学许可", "预付费监管", "未成年人保护"],
    "制造": ["环保排污许可", "安全生产", "特种设备", "危化品"],
    "食品": ["食品生产经营许可", "标签标识", "溯源"],
    "房地产": ["土地出让与规划许可", "预售许可", "工程质量"],
}


def main():
    p = argparse.ArgumentParser(description="尽职调查清单生成",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--type", dest="deal_type", help="交易类型")
    p.add_argument("--industry", help="标的行业，用于提示特殊模块")
    p.add_argument("--extra", nargs="*", default=[], help="额外补充的模块名")
    p.add_argument("--list", action="store_true", help="列出可用交易类型与模块")
    args = p.parse_args()

    d = json.load(open(DATA, encoding="utf-8"))
    types, mods = d["_交易类型模块"], d["_模块清单"]

    if args.list:
        print(json.dumps({"交易类型": types, "全部模块": list(mods),
                          "行业提示": list(INDUSTRY_HINT)}, ensure_ascii=False, indent=2))
        return
    if not args.deal_type:
        p.error("需指定 --type，可用值见 --list")
    if args.deal_type not in types:
        sys.exit(f"错误：未知交易类型「{args.deal_type}」。可用：{list(types)}")

    selected = types[args.deal_type] + [m for m in args.extra if m in mods]
    out, all_flags = [], []
    for m in selected:
        if m not in mods:
            continue
        out.append({"模块": m, "核查事项": mods[m]["核查事项"],
                    "红旗问题": mods[m]["红旗"]})
        all_flags += [f"[{m}] {x}" for x in mods[m]["红旗"]]

    hint = INDUSTRY_HINT.get(args.industry or "", [])
    notes = [
        "**清单是起点不是终点。** 尽调质量的差异不在清单长短，在于对红旗问题的追查深度——"
        "发现「存在代持」只是开始，要查到原因、还原可行性、税务风险才算做完",
        "**红旗问题优先。** 时间与资源永远不够，先把红旗查透，再补常规项",
        "核查事项须落到**具体文件**：不是「核查股权结构」，而是「调取工商档案、章程、"
        "历次股东会决议、股权转让协议及付款凭证」",
        "⚠️ **行业特殊模块本清单不覆盖**，须按标的行业另行检索主管部门规章与许可要求",
    ]
    notes.append(f"「{args.industry}」行业建议补充：{'、'.join(hint)}" if hint
                 else "未指定行业或无内置提示，**须自行确认该行业的特殊许可与监管要求**")

    print(json.dumps({
        "交易类型": args.deal_type, "标的行业": args.industry or "未指定",
        "模块数": len(out), "清单": out,
        "红旗问题汇总（优先查这些）": all_flags,
        "行业特殊模块提示": hint or "须自行确认",
        "提示": notes,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
