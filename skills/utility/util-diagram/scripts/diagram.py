#!/usr/bin/env python3
"""
法律结构图生成

把股权结构、交易结构、时间轴、法律关系画成 Mermaid 图。

**为什么值得单独做**：法律关系用文字描述三段还看不明白，一张图两秒就懂。
尽调报告、交易方案、诉讼代理词里的多方关系，图比文字有效得多。

四种图各有适用：
  equity      股权结构 —— 多层持股、代持、一致行动
  deal        交易结构 —— 交易前后的股权变化与资金流
  timeline    事实时间轴 —— 与「案件事实时间轴」的产出对接
  relation    法律关系 —— 多方主体之间的合同与债权债务关系

用法：
    diagram.py --type equity --node "创始人:个人" --node "持股平台:有限合伙" \
        --edge "创始人|持股平台|GP 1%" --edge "持股平台|目标公司|15%"
    diagram.py --type timeline --from-timeline timeline_out.json
"""
import argparse, json, re, sys

SHAPE = {
    "个人": ('(["', '"])'), "自然人": ('(["', '"])'),
    "公司": ('["', '"]'), "有限公司": ('["', '"]'), "股份公司": ('["', '"]'),
    "有限合伙": ('[/"', '"/]'), "合伙企业": ('[/"', '"/]'),
    "境外主体": ('[("', '")]'), "SPV": ('[("', '")]'),
    "标的": ('{{"', '"}}'), "目标公司": ('{{"', '"}}'),
    "法院": ('[["', '"]]'), "机构": ('[["', '"]]'),
}
DEFAULT_SHAPE = ('["', '"]')


def nid(name, seen):
    """生成合法的 Mermaid 节点 ID——中文与特殊字符不能直接做 ID。"""
    if name in seen:
        return seen[name]
    i = f"N{len(seen)}"
    seen[name] = i
    return i


def esc(t):
    # Mermaid 标签里的引号与方括号会破坏语法
    return re.sub(r'["\[\]{}|]', "", str(t or "")).strip()


def build_graph(nodes, edges, direction="TB"):
    seen, lines = {}, [f"graph {direction}"]
    for spec in nodes:
        parts = spec.split(":")
        name = esc(parts[0])
        kind = parts[1].strip() if len(parts) > 1 else ""
        note = parts[2].strip() if len(parts) > 2 else ""
        pre, suf = SHAPE.get(kind, DEFAULT_SHAPE)
        label = name + (f"<br/><small>{esc(note)}</small>" if note else "")
        lines.append(f"    {nid(name, seen)}{pre}{label}{suf}")
    for spec in edges:
        parts = spec.split("|")
        if len(parts) < 2:
            sys.exit(f"错误：边格式应为 「起点|终点|标签」，收到「{spec}」")
        a, b = esc(parts[0]), esc(parts[1])
        lab = esc(parts[2]) if len(parts) > 2 else ""
        style = parts[3].strip() if len(parts) > 3 else "solid"
        arrow = {"solid": "-->", "dashed": "-.->", "thick": "==>",
                 "none": "---"}.get(style, "-->")
        ia, ib = nid(a, seen), nid(b, seen)
        lines.append(f"    {ia} {arrow}{f'|{lab}|' if lab else ''} {ib}")
    return "\n".join(lines)


def build_timeline(events):
    lines = ["timeline"]
    cur = None
    for e in events:
        dt = str(e.get("日期") or e.get("date") or "")
        y = dt[:4] if dt else "未知"
        fact = esc(e.get("事实") or e.get("fact") or "")[:40]
        ev = esc(e.get("证据") or e.get("evidence") or "")
        mark = "" if ev else "（无证据）"
        if y != cur:
            lines.append(f"    section {y}")
            cur = y
        lines.append(f"        {dt[5:] or dt} : {fact}{mark}")
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description="法律结构图生成",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--type", required=True,
                   choices=["equity", "deal", "timeline", "relation"])
    p.add_argument("--node", action="append", default=[],
                   help='节点，格式 "名称:类型:备注"。类型：个人/公司/有限合伙/境外主体/目标公司/机构')
    p.add_argument("--edge", action="append", default=[],
                   help='边，格式 "起点|终点|标签|线型"。线型：solid/dashed/thick/none')
    p.add_argument("--from-timeline", dest="from_timeline",
                   help="读取「案件事实时间轴」的输出 JSON")
    p.add_argument("--direction", default="TB", choices=["TB", "LR", "BT", "RL"])
    p.add_argument("--title", default="")
    args = p.parse_args()

    if args.type == "timeline":
        if args.from_timeline:
            d = json.load(open(args.from_timeline, encoding="utf-8"))
            events = d.get("时间轴") or []
        else:
            events = [{"日期": n.split(":")[0], "事实": n.split(":", 1)[1] if ":" in n else n}
                      for n in args.node]
        if not events:
            sys.exit("错误：时间轴需要 --from-timeline 或 --node 提供事件")
        code = build_timeline(events)
    else:
        if not args.node:
            sys.exit("错误：需要 --node 定义节点")
        direction = args.direction if args.type != "deal" else "LR"
        code = build_graph(args.node, args.edge, direction)

    tips = {
        "equity": ["持股比例标在边上，代持关系用 dashed 线并注明「代持」",
                   "**一致行动关系用 none 线连接**，避免与持股关系混淆",
                   "多层架构自上而下画，最终控制人在最顶层"],
        "deal": ["交易前后各画一张，或用不同颜色区分",
                 "资金流与股权流分开标注，避免箭头方向混淆",
                 "SPV、过桥主体要标明设立地与性质"],
        "timeline": ["无证据支撑的节点已标注「（无证据）」——**这是时间轴最该被看见的信息**",
                     "与「案件事实时间轴」的产出直接对接，不必重新录入"],
        "relation": ["合同关系与侵权关系用不同线型",
                     "债权债务标明方向与金额",
                     "多方关系中先画核心争议双方，再补外围"],
    }
    print(json.dumps({
        "图类型": args.type,
        "mermaid": code,
        "使用说明": "复制 mermaid 代码块到支持 Mermaid 的编辑器渲染；"
                    "或用 mermaid-cli 导出为图片嵌入文书",
        "提示": tips[args.type] + [
            "**图是为了让人两秒看懂，不是为了展示复杂度。** "
            "节点超过 15 个考虑拆分或分层",
            "对外文书中的结构图**同样需要脱敏**——图里的公司名、人名一样会泄露",
        ],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
