#!/usr/bin/env python3
"""
应诉应急响应（企业侧）

企业收到起诉状、律师函、仲裁通知时，法务的头三天决定后面三个月。

**律师视角关心「怎么打」，法务视角还要多管三件事**：
  1. **时限** —— 答辩期、管辖异议期，逾期不可补
  2. **内部动作** —— 证据冻结、通知相关部门、锁定知情人
  3. **对外口径** —— 谁能说、说什么、要不要披露

第 2、3 项律师往往不管，但**证据被删、员工乱说、该披露没披露**造成的损失，
常常大于案件本身。

用法：
    litigation_response.py --type 起诉状 --received 20260801 --amount 5000000 \
        --listed --today 20260804
"""

import argparse
import json
import re
from datetime import date, timedelta

# 各类文书的法定时限
DEADLINES = {
    "起诉状": [
        ("提交答辩状", 15, "**逾期不影响审理，但会失去管辖异议权**"),
        ("**提出管辖权异议**", 15, "**与答辩期同，逾期即丧失，不可补**——"
                                   "这是企业侧最常错过的一项"),
        ("举证期限", None, "由法院指定，收到举证通知书后确认。"
                           "申请证人出庭、申请鉴定、申请调取证据均有期限"),
    ],
    "上诉状": [
        ("提交答辩状", 15, "收到上诉状副本之日起"),
    ],
    "仲裁通知": [
        ("提交答辩", None, "按仲裁规则确定，通常 15–45 日，**须核对具体规则**"),
        ("选定仲裁员", None, "按仲裁规则，逾期由仲裁机构指定——"
                             "**放弃选择权对企业不利**"),
        ("提出管辖异议", None, "须在首次实体答辩前提出"),
    ],
    "劳动仲裁": [
        ("提交答辩", 10, "收到申请书副本之日起十日内"),
        ("举证", None, "**注意：因用人单位作出的解除等决定发生争议的，"
                       "由用人单位举证**——考勤、工资、制度、告知记录都要备齐"),
    ],
    "律师函": [
        ("回函（非法定）", None, "**律师函没有法定回复期限**，但沉默可能被解读为默认或怠于处理。"
                                 "是否回、怎么回，取决于策略——**不要凭本能立刻回复**"),
    ],
    "行政处罚告知书": [
        ("陈述申辩", 3, "**收到告知书后 3 日内**（部分领域更长，须核对具体规定）"),
        ("**申请听证**", 3, "达到听证标准的，**逾期视为放弃**——"
                            "听证是处罚作出前最后的实质抗辩机会"),
    ],
}

# 内部应急动作。按紧急度排序。
INTERNAL_ACTIONS = [
    ("🔴 立即", "证据冻结与保全",
     "**暂停一切可能涉案的数据删除、系统清理、邮件归档策略**。"
     "包括邮件、聊天记录、系统日志、监控录像、纸质文件。"
     "**诉讼中删除证据的后果远重于证据本身不利**"),
    ("🔴 立即", "锁定知情人与统一口径",
     "确定谁接触过涉案事项、谁可能被问询。**明确对外发言人**，"
     "其余人员不得对外表态——员工的一句话可能成为对方的证据"),
    ("🔴 立即", "核对文书真伪与送达",
     "确认案号、法院、送达方式与签收人。**送达日期直接决定所有期限的起算**"),
    ("🟡 三日内", "调取合同与往来记录",
     "涉案合同原件、履行凭证、往来函件、付款记录、会议纪要"),
    ("🟡 三日内", "评估披露义务",
     "上市公司、发债主体、有对赌或融资协议的，**可能触发信息披露或通知义务**。"
     "重大诉讼的披露标准须核对；**漏披露的后果独立于案件本身**"),
    ("🟡 三日内", "通知相关方",
     "保险（是否有责任险可报案，**报案有期限**）、担保方、关联方、投资人"),
    ("🔵 一周内", "内部访谈与事实还原",
     "与经办人访谈并留痕。**注意：内部调查记录可能成为证据**，"
     "涉及法律意见的部分应通过律师进行"),
    ("🔵 一周内", "业务影响评估",
     "是否影响在手订单、招投标资格、融资、上市进程"),
]

LISTED_EXTRA = [
    "重大诉讼仲裁的**信息披露义务**，须核对交易所规则的金额与影响标准",
    "定期报告中的诉讼事项披露",
    "**内幕信息管理**——知情人登记，防止在披露前发生交易",
]


def d(s):
    s = re.sub(r"\D", "", str(s))
    if len(s) != 8:
        raise SystemExit(f"错误：日期格式应为 YYYYMMDD，收到「{s}」")
    return date(int(s[:4]), int(s[4:6]), int(s[6:]))


def main():
    p = argparse.ArgumentParser(description="应诉应急响应（企业侧）",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--type", dest="doc_type", required=True, choices=list(DEADLINES),
                   help="收到的文书类型")
    p.add_argument("--received", required=True, help="**签收日期** YYYYMMDD——决定所有期限起算")
    p.add_argument("--amount", type=float, help="涉案金额（元）")
    p.add_argument("--listed", action="store_true", help="上市公司或有披露义务的主体")
    p.add_argument("--insured", action="store_true", help="可能有责任险覆盖")
    p.add_argument("--today", help="计算基准日")
    args = p.parse_args()

    today = d(args.today) if args.today else date.today()
    recv = d(args.received)

    deadlines = []
    for name, days, note in DEADLINES[args.doc_type]:
        if days is None:
            deadlines.append({"事项": name, "期限": "**须核对具体规定或通知书**",
                              "说明": note, "状态": "待确认"})
            continue
        e = recv + timedelta(days=days)
        left = (e - today).days
        deadlines.append({
            "事项": name, "期限": f"{days} 日",
            "起算": recv.isoformat(), "届满": e.isoformat(), "剩余天数": left,
            "状态": "❌ **已过**" if left < 0 else ("🔴 紧急" if left <= 3 else "进行中"),
            "说明": note,
        })

    overdue = [x for x in deadlines if x.get("状态", "").startswith("❌")]
    urgent = [x for x in deadlines if x.get("状态") == "🔴 紧急"]

    actions = [{"紧急度": lv, "动作": a, "要点": t} for lv, a, t in INTERNAL_ACTIONS]

    notes = [
        "**收到文书的头三天决定后面三个月。** 时限、证据、口径三件事必须同时启动，"
        "不能等律师到位再说",
        "⚠️ **证据冻结是第一位的。** 暂停所有可能涉案的数据删除与清理策略——"
        "**诉讼中删除证据的后果远重于证据本身不利**",
        "⚠️ **送达日期决定所有期限的起算。** 务必核实签收人、签收日期与签收方式；"
        "签收人非授权人员的，须评估送达是否有效",
        "**统一对外口径。** 员工、客户、供应商、媒体的询问都应归口，"
        "**一句随口的话可能成为对方的证据**",
    ]
    if overdue:
        notes.insert(0, f"🔴 **{len(overdue)} 项期限已过**："
                        f"{'、'.join(x['事项'] for x in overdue)}。"
                        f"立即评估补救可能——管辖异议逾期不可补，"
                        f"但答辩逾期不影响实体审理")
    if urgent:
        notes.insert(0, f"🔴 **{len(urgent)} 项期限剩余不足 3 天**，须今日安排")
    if args.listed:
        notes.append("**上市主体的额外义务**：" + "；".join(LISTED_EXTRA))
    if args.insured:
        notes.append("**责任险报案有期限**，且晚报可能影响理赔。"
                     "收到文书后应立即通知保险人并保留通知凭证")
    if args.doc_type == "律师函":
        notes.append("**律师函不必立刻回。** 先判断对方的真实意图（施压 / 固定证据 / "
                     "为诉讼做准备 / 试探态度）。**急于回复反而可能暴露己方判断或做出不利自认**")
    if args.doc_type == "劳动仲裁":
        notes.append("⚠️ **劳动争议的举证责任大量在用人单位**——"
                     "解除决定、工资标准、考勤、制度公示都要企业证明。"
                     "证据备不齐，实体再有理也会输")

    print(json.dumps({
        "文书类型": args.doc_type,
        "签收日": recv.isoformat(), "基准日": today.isoformat(),
        "涉案金额": args.amount,
        "一、⚠️ 法定时限": deadlines,
        "二、内部应急动作": actions,
        "三、提示": notes,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
