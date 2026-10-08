#!/usr/bin/env python3
"""
卷宗解析与编目

律师接手案件时拿到的往往是一堆命名混乱的扫描件。本脚本做四件事：
  扫描目录 → 按文件名与类型分类 → 检测重复与缺失 → 生成证据目录

**编目的价值不在整理整齐，在于发现问题**：
  - 哪些文件是重复的（同一份合同扫了三遍，页码却不一样）
  - 哪些关键文件缺失（有解除通知却没有劳动合同）
  - 哪些是扫描件需要 OCR、哪些体积异常可能是空白页

用法：
    case_file_index.py --dir ./卷宗 --case-type 劳动争议
    case_file_index.py --dir ./卷宗 --output-catalog
"""
import argparse, hashlib, json, os, re, sys
from collections import defaultdict

# 文件类型 → 证据种类推定（与「证据清单与举证责任」口径一致）
TYPE_RULES = [
    (r"合同|协议|契约", "书证", "合同类"),
    (r"发票|收据|凭证|流水|对账|账单|回单", "书证", "财务凭证"),
    (r"函|通知|告知书|催告|律师函|决定书", "书证", "往来函件"),
    (r"聊天|微信|短信|邮件|截图|QQ", "电子数据", "电子数据"),
    (r"录音|录像|视频|监控|音频", "视听资料", "视听资料"),
    (r"证言|证人|情况说明", "证人证言", "证人材料"),
    (r"鉴定|评估|审计|检测", "鉴定意见", "鉴定材料"),
    (r"判决|裁定|调解书|仲裁裁决|裁决书", "书证", "生效法律文书"),
    (r"营业执照|身份证|户口|工商|登记|章程", "书证", "主体资格材料"),
    (r"起诉状|答辩状|代理词|上诉状|申请书", "书证", "诉讼文书"),
    (r"照片|现场|图片", "物证/书证", "现场材料"),
]
# 按案由推定的必备材料
# 必备材料用**关键词组**匹配而非整名匹配。
# 教训：初版拿「工资发放记录」整串去文件名里找，卷宗里明明有「工资流水」却报缺失——
# 误报会让律师对整份缺失清单失去信任。
REQUIRED = {
    "劳动争议": [
        ("劳动合同", ["劳动合同", "聘用合同", "用工协议"]),
        ("解除/终止通知", ["解除", "终止", "辞退", "离职"]),
        ("工资发放记录", ["工资", "薪资", "薪酬", "报酬", "流水"]),
        ("考勤记录", ["考勤", "打卡", "出勤"]),
        ("社保缴纳记录", ["社保", "社会保险", "参保"]),
        ("规章制度及公示证据", ["规章", "制度", "员工手册", "公示"]),
        ("仲裁裁决书/不予受理通知", ["仲裁", "裁决", "不予受理"]),
    ],
    "买卖合同": [
        ("买卖合同", ["买卖", "购销", "采购", "订单"]),
        ("送货/验收单", ["送货", "交货", "验收", "签收"]),
        ("发票", ["发票"]),
        ("付款凭证", ["付款", "转账", "汇款", "流水", "回单"]),
        ("催告函", ["催告", "催款", "律师函", "催收"]),
    ],
    "借款合同": [
        ("借款合同/借条", ["借款", "借条", "欠条", "借据"]),
        ("转账凭证", ["转账", "汇款", "流水", "回单", "付款"]),
        ("催收记录", ["催收", "催告", "律师函"]),
        ("担保文件", ["担保", "保证", "抵押", "质押"]),
    ],
    "建设工程": [
        ("施工合同", ["施工", "承包", "工程合同"]),
        ("中标通知书", ["中标", "招标", "投标"]),
        ("工程量签证", ["签证", "工程量", "变更单"]),
        ("竣工验收文件", ["竣工", "验收"]),
        ("结算文件", ["结算", "决算"]),
        ("付款凭证", ["付款", "转账", "进度款", "流水"]),
    ],
    "股权转让": [
        ("股权转让协议", ["股权转让", "股份转让", "转股"]),
        ("工商变更登记", ["工商", "变更", "登记", "章程"]),
        ("付款凭证", ["付款", "转账", "汇款", "流水"]),
        ("股东会决议", ["股东会", "决议", "董事会"]),
        ("公司章程", ["章程"]),
    ],
}
SCAN_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}
DOC_EXT = {".pdf", ".doc", ".docx", ".txt", ".md"}


def classify(name):
    for pat, kind, cat in TYPE_RULES:
        if re.search(pat, name):
            return kind, cat
    return "待确定", "未分类"


def file_hash(path, blocks=8):
    """抽样哈希：只读首尾若干块，大扫描件不必全读。"""
    h, size = hashlib.md5(), os.path.getsize(path)
    with open(path, "rb") as f:
        h.update(f.read(65536))
        if size > 131072:
            f.seek(-65536, os.SEEK_END)
            h.update(f.read(65536))
    h.update(str(size).encode())
    return h.hexdigest()[:16]


def main():
    p = argparse.ArgumentParser(description="卷宗解析与编目",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dir", required=True, help="卷宗目录")
    p.add_argument("--case-type", dest="case_type",
                   help=f"案由，用于缺失检查。可选：{list(REQUIRED)}")
    p.add_argument("--min-size", dest="min_size", type=int, default=20480,
                   help="小于该字节数的文件标为可疑（可能是空白页），默认20KB")
    p.add_argument("--output-catalog", dest="catalog", action="store_true",
                   help="额外输出可直接提交的证据目录表格")
    args = p.parse_args()

    root = args.dir
    if not os.path.isdir(root):
        sys.exit(f"错误：目录不存在 {root}")

    files, by_hash = [], defaultdict(list)
    for dirpath, _, names in os.walk(root):
        for n in sorted(names):
            if n.startswith("."):
                continue
            fp = os.path.join(dirpath, n)
            ext = os.path.splitext(n)[1].lower()
            size = os.path.getsize(fp)
            kind, cat = classify(n)
            h = file_hash(fp)
            by_hash[h].append(os.path.relpath(fp, root))
            flags = []
            if ext in SCAN_EXT:
                flags.append("扫描件，**须 OCR 后方可检索与引用**")
            if size < args.min_size:
                flags.append(f"体积仅 {size//1024}KB，**可能是空白页或扫描失败**")
            if kind == "待确定":
                flags.append("**文件名无法判断类型**，须打开确认")
            files.append({
                "文件": os.path.relpath(fp, root), "大小KB": size // 1024,
                "推定证据种类": kind, "分类": cat, "_hash": h, "标记": flags,
            })

    dups = [{"疑似重复": v, "份数": len(v)} for v in by_hash.values() if len(v) > 1]
    for f in files:
        f.pop("_hash")

    by_cat = defaultdict(list)
    for f in files:
        by_cat[f["分类"]].append(f["文件"])

    missing = []
    if args.case_type and args.case_type in REQUIRED:
        allnames = " ".join(f["文件"] for f in files)
        missing = [label for label, keys in REQUIRED[args.case_type]
                   if not any(k in allnames for k in keys)]

    scans = [f for f in files if any("扫描件" in x for x in f["标记"])]
    suspicious = [f for f in files if any("空白页" in x for x in f["标记"])]
    unknown = [f for f in files if f["推定证据种类"] == "待确定"]

    notes = [
        "**编目的价值不在整理整齐，在于发现问题**——重复件、缺失件、空白页、"
        "无法判断类型的文件，这些才是要处理的",
        "推定证据种类仅按**文件名**判断，须打开核对。"
        "「考勤记录微信截图」是电子数据不是书证，形式要件要求完全不同",
        "扫描件须 OCR 后方可全文检索与引用；**OCR 结果须校对**，"
        "法律文书中的数字与人名识别错误后果严重",
    ]
    if dups:
        notes.insert(0, f"⚠️ {len(dups)} 组疑似重复文件。"
                        f"**同一份材料重复提交会被对方质疑证据组织混乱**，须合并或说明差异")
    if suspicious:
        notes.insert(0, f"⚠️ {len(suspicious)} 个文件体积异常小，可能是空白页或扫描失败")
    if missing:
        notes.insert(0, f"⚠️ 按「{args.case_type}」案由，**疑似缺失 {len(missing)} 类关键材料**")

    out = {
        "卷宗目录": root, "文件总数": len(files),
        "分类统计": {k: len(v) for k, v in sorted(by_cat.items(), key=lambda x: -len(x[1]))},
        "疑似缺失材料": missing or ("未指定案由，跳过缺失检查" if not args.case_type else "无"),
        "疑似重复": dups or "无",
        "体积异常": [f["文件"] for f in suspicious] or "无",
        "类型待确认": [f["文件"] for f in unknown] or "无",
        "需OCR": len(scans),
        "文件清单": files,
        "提示": notes,
    }
    if args.catalog:
        out["证据目录（待补证明对象与页码）"] = [
            {"序号": i + 1, "证据名称": os.path.basename(f["文件"]),
             "证据种类": f["推定证据种类"], "证明对象": "<待补>",
             "页码": "<待补>", "原件是否在手": "<待补>"}
            for i, f in enumerate(files)]
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
