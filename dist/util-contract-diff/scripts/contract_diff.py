#!/usr/bin/env python3
"""
合同版本比对（立场感知）

不是普通 diff。普通 diff 只告诉你「哪几个字变了」，律师真正要的是：
  **这次改动对我方是变好还是变坏？哪些是实质变更、哪些只是文字调整？**

所以本脚本在文本比对之外，对改动前后各跑一遍风险信号扫描，
按立场比较信号增减——「异议期从30日改回3日」这类回稿伎俩会被直接标出来。

用法：
    contract_diff.py --old v1.txt --new v2.txt --stance 乙方
    contract_diff.py --old v1.txt --new v2.txt --stance 甲方 --show-cosmetic
"""

import argparse
import difflib
import importlib.util
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
# 复用合同审查的条款切分与风险规则，避免两处维护出现口径不一致。
# 部署为独立 skill 时按同级 skills/ 目录查找。
CANDIDATES = [
    os.path.join(HERE, "..", "..", "..", "nonlitigation",
                 "nonlit-review-contract", "scripts", "review_contract.py"),
    os.path.join(HERE, "..", "..", "nonlit-review-contract",
                 "scripts", "review_contract.py"),
    os.path.join(HERE, "..", "references", "review_contract.py"),
]


def load_review():
    for c in CANDIDATES:
        pth = os.path.normpath(c)
        if os.path.exists(pth):
            spec = importlib.util.spec_from_file_location("rv", pth)
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            return m
    return None


# 纯文字性调整：不改变权利义务，默认折叠
COSMETIC = re.compile(r"^[\s，。、；：\"'（）()《》\-—…·\d]*$")
# 实质性词汇：出现变化必属实质变更
SUBSTANTIVE = re.compile(
    r"(应当|应|须|不得|有权|可以|无需|概不|免除|承担|赔偿|违约金|定金|"
    r"解除|终止|变更|验收|异议|保密|竞业|管辖|仲裁|诉讼|知识产权|"
    r"日|天|月|年|%|百分之|万元|元)")


def norm(t):
    return re.sub(r"[\s\u3000]", "", t or "")


def clause_map(m, text):
    if m:
        return {c["标题"]: c for c in m.split_clauses(text)}
    return {ln.strip()[:40]: {"标题": ln.strip()[:40], "正文": ln.strip()}
            for ln in text.split("\n") if ln.strip()}


def word_diff(a, b):
    sm = difflib.SequenceMatcher(None, a, b)
    dele, ins = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "delete") and a[i1:i2].strip():
            dele.append(a[i1:i2])
        if tag in ("replace", "insert") and b[j1:j2].strip():
            ins.append(b[j1:j2])
    return dele, ins, sm.ratio()


def scan_signals(m, text, stance):
    if not m:
        return {}
    out = {}
    for r in m.RULES:
        mt = re.search(r["pat"], text)
        if not mt:
            continue
        sent = m.sentence_of(text, mt.start())
        lvl, side, _ = m.adapt(r, sent, stance)
        out[r["id"]] = (r["name"], lvl, side)
    return out


ORDER = {"高": 3, "中": 2, "低": 1}


def judge_impact(before, after):
    added = {k: v for k, v in after.items() if k not in before}
    removed = {k: v for k, v in before.items() if k not in after}
    worsened, improved = [], []

    # 效力瑕疵类（或裁或审、定金违约金并用等）不分立场：
    # 新增是坏事、移除是好事，与我方站哪边无关。
    # 教训：初版按「是否含"不利于我方"」二分，把「移除或裁或审条款」判成了变差。
    def is_defect(side):
        return "双方" in side or "效力瑕疵" in side

    for name, lvl, side in added.values():
        bad = is_defect(side) or "不利于我方" in side
        (worsened if bad else improved).append(
            f"新增「{name}」信号（{lvl}，{side}）")
    for name, lvl, side in removed.values():
        good = is_defect(side) or "不利于我方" in side
        (improved if good else worsened).append(
            f"移除「{name}」信号（原 {lvl}，{side}）")
    for k in set(before) & set(after):
        n0, l0, _ = before[k]
        n1, l1, s1 = after[k]
        if ORDER.get(l1, 0) > ORDER.get(l0, 0):
            worsened.append(f"「{n1}」等级由 {l0} 升至 {l1}（{s1}）")
        elif ORDER.get(l1, 0) < ORDER.get(l0, 0):
            improved.append(f"「{n1}」等级由 {l0} 降至 {l1}（{s1}）")

    if worsened and improved:
        v = "⚠️ 有得有失，须逐项权衡"
    elif worsened:
        v = "🔴 对我方变差"
    elif improved:
        v = "🟢 对我方变好"
    else:
        v = "⚪ 风险信号无变化"
    return {"判定": v, "变差": worsened, "变好": improved}


def main():
    p = argparse.ArgumentParser(description="合同版本比对（立场感知）",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--old", required=True, help="旧版本（如我方发出稿）")
    p.add_argument("--new", required=True, help="新版本（如对方回稿）")
    p.add_argument("--stance", choices=["甲方", "乙方", "中立"], required=True,
                   help="立场。**必填**——同一处改动对甲乙双方意义相反")
    p.add_argument("--show-cosmetic", action="store_true",
                   help="展示纯文字性调整（默认折叠）")
    args = p.parse_args()

    m = load_review()
    old = open(args.old, encoding="utf-8").read()
    new = open(args.new, encoding="utf-8").read()
    co, cn = clause_map(m, old), clause_map(m, new)

    changed, added, deleted, cosmetic = [], [], [], []

    # 标题匹配不上的按相似度配对——对方常在回稿时改条款标题
    unmatched_new = [k for k in cn if k not in co]
    unmatched_old = [k for k in co if k not in cn]
    pairs = [(k, k) for k in co if k in cn]
    for nk in list(unmatched_new):
        best, score = None, 0.0
        for ok_ in unmatched_old:
            r = difflib.SequenceMatcher(None, norm(co[ok_]["正文"]),
                                        norm(cn[nk]["正文"])).ratio()
            if r > score:
                best, score = ok_, r
        if best and score >= 0.55:
            pairs.append((best, nk))
            unmatched_old.remove(best)
            unmatched_new.remove(nk)

    for ok_, nk in pairs:
        a, b = co[ok_]["正文"], cn[nk]["正文"]
        if norm(a) == norm(b):
            continue
        dele, ins, ratio = word_diff(a, b)
        blob = "".join(dele + ins)
        is_sub = bool(SUBSTANTIVE.search(blob)) and not COSMETIC.match(blob)
        rec = {"旧条款": ok_, "新条款": nk if nk != ok_ else None,
               "相似度": round(ratio, 3),
               "删除": [d[:120] for d in dele][:6],
               "新增": [i[:120] for i in ins][:6]}
        if not is_sub:
            cosmetic.append(rec)
            continue
        rec["影响"] = judge_impact(scan_signals(m, a, args.stance),
                                   scan_signals(m, b, args.stance))
        rec["旧全文"] = a[:500]
        rec["新全文"] = b[:500]
        changed.append(rec)

    for nk in unmatched_new:
        sig = scan_signals(m, cn[nk]["正文"], args.stance)
        added.append({"条款": nk, "正文": cn[nk]["正文"][:500],
                      "风险信号": [f"{v[0]}（{v[1]}，{v[2]}）" for v in sig.values()]
                      or ["未命中信号"]})
    for ok_ in unmatched_old:
        sig = scan_signals(m, co[ok_]["正文"], args.stance)
        deleted.append({"条款": ok_, "正文": co[ok_]["正文"][:500],
                        "原风险信号": [f"{v[0]}（{v[1]}，{v[2]}）" for v in sig.values()]
                        or ["未命中信号"]})

    worse = sum(1 for c in changed if "变差" in c["影响"]["判定"])
    better = sum(1 for c in changed if "变好" in c["影响"]["判定"])
    mixed = sum(1 for c in changed if "有得有失" in c["影响"]["判定"])

    notes = [
        f"立场：**{args.stance}**。所有影响判定按此立场——换立场须重跑，判定是反的",
        "**新增与删除的条款最容易被忽略。** 逐字比对时注意力都在改动的字上，"
        "对方悄悄删掉一条对其不利的条款，很难被注意到",
        "风险信号无变化 ≠ 改动无影响。金额、期限、标的范围的调整不产生信号，"
        "但可能正是本次谈判最关键的让步——**这类改动必须靠人读**",
    ]
    if not m:
        notes.insert(0, "⚠️ 未能加载合同审查规则，本次仅做文本比对，无立场影响判定")
    if cosmetic and not args.show_cosmetic:
        notes.append(f"已折叠 {len(cosmetic)} 处纯文字性调整（--show-cosmetic 展开）")

    out = {
        "立场": args.stance,
        "汇总": {"实质修改": len(changed), "其中对我方变差": worse,
                 "变好": better, "有得有失": mixed,
                 "新增条款": len(added), "删除条款": len(deleted),
                 "文字性调整": len(cosmetic)},
        "提示": notes,
        "实质修改": changed,
        "新增条款": added,
        "删除条款": deleted,
    }
    if args.show_cosmetic:
        out["文字性调整"] = cosmetic
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
