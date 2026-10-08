#!/usr/bin/env python3
"""
劳动争议请求项与时效核查

劳动争议最常见的两类失误，本脚本各拦一类：

**① 请求项漏项。** 劳动者一次仲裁往往只提"违法解除赔偿金"，漏掉未休年休假工资、
   未签合同二倍工资、加班费、经济补偿差额。仲裁裁决后再就漏掉的项目另行申请，
   常因超过时效或被认为重复处理而受阻。

**② 仲裁时效算错。** 劳动争议仲裁时效是**一年**，但有一条重要例外：
   **劳动关系存续期间因拖欠劳动报酬发生争议的，不受一年限制**，
   自劳动关系终止之日起一年内提出即可。不知道这条，会把还能主张的工资算成过期。

用法：
    labor_claims.py --relation-start 20180301 --relation-end 20240301 \
        --monthly-wage 20000 --avg-wage 12000 --dispute-type 违法解除 \
        --written-contract-from 20180901 --unpaid-wage --annual-leave-days 15 \
        --know-date 20240301
"""

import argparse
import json
import re
from datetime import date, datetime, timedelta

ARB_LIMIT_MONTHS = 12  # 劳动争议仲裁时效一年


def d(s):
    s = re.sub(r"\D", "", str(s))
    if len(s) != 8:
        raise SystemExit(f"错误：日期格式应为 YYYYMMDD，收到「{s}」")
    return date(int(s[:4]), int(s[4:6]), int(s[6:]))


def add_months(base, n):
    m = base.month - 1 + n
    y = base.year + m // 12
    m = m % 12 + 1
    day = base.day
    while True:
        try:
            return date(y, m, day)
        except ValueError:
            day -= 1


def months_between(a, b):
    m = (b.year - a.year) * 12 + (b.month - a.month)
    if b.day < a.day:
        m -= 1
    return max(m, 0)


def service_years(start, end):
    """《劳动合同法》47条：满一年一个月；六个月以上不满一年按一年；不满六个月半个月。"""
    m = months_between(start, end)
    full, rem = m // 12, m % 12
    if rem == 0:
        return full, f"{full}年整"
    if rem >= 6:
        return full + 1, f"{full}年{rem}个月，满六个月按一年计"
    return full + 0.5, f"{full}年{rem}个月，不满六个月加计半个月"


def build_claims(a):
    """按案件要素倒推应当主张的请求项，标出可能漏掉的。"""
    claims = []
    start = d(a.relation_start)
    end = d(a.relation_end) if a.relation_end else None
    wage = a.monthly_wage

    if a.dispute_type in ("违法解除", "违法终止"):
        yrs, desc = service_years(start, end) if end else (0, "未提供终止日")
        base = wage
        capped = False
        if a.avg_wage and wage and wage > a.avg_wage * 3:
            base = a.avg_wage * 3
            capped = True
            yrs = min(yrs, 12)
        claims.append({
            "请求项": "违法解除劳动合同赔偿金（2N）",
            "计算": f"{base:,.0f} × {yrs} × 2" if base else "需月工资",
            "金额": round(base * yrs * 2, 2) if base else None,
            "说明": f"工作年限：{desc}" + ("｜月工资超当地社平三倍，按三倍封顶且年限最多12年" if capped else ""),
            "要件": "须先认定解除违法。**举证责任在用人单位**——因用人单位作出的解除决定发生争议的，由其举证",
        })
        claims.append({
            "请求项": "【备选】经济补偿金（N）",
            "计算": f"{base:,.0f} × {yrs}" if base else "需月工资",
            "金额": round(base * yrs, 2) if base else None,
            "说明": "解除被认定为合法但符合支付经济补偿情形时主张。**与2N择一，不并列**",
            "要件": "属《劳动合同法》46条情形",
        })

    if a.written_contract_from:
        wc = d(a.written_contract_from)
        gap = months_between(start, wc)
        # 用工之日起满一个月不满一年未订立书面合同的，支付二倍工资，最多11个月
        payable = max(min(gap - 1, 11), 0)
        if payable > 0:
            claims.append({
                "请求项": "未订立书面劳动合同二倍工资差额",
                "计算": f"{wage:,.0f} × {payable} 个月" if wage else f"{payable} 个月",
                "金额": round(wage * payable, 2) if wage else None,
                "说明": f"用工之日 {start.isoformat()} 至订立书面合同 {wc.isoformat()}，"
                        f"间隔 {gap} 个月，**自第二个月起计算，法定最多11个月**",
                "要件": "⚠️ **该项适用一年仲裁时效且从未签期间届满起算**，最易超期，须优先核算",
            })

    if a.unpaid_wage:
        claims.append({
            "请求项": "拖欠工资",
            "计算": "按实际拖欠金额", "金额": None,
            "说明": "**劳动关系存续期间因拖欠劳动报酬发生争议的，不受一年仲裁时效限制**，"
                    "自劳动关系终止之日起一年内提出即可",
            "要件": "举证：工资标准与实发记录。用人单位掌握工资支付凭证，"
                    "应申请其提供（保存两年备查）",
        })

    if a.overtime:
        claims.append({
            "请求项": "加班费",
            "计算": "平时1.5倍／休息日2倍／法定节假日3倍", "金额": None,
            "说明": "劳动者对加班事实初步举证；**用人单位掌握考勤记录而不提供的承担不利后果**，"
                    "应先申请仲裁委责令其提供",
            "要件": "⚠️ 加班费属劳动报酬，存续期间的争议同样**不受一年时效限制**",
        })

    if a.annual_leave_days:
        daily = wage / 21.75 if wage else None
        amt = round(daily * a.annual_leave_days * 2, 2) if daily else None
        claims.append({
            "请求项": "未休年休假工资报酬",
            "计算": f"日工资 × {a.annual_leave_days} 天 × 200%（另100%已含在正常工资中）",
            "金额": amt,
            "说明": "日工资按月工资 ÷ 21.75 折算。**未休年休假工资报酬的性质存在争议**——"
                    "部分地区认定为福利适用一年时效，部分认定为劳动报酬，须核对当地口径",
            "要件": "举证：应休天数与实际未休",
        })

    if a.social_insurance:
        claims.append({
            "请求项": "未缴/少缴社会保险",
            "计算": "按欠缴基数与年限", "金额": None,
            "说明": "⚠️ **社保补缴争议通常不属于劳动争议受案范围**，应向社保征收机构投诉；"
                    "但因未缴社保导致的**损失赔偿**（如无法享受待遇）属受案范围",
            "要件": "须区分「补缴」与「赔偿损失」两条路径",
        })

    if a.notice_pay:
        claims.append({
            "请求项": "代通知金（+1）",
            "计算": f"{wage:,.0f} × 1" if wage else "1个月工资",
            "金额": wage,
            "说明": "仅适用于《劳动合同法》40条情形且用人单位选择支付而非提前三十日通知。"
                    "**不是所有解除都有+1**",
            "要件": "属40条三种情形之一",
        })

    return claims


def check_limitation(a):
    """仲裁时效核查，重点是劳动报酬类的例外规则。"""
    today = d(a.today) if a.today else date.today()
    end = d(a.relation_end) if a.relation_end else None
    know = d(a.know_date) if a.know_date else end

    out = {"基准日": today.isoformat(), "一般时效": "一年", "分项": []}

    if know:
        general_expire = add_months(know, ARB_LIMIT_MONTHS)
        out["分项"].append({
            "类别": "一般争议（解除赔偿金、经济补偿、二倍工资等）",
            "起算": know.isoformat(),
            "届满": general_expire.isoformat(),
            "剩余天数": (general_expire - today).days,
            "状态": "已过" if general_expire < today else
                    ("紧急" if (general_expire - today).days <= 60 else "时效内"),
        })

    if a.unpaid_wage or a.overtime:
        if end:
            wage_expire = add_months(end, ARB_LIMIT_MONTHS)
            out["分项"].append({
                "类别": "劳动报酬（工资、加班费）",
                "起算": end.isoformat(),
                "届满": wage_expire.isoformat(),
                "剩余天数": (wage_expire - today).days,
                "状态": "已过" if wage_expire < today else
                        ("紧急" if (wage_expire - today).days <= 60 else "时效内"),
                "特别规则": "**劳动关系存续期间因拖欠劳动报酬发生争议的，不受一年限制**，"
                            "自劳动关系终止之日起一年内提出即可。"
                            "不知道这条会把还能主张的工资算成过期",
            })
        else:
            out["分项"].append({
                "类别": "劳动报酬（工资、加班费）",
                "状态": "劳动关系存续中",
                "特别规则": "**存续期间不受一年仲裁时效限制**，随时可提出；"
                            "关系终止后一年内仍可主张",
            })
    return out


def main():
    p = argparse.ArgumentParser(description="劳动争议请求项与时效核查",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--relation-start", dest="relation_start", required=True, help="用工之日 YYYYMMDD")
    p.add_argument("--relation-end", dest="relation_end", help="劳动关系终止日 YYYYMMDD")
    p.add_argument("--monthly-wage", dest="monthly_wage", type=float, help="解除前十二个月平均工资")
    p.add_argument("--avg-wage", dest="avg_wage", type=float, help="当地上年度职工月平均工资")
    p.add_argument("--dispute-type", dest="dispute_type",
                   choices=["违法解除", "违法终止", "拖欠工资", "其他"], default="其他")
    p.add_argument("--written-contract-from", dest="written_contract_from",
                   help="订立书面劳动合同之日 YYYYMMDD，用于二倍工资核算")
    p.add_argument("--unpaid-wage", action="store_true", help="存在拖欠工资")
    p.add_argument("--overtime", action="store_true", help="存在加班")
    p.add_argument("--annual-leave-days", dest="annual_leave_days", type=int, help="未休年休假天数")
    p.add_argument("--social-insurance", dest="social_insurance", action="store_true", help="涉社保")
    p.add_argument("--notice-pay", dest="notice_pay", action="store_true", help="主张代通知金")
    p.add_argument("--know-date", dest="know_date", help="知道权利被侵害之日，默认取关系终止日")
    p.add_argument("--today", help="计算基准日")
    args = p.parse_args()

    claims = build_claims(args)
    lim = check_limitation(args)
    total = sum(c["金额"] for c in claims if c.get("金额") and "备选" not in c["请求项"])

    # 遗漏检查用关键词集合而非整串匹配。
    # 教训：初版拿「未签书面合同二倍工资」去 in 请求项串，
    # 而实际请求项写的是「未订立书面劳动合同二倍工资差额」，字面对不上，
    # 明明已主张的项目被报成遗漏——误报会让律师不信任这份清单。
    CHECKLIST = [
        ("违法解除赔偿金 / 经济补偿", ["赔偿金", "经济补偿"]),
        ("代通知金", ["代通知金"]),
        ("未签书面合同二倍工资", ["二倍工资"]),
        ("拖欠工资", ["拖欠工资", "工资差额"]),
        ("加班费", ["加班"]),
        ("未休年休假工资", ["年休假"]),
        ("社保相关", ["社会保险", "社保"]),
        ("竞业限制补偿", ["竞业"]),
        ("工伤保险待遇", ["工伤"]),
        ("高温津贴等专项补贴", ["津贴", "补贴"]),
    ]
    claimed = "｜".join(c["请求项"] for c in claims)
    missing = [label for label, keys in CHECKLIST
               if not any(k in claimed for k in keys)]

    notes = [
        "**请求项漏项是劳动争议最常见的失误。** 仲裁裁决后再就漏掉的项目另行申请，"
        "常因超过时效或被认为重复处理而受阻——**一次提全**",
        "⚠️ **仲裁前置**：劳动争议须先经劳动仲裁，未经仲裁直接起诉将被驳回起诉",
        "举证责任：**因用人单位作出的开除、除名、辞退、解除、减少劳动报酬、"
        "计算工作年限等决定发生争议的，由用人单位举证**。这一条务必用足",
        "各地对未休年休假工资性质、二倍工资起算、社保争议受案范围的口径不一，"
        "**须核对当地仲裁委与法院的实际做法**（可用「类案检索」按地域对比）",
    ]
    urgent = [x for x in lim["分项"] if x.get("状态") in ("紧急", "已过")]
    if urgent:
        notes.insert(0, f"⚠️ 有 {len(urgent)} 类请求处于**{urgent[0]['状态']}**状态，"
                        f"须立即处理或核对有无时效中断事由")

    print(json.dumps({
        "劳动关系": {"起": args.relation_start, "止": args.relation_end or "存续中"},
        "请求项": claims,
        "合计（不含备选项）": round(total, 2) if total else None,
        "仲裁时效": lim,
        "可能遗漏的请求项": missing,
        "遗漏提示": "上列项目请逐一确认本案是否涉及。**不涉及的可排除，涉及而未主张的须补入**",
        "提示": notes,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
