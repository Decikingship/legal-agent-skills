#!/usr/bin/env python3
"""
构建可部署的 skill 包。

设计原则：
  conventions/  是研发期的唯一事实来源，**不部署到平台**。
  平台上每个 skill 是独立目录，跨 skill 的相对路径引用不存在，
  所以部署前必须把该 skill 实际需要的规范内容注入它自己的 references/。

  单一来源 + 构建期注入，避免了「多处维护、迟早不一致」。
  改规范只改 conventions/，重跑本脚本即可。

用法：
    python3 build_skills.py            # 产出到 dist/
    python3 build_skills.py --check    # 只校验，不写文件
"""

import argparse
import hashlib
import re
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONV = ROOT / "conventions"
SKILLS = ROOT / "skills"
DIST = ROOT / "dist"
EXPERTS = ROOT / "experts"
EXPERT_DIST = ROOT / "dist-experts"

# 每个 skill 需要注入哪些规范文件。
# 只注入真正用得到的——skill 目录越小，模型读起来越聚焦。
# 跨 skill 的脚本依赖：构建时把源脚本注入到依赖方的 references/。
# 手工复制会造成两处维护迟早不一致——与 conventions 注入同理，单一来源 + 构建期分发。
SHARED_SCRIPTS = {
    "util-contract-diff": [
        ("nonlit-review-contract", "scripts/review_contract.py"),
    ],
    "nonlit-data-compliance": [
        ("nonlit-compliance-obligation-matrix", "scripts/obligation_matrix.py"),
    ],
}

INJECT = {
    "zhihe-law-search": ["retrieval-contract.md", "output-formats.md"],
    "zhihe-case-search": ["retrieval-contract.md", "code-tables.md", "output-formats.md"],
    "core-verify-statute-validity": ["retrieval-contract.md", "output-formats.md", "time-effect.md"],
    "core-audit-case-citation": ["retrieval-contract.md", "code-tables.md", "output-formats.md"],
    "core-claim-evidence-audit": ["output-formats.md"],
    "util-legal-calculator": ["output-formats.md"],
    "lit-search-similar-cases": ["retrieval-contract.md", "code-tables.md", "output-formats.md"],
    "lit-extract-judicial-rules": ["code-tables.md", "output-formats.md"],
    "lit-analyze-judgment-divergence": ["code-tables.md", "output-formats.md"],
    "lit-analyze-law-conflict": ["retrieval-contract.md", "output-formats.md", "time-effect.md"],
    "lit-counter-adverse-precedent": ["code-tables.md", "output-formats.md"],
    "util-research-memo": ["output-formats.md"],
    "nonlit-review-contract": ["output-formats.md"],
    "nonlit-clause-benchmark": ["output-formats.md"],
    "util-contract-diff": ["output-formats.md"],
    "nonlit-compliance-obligation-matrix": ["retrieval-contract.md", "output-formats.md"],
    "nonlit-regulatory-penalty-scan": ["retrieval-contract.md", "code-tables.md", "output-formats.md"],
    "nonlit-new-rule-tracking": ["retrieval-contract.md", "output-formats.md"],
    "nonlit-data-compliance": ["retrieval-contract.md", "output-formats.md"],
    "lit-criminal-sentencing-calibration": ["retrieval-contract.md", "code-tables.md", "output-formats.md"],
    "lit-analyze-claim-basis": ["output-formats.md"],
    "lit-assess-case-risk": ["output-formats.md"],
    "lit-cross-examination": ["output-formats.md"],
    "lit-draft-complaint": ["output-formats.md"],
    "lit-draft-defense": ["output-formats.md"],
    "lit-draft-argument": ["output-formats.md"],
    "lit-draft-appeal": ["output-formats.md"],
    "lit-prepare-hearing": ["output-formats.md"],
    "lit-plan-enforcement": ["output-formats.md"],
    "lit-analyze-jurisdiction": ["retrieval-contract.md", "output-formats.md"],
    "lit-analyze-limitation": ["time-effect.md", "output-formats.md"],
    "lit-build-evidence-list": ["output-formats.md"],
    "lit-build-timeline": ["output-formats.md"],
    "lit-labor-dispute": ["output-formats.md"],
    "lit-administrative-remedy": ["output-formats.md"],
    "lit-bankruptcy": ["output-formats.md"],
    "nonlit-labor-compliance": ["output-formats.md"],
    "nonlit-admin-penalty-benchmark": ["output-formats.md"],
    "nonlit-due-diligence": ["output-formats.md"],
    "nonlit-dd-opinion": ["output-formats.md"],
    "nonlit-corporate-governance": ["output-formats.md"],
    "nonlit-transaction-documents": ["output-formats.md"],
    "nonlit-ipo-review": ["output-formats.md"],
    "lit-case-intake": ["output-formats.md"],
    "lit-opponent-background": ["retrieval-contract.md", "code-tables.md", "output-formats.md"],
    "lit-case-closing": ["output-formats.md"],
    "lit-criminal-defense": ["output-formats.md"],
    "lit-shareholder-dispute": ["output-formats.md"],
    "nonlit-litigation-response": ["output-formats.md"],
    "nonlit-new-business-review": ["retrieval-contract.md", "output-formats.md"],
    "nonlit-regulatory-inspection": ["output-formats.md"],
    "util-bid-proposal": ["output-formats.md"],
    "util-legal-training": ["output-formats.md"],
    "util-anonymize": ["output-formats.md"],
    "util-diagram": ["output-formats.md"],
    "util-case-file-index": ["output-formats.md"],
    "util-docx-format": ["output-formats.md"],
    "util-plain-language": ["output-formats.md"],
    "util-legal-translation": ["output-formats.md"],
    "util-engagement-proposal": ["output-formats.md"],
}

BANNER = (
    "<!-- 本文件由 build_skills.py 从 conventions/{name} 注入，请勿直接编辑。\n"
    "     修改请改 conventions/{name} 后重新构建。源文件校验和：{sha} -->\n\n"
)


def find_skill(name):
    hits = list(SKILLS.rglob(name))
    if not hits:
        sys.exit(f"错误：找不到 skill 目录 {name}")
    return hits[0]


def build(check_only=False):
    problems = []
    if not check_only:
        shutil.rmtree(DIST, ignore_errors=True)
        DIST.mkdir(parents=True)

    for skill_name, files in INJECT.items():
        src = find_skill(skill_name)
        skill_md = src / "SKILL.md"
        if not skill_md.exists():
            problems.append(f"{skill_name}: 缺少 SKILL.md")
            continue

        if not check_only:
            dst = DIST / skill_name
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns(
                "__pycache__", "*.pyc", ".DS_Store"))
            (dst / "references").mkdir(exist_ok=True)

        text = skill_md.read_text(encoding="utf-8")
        for f in files:
            conv_file = CONV / f
            if not conv_file.exists():
                problems.append(f"{skill_name}: conventions/{f} 不存在")
                continue
            body = conv_file.read_text(encoding="utf-8")
            sha = hashlib.sha256(body.encode()).hexdigest()[:12]
            if not check_only:
                (dst / "references" / f).write_text(
                    BANNER.format(name=f, sha=sha) + body, encoding="utf-8"
                )

        # 注入跨 skill 的脚本依赖
        for src_skill, rel in SHARED_SCRIPTS.get(skill_name, []):
            src_path = find_skill(src_skill) / rel
            if not src_path.exists():
                problems.append(f"{skill_name}: 依赖的 {src_skill}/{rel} 不存在")
                continue
            if not check_only:
                body = src_path.read_text(encoding="utf-8")
                sha = hashlib.sha256(body.encode()).hexdigest()[:12]
                (dst / "references" / src_path.name).write_text(
                    f"# 由 build_skills.py 从 {src_skill}/{rel} 注入，请勿直接编辑。\n"
                    f"# 修改请改源文件后重新构建。源文件校验和：{sha}\n" + body,
                    encoding="utf-8")

        # SKILL.md 里的 conventions/ 路径在平台上不存在，改写为 references/
        new_text = text.replace("conventions/", "references/")
        if not check_only:
            (dst / "SKILL.md").write_text(new_text, encoding="utf-8")

        # 校验：SKILL.md 引用到的文件，要么来自 conventions 注入，
        # 要么是 skill 自带的 references（如领域专属的补充材料）
        import re
        refs = set(re.findall(r"(?:conventions|references)/([\w\-.]+\.md)", text))
        own = {f.name for f in (src / "references").glob("*.md")} \
            if (src / "references").is_dir() else set()
        missing = refs - set(files) - own
        if missing:
            problems.append(
                f"{skill_name}: SKILL.md 引用了 {sorted(missing)}，但未在 INJECT 清单中，"
                f"部署后会指向不存在的文件"
            )

    # 专家清单校验：--check 阶段就查，不必等到打包
    import re as _re
    if EXPERTS.is_dir():
        for exp in sorted(EXPERTS.rglob("EXPERT.md")):
            _t = exp.read_text(encoding="utf-8")
            _m = _re.match(r"^---\n(.*?)\n---", _t, _re.S)
            if not _m:
                problems.append(f"{exp.parent.name}: EXPERT.md 缺少 frontmatter")
                continue
            _fm = _m.group(1)
            if not _re.search(r"^skills:", _fm, _re.M):
                problems.append(
                    f"{exp.parent.name}: **EXPERT.md 缺少 skills 清单**——"
                    f"专家将成为无技能的空壳")
                continue
            for _d, _n in _re.findall(r"dir:\s*(\S+)\s*\n\s*name:\s*(.+)", _fm):
                _hits = list(SKILLS.rglob(f"{_d}/SKILL.md"))
                if not _hits:
                    problems.append(f"{exp.parent.name}: dir「{_d}」找不到对应 skill")
                    continue
                _sm = _re.match(r"^---\n(.*?)\n---",
                                _hits[0].read_text(encoding="utf-8"), _re.S)
                _real = _re.search(r"^name:\s*(.+)$", _sm.group(1), _re.M).group(1).strip()
                if _real != _n.strip():
                    problems.append(
                        f"{exp.parent.name}: dir「{_d}」清单写「{_n.strip()}」，"
                        f"SKILL.md 是「{_real}」")

    if problems:
        print("发现问题：")
        for p in problems:
            print("  -", p)
        return 1

    if check_only:
        print("校验通过：无悬空引用，专家清单完整且 dir↔name 一致")
        return 0

    print(f"已构建到 {DIST}/")
    for d in sorted(DIST.iterdir()):
        files = sorted(x.relative_to(d) for x in d.rglob("*") if x.is_file())
        print(f"\n{d.name}/")
        for f in files:
            print(f"  {f}")
    return 0


def read_frontmatter(path):
    """极简 frontmatter 解析：只取顶层 key: value 与 skills 列表。"""
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    if not m:
        return None, text
    return m.group(1), m.group(2)


def pack_experts():
    """把 experts/<name>/EXPERT.md 打成 .expert（zip），技能目录从 dist/ 取。

    目录名显式来自 EXPERT.md 的 skills[].dir，不依赖平台 slugify——
    平台会把中文 name 压成 ai / ai-2，无语义且随上传顺序漂移。
    """
    if not EXPERTS.is_dir():
        print("无 experts/ 目录，跳过专家打包")
        return 0
    if not DIST.is_dir():
        print("错误：请先构建 dist/（python3 build_skills.py）")
        return 1

    shutil.rmtree(EXPERT_DIST, ignore_errors=True)
    EXPERT_DIST.mkdir(parents=True)
    problems = []

    for exp_dir in sorted(x for x in EXPERTS.iterdir() if x.is_dir()):
        md = exp_dir / "EXPERT.md"
        if not md.exists():
            problems.append(f"{exp_dir.name}: 缺少 EXPERT.md")
            continue
        fm, _ = read_frontmatter(md)
        if fm is None:
            problems.append(f"{exp_dir.name}: EXPERT.md 缺少 frontmatter")
            continue

        exp_name = ""
        for line in fm.split("\n"):
            if line.startswith("name:"):
                exp_name = line.split(":", 1)[1].strip()
                break

        # 解析 skills 列表中的 dir / name 配对
        pairs, cur = [], {}
        in_skills = False
        for line in fm.split("\n"):
            if line.startswith("skills:"):
                in_skills = True
                continue
            if in_skills:
                if line and not line.startswith((" ", "-")):
                    break
                st = line.strip()
                if st.startswith("- "):
                    if cur:
                        pairs.append(cur)
                    cur = {}
                    st = st[2:].strip()
                if ":" in st:
                    k, v = st.split(":", 1)
                    cur[k.strip()] = v.strip()
        if cur:
            pairs.append(cur)

        if not pairs:
            # 这是硬错误：专家没有技能清单，装上平台就是个空壳。
            # 教训：曾有一次批量改写 EXPERT.md 时丢掉了整个 skills 块，
            # 而 --check 当时不查这一项，直到打包才发现 29 个专家全空了。
            problems.append(
                f"{exp_dir.name}: **EXPERT.md 缺少 skills 清单**——"
                f"专家将成为无技能的空壳。检查 frontmatter 是否在改写时丢失了 skills 块")
            continue

        out = EXPERT_DIST / f"{exp_name or exp_dir.name}.expert"
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(md, "EXPERT.md")
            for pr in pairs:
                dname = pr.get("dir")
                if not dname:
                    problems.append(f"{exp_dir.name}: skills 条目缺 dir")
                    continue
                src = DIST / dname
                if not src.is_dir():
                    problems.append(
                        f"{exp_dir.name}: dir「{dname}」在 dist/ 中不存在，"
                        f"检查 INJECT 清单或拼写")
                    continue
                # 校验 name 与 SKILL.md 一致
                sk = src / "SKILL.md"
                if sk.exists() and pr.get("name"):
                    sfm, _ = read_frontmatter(sk)
                    real = ""
                    for line in (sfm or "").split("\n"):
                        if line.startswith("name:"):
                            real = line.split(":", 1)[1].strip()
                            break
                    if real and real != pr["name"]:
                        problems.append(
                            f"{exp_dir.name}: dir「{dname}」的 SKILL.md name 是"
                            f"「{real}」，与 EXPERT.md 中的「{pr['name']}」不一致")
                for f in sorted(src.rglob("*")):
                    if f.is_file():
                        z.write(f, f"skills/{dname}/{f.relative_to(src)}")
        # 额外附带一份未压缩目录，便于人工检查
        print(f"  {out.name}  ({len(pairs)} skills)")

    if problems:
        print("发现问题：")
        for p_ in problems:
            print("  -", p_)
        return 1
    print(f"已打包到 {EXPERT_DIST}/")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只校验引用完整性，不写文件")
    ap.add_argument("--expert", action="store_true",
                    help="打包 experts/ 下的专家为 .expert（需先构建 dist/）")
    a = ap.parse_args()
    if a.expert:
        sys.exit(pack_experts())
    sys.exit(build(a.check))
