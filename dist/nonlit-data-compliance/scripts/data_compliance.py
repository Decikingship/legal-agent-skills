#!/usr/bin/env python3
"""
数据合规专项

数据合规的义务**高度场景化**——同样是收集手机号，用于账号登录和用于精准营销，
合规要求完全不同。所以本工具不做「个保法有哪些条」，而是按场景展开：
  选场景 → 展开业务动作 → 走合规义务矩阵引擎检索 → 附加该场景的专属检查项

场景模板见 assets/scenario-templates.json，覆盖收集告知同意、存储与安全、
共享与委托、数据出境、自动化决策、个人权利响应、合规审计七类。

用法：
    data_compliance.py --scenes 收集与告知同意 数据出境 --subject 个人信息处理者
    data_compliance.py --list
"""

import argparse
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATES = os.path.normpath(os.path.join(HERE, "..", "assets", "scenario-templates.json"))
# 复用合规义务矩阵引擎，避免检索逻辑两处维护
ENGINE_CANDIDATES = [
    os.path.join(HERE, "..", "..", "nonlit-compliance-obligation-matrix",
                 "scripts", "obligation_matrix.py"),
    os.path.join(HERE, "..", "references", "obligation_matrix.py"),
]


def load_engine():
    for c in ENGINE_CANDIDATES:
        p = os.path.normpath(c)
        if os.path.exists(p):
            spec = importlib.util.spec_from_file_location("om", p)
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            return m
    return None


def main():
    p = argparse.ArgumentParser(description="数据合规专项",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--scenes", nargs="+", help="场景名，见 --list")
    p.add_argument("--subject", default="个人信息处理者",
                   help="主体称谓，须用法规用语")
    p.add_argument("--area", help="地域")
    p.add_argument("--rows", type=int, default=20)
    p.add_argument("--list", action="store_true", help="列出可用场景与主体称谓映射")
    p.add_argument("--no-penalty", action="store_true")
    p.add_argument("--penalty-top", type=int, default=4)
    p.add_argument("--include-permissive", action="store_true")
    args = p.parse_args()

    tpl = json.load(open(TEMPLATES, encoding="utf-8"))
    scenes = [k for k in tpl if not k.startswith("_")]

    if args.list:
        print(json.dumps({
            "可用场景": {k: tpl[k]["actions"] for k in scenes},
            "主体称谓": tpl["_主体称谓"],
        }, ensure_ascii=False, indent=2))
        return

    if not args.scenes:
        p.error("需指定 --scenes，可用值见 --list")
    bad = [s for s in args.scenes if s not in scenes]
    if bad:
        sys.exit(f"错误：未知场景 {bad}。可用：{scenes}")

    engine = load_engine()
    if engine is None:
        sys.exit("错误：未找到合规义务矩阵引擎 obligation_matrix.py")

    actions, checks = [], {}
    for s in args.scenes:
        actions.extend(tpl[s]["actions"])
        checks[s] = tpl[s]["检查项"]

    plan = {"subject": args.subject, "industry": "数据与个人信息保护",
            "area": args.area, "rows": args.rows, "max_per_law": 3,
            "actions": actions}

    class A:
        no_penalty = args.no_penalty
        penalty_top = args.penalty_top
        include_permissive = args.include_permissive

    result = engine.build(plan, A)
    result["数据合规场景"] = args.scenes
    result["场景专属检查项"] = checks
    result["提示"] = [
        "**数据合规的义务高度场景化。** 同样是收集手机号，用于登录和用于精准营销，"
        "合规要求完全不同——务必确认真实的处理目的，而非按功能罗列",
        "「场景专属检查项」是清单落地的抓手：义务矩阵回答「法律要求什么」，"
        "检查项回答「怎么验证做到了」",
        "⚠️ **数据出境规则调整频繁**，免申报情形、申报门槛、标准合同范本都在变，"
        "**必须核对最新规定**，不能依赖本次检索结果的时点",
        "⚠️ 行业分类过滤不可用（见检索契约 G-16），"
        "金融、医疗、汽车、征信等领域有专门的数据规定，**须另行核对主管部门规章**",
    ] + result.get("提示", [])
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
