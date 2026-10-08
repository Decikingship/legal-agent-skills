#!/usr/bin/env python3
"""
智合AI搜案 CLI — 司法案例检索

对接 CaseVectorService。相对旧版新增：
  - v2：分维度向量检索（案情/查明/请求/焦点各自文本与权重）、关键词模式、裁判结果类型
  - v3：刑事四参数（罪名 / 涉案金额 / 量刑编码 / 裁判结果）
  - 内置确定性防护：
      * 入库案例应用层二次筛选（服务端对 RKAL 不生效，契约 G-6）
      * 审判人员字段一律剥离（合规红线，契约 3.10）
      * 无 id 剔除、案件标题剔除、超链接生成、字符量控制
      * 一审裁判自动标注终局性未核实（契约 G-2）

用法示例：
    # 类案检索（推荐：分维度）
    zhihe_case_search.py --focus "<争议焦点，≥100字陈述句>" \
        --discovery "<关键事实>" --claim "<诉讼请求>" \
        --focus-weight 0.6 --discovery-weight 0.25 --claim-weight 0.15 \
        --court-area "江苏省" --date-start 20210101 --limit 20 --instruct-limit 5

    # 单一 cvector（v1 兼容）
    zhihe_case_search.py --cvector "<≥100字案情陈述>" --weight 0.15 0.25 0.60

    # 关键词精确检索（找特定表述/条款援引）
    zhihe_case_search.py --keywords "建设工程价款优先受偿权 六个月" --limit 20

    # 按案号 / 当事人
    zhihe_case_search.py --case-code "（2015）宿中民一终第00223号"
    zhihe_case_search.py --caseparties "某某建设集团有限公司" --limit 20

    # 刑事量刑校准
    zhihe_case_search.py --focus "<案情>" --crime "诈骗罪" \
        --amount-min 500000 --amount-max 1000000 --penalty-codes "2.3" --limit 30
"""

import argparse
import json
import re
import sys
import urllib.error
import urllib.request

import os

# 服务地址与令牌走环境变量，便于内网/公网、测试/生产切换。
# 开源版默认地址为占位地址，使用前请配置 ZHIHE_BASE_URL。
# ZHIHE_TOKEN 不设置时不发 Authorization 头。
# 需要走公网网关时，设 ZHIHE_BASE_URL 与 ZHIHE_TOKEN 两个变量即可，脚本无需改动。
BASE_URL = os.environ.get(
    "ZHIHE_BASE_URL",
    "https://your-legal-api.example.com").rstrip("/")
ZHIHE_TOKEN = os.environ.get("ZHIHE_TOKEN", "")


def auth_headers():
    h = {"Content-Type": "application/json"}
    if ZHIHE_TOKEN:
        h["Authorization"] = ZHIHE_TOKEN if ZHIHE_TOKEN.lower().startswith("bearer ") \
            else f"Bearer {ZHIHE_TOKEN}"
    return h

DETAIL_URL = "https://www.zhiexa.com/fuxi/judgment-documents/detail?id={}"

# 端点。v2/v3 路径需与后端确认（见 conventions D-9），可用 --endpoint 覆盖。
ENDPOINT_V1 = "/case/vector"
ENDPOINT_V2 = "/case/vector2"
ENDPOINT_V3 = "/case/vector3"

# 合规红线：审判人员相关字段一律不出现在输出中
FORBIDDEN_FIELDS = {"judges", "judge", "审判人员", "judge_name"}

# result_type 分组（详见 conventions/code-tables.md）
RT_MERIT_WIN = {1, 2}                 # 全部支持、部分支持
RT_MERIT_LOSE = {3}                   # 驳回全部诉讼请求
RT_PROCEDURAL = {4, 5, 29}            # 驳回起诉、不予受理 —— 不进入实体支持率分母
RT_NO_REASONING = {10, 11, 30, 31}    # 调解、撤诉、终止/中止审理 —— 无裁判说理
RT_UPHOLD = 6                         # 维持原判 → 下级裁判生效
RT_REVERSED = {7, 8, 9}               # 改判、发回重审、提审 → 下级裁判被推翻
RT_VALID = set(range(0, 32))

# reference_level 呈现顺序；08-11 为评析/编选材料，非裁判文书
REF_ORDER = ["01", "02", "04", "06", "0301", "03", "0302", "05", "07"]
REF_COMMENTARY = {"08", "09", "10", "11"}
REF_NAMES = {
    "01": "最高法指导性案例", "02": "最高检指导性案例", "03": "入库案例",
    "0301": "最高法指导性案例（入库）", "0302": "参考案例（入库）",
    "04": "公报案例", "05": "参考案例", "06": "官方典型案例",
    "07": "上海金融法院精选案例", "08": "最高人民法院民商事裁判观点",
    "09": "法官精析评案", "10": "律师点评案例", "11": "经典案例",
}

# 专门法院不在 1-4 审级序列内，限定中级/基层会漏掉它们
SPECIALIZED_DOMAINS = [
    (r"知识产权|专利|商标|著作权|商业秘密", "知识产权法院"),
    (r"金融|证券|期货|信托|票据|保险", "金融法院"),
    (r"海事|海商|船舶|港口|提单", "海事法院"),
    (r"网络|电商|平台|直播|数据|个人信息", "互联网法院"),
    (r"铁路运输", "铁路运输法院"),
]

# 案例评析不是裁判文书
NON_JUDGMENT_DOCTYPE = {"案例评析", "通知书", "令"}

FINAL_PROCEDURE_RE = re.compile(r"二审|再审|终审")
FIRST_INSTANCE_RE = re.compile(r"一审|初审")
NON_JUDGMENT_RE = re.compile(r"调解书|撤诉|裁定书")

# 领域修法节点，用于提示时间起点是否恰当
LAW_MILESTONES = [
    ("公司法|股东|出资|股权", "20240701", "新公司法 2024-07-01 施行"),
    ("个人信息|数据出境|隐私", "20211101", "个保法 2021-11-01 施行"),
    ("民法典|合同|物权|婚姻|继承|侵权", "20210101", "民法典 2021-01-01 施行"),
]

LONG_TEXT_FIELDS = [
    "court_opinion", "summary", "discovery", "complaint", "reason",
    "jbaq", "cply", "cpyz", "judgment_result",
]


def clean_date(s):
    return (s or "").replace("-", "").replace("/", "")


def build_body(args):
    body = {}

    def put(key, val):
        if val is not None:
            body[key] = val

    # 通用过滤
    put("case_code", args.case_code)
    put("caseparties", args.caseparties)
    put("court_name", args.court_name)
    put("court_level", args.court_level)
    put("court_area", args.court_area)
    put("judgment_date_start", args.date_start)
    put("judgment_date_end", args.date_end)
    put("limit", args.limit)
    put("instruct_limit", args.instruct_limit)
    put("instruct_title", args.instruct_title)
    put("rukuid", args.rukuid)
    put("minScore", args.min_score)

    tier = detect_tier(args)

    if tier == 1:
        put("cvector", args.cvector)
        if args.weight:
            body["weight"] = args.weight  # [complaint, discovery, focus]，服务端归一化
    else:
        # SearchMode 枚举值是 "mvector"／"keyword"，不是 "vector"。
        # 写错会被 pydantic 直接拒（422），且 v2/v3 该字段为必填。
        put("search_mode", "keyword" if args.keywords else "mvector")
        put("keywords", args.keywords)
        put("summary_vector", args.summary or args.cvector)
        put("summary_weight", args.summary_weight)
        put("discovery_vector", args.discovery)
        put("discovery_weight", args.discovery_weight)
        put("claim_vector", args.claim)
        put("claim_weight", args.claim_weight)
        put("focus_vector", args.focus)
        put("focus_weight", args.focus_weight)
        if args.result_type:
            # 声明为 list[int]，且服务端校验编码合法性，非法码会整体报错
            codes = [int(x) for x in args.result_type]
            bad = [c for c in codes if c not in RT_VALID]
            if bad:
                raise SystemExit(f"错误：result_type 含非法码值 {bad}，合法范围 0-31，"
                                 f"详见 conventions/code-tables.md")
            body["result_type"] = codes
        if tier == 3:
            put("crime", args.crime)
            put("involved_amount_min", args.amount_min)
            put("involved_amount_max", args.amount_max)
            put("penalty_codes", args.penalty_codes)

    return body


def detect_tier(args):
    if any([args.crime, args.amount_min, args.amount_max, args.penalty_codes]):
        return 3
    if any([args.summary, args.discovery, args.claim, args.focus,
            args.keywords, args.result_type]):
        return 2
    return 1


def request_api(path, body, timeout=90):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}{path}", data=data,
        headers=auth_headers(), method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def rkal_post_filter(items, args, warnings):
    """入库案例二次筛选。

    服务端 _search_rkal_by_cvector / _by_instructtitle 中过滤表达式被注释掉，
    court_area / court_level / judgment_date 对入库案例完全不生效（契约 G-6）。
    这里在应用层补上，否则用户以为筛过了，实际没有。
    """
    if not any([args.date_start, args.date_end, args.court_level, args.court_area, args.court_name]):
        return items

    kept, dropped = [], 0
    for it in items:
        if it.get("source") != "rkal":
            kept.append(it)
            continue

        jd = clean_date(it.get("judgment_date"))
        if args.date_start and jd and jd < clean_date(args.date_start):
            dropped += 1
            continue
        if args.date_end and jd and jd > clean_date(args.date_end):
            dropped += 1
            continue

        lvl = it.get("court_level") or ""
        if args.court_level and not any(x in lvl for x in args.court_level.split()):
            dropped += 1
            continue

        cname = it.get("court_name") or ""
        if args.court_name and not any(x in cname for x in args.court_name.split()):
            dropped += 1
            continue
        if args.court_area and not any(
            x.rstrip("省市自治区") in cname for x in args.court_area.split()
        ):
            dropped += 1
            continue

        kept.append(it)

    if dropped:
        warnings.append(
            f"入库案例二次筛选剔除 {dropped} 条（服务端过滤器对入库案例不生效，契约 G-6，已在客户端补筛）"
        )
    return kept


def check_date_milestone(args, warnings):
    probe = " ".join(filter(None, [
        args.cvector, args.summary, args.discovery, args.claim,
        args.focus, args.keywords, args.crime,
    ]))
    if not probe:
        return
    start = clean_date(args.date_start) or "20200101"
    for pat, milestone, why in LAW_MILESTONES:
        if re.search(pat, probe) and start < milestone:
            warnings.append(
                f"检索内容涉及已修法领域（{why}），当前起始日期 {start} 早于修法节点 {milestone}。"
                f"修订前裁判不得与现行法裁判混排，建议上调 --date-start 或对旧法裁判单列标注"
            )


def check_specialized_court(args, warnings):
    """专门法院是独立层级（code 9），限定中级/基层会把它们整体排除。"""
    if not args.court_level:
        return
    if "专门" in args.court_level:
        return
    probe = " ".join(filter(None, [
        args.cvector, args.summary, args.discovery, args.claim, args.focus, args.keywords,
    ]))
    for pat, court in SPECIALIZED_DOMAINS:
        if re.search(pat, probe):
            warnings.append(
                f"检索内容涉及{court}管辖领域，但 --court-level 未包含「专门法院」。"
                f"{court}属独立审级（code 9），当前过滤条件会将其整体排除。"
                f"建议改为 --court-level \"{args.court_level} 专门法院\" 或不限审级后按 court_name 判断"
            )
            break


def check_terminality(items, args, warnings):
    """终局性核查（契约 G-2 的正解）。

    result_type 码表提供了上诉审结果：6 维持原判 / 7 改判 / 8 发回重审 / 9 提审。
    对结果集中的一审裁判，可用同当事人 + 上诉审的 result_type 反查其是否被推翻。
    这里只做标注与提示，实际反查需调用方再发一次检索（当事人信息在 parties 字段）。
    """
    first_instance = [i for i in items
                      if FIRST_INSTANCE_RE.search(i.get("procedure") or "")
                      and not FINAL_PROCEDURE_RE.search(i.get("procedure") or "")]
    if not first_instance:
        return
    warnings.append(
        f"{len(first_instance)} 条为一审裁判。终局性反查方法："
        f"取该案当事人，以 --caseparties 加二审/再审程序检索，"
        f"读 result_type —— 「维持原判」则一审生效可引用，"
        f"「改判」「发回重审」「提审」则一审观点已被推翻，禁止引用"
    )


def postprocess(raw, args, warnings):
    items = (raw or {}).get("results") or (raw or {}).get("data", {}).get("results") or []
    if isinstance(raw, list):
        items = raw

    items = rkal_post_filter(items, args, warnings)

    out = []
    dropped_no_id = 0
    total_chars = 0
    first_instance_n = 0

    for it in items:
        cid = it.get("id")
        if not cid:
            dropped_no_id += 1
            continue

        procedure = it.get("procedure") or ""
        doc_type = it.get("document_type") or ""
        flags = []

        if NON_JUDGMENT_RE.search(doc_type) and "判决书" not in doc_type:
            flags.append(f"文书类型为{doc_type}，不宜作为裁判规则依据")

        if FIRST_INSTANCE_RE.search(procedure) and not FINAL_PROCEDURE_RE.search(procedure):
            flags.append("一审裁判，终局性未核实：用 --caseparties 反查同当事人的二审/再审，"
                         "看 result_type 是「维持原判」还是「改判/发回重审」")
            first_instance_n += 1

        ref = str(it.get("reference_level") or "")
        ref_code = next((c for c, n in REF_NAMES.items() if n == ref), ref)
        if ref_code in REF_COMMENTARY or ref in REF_NAMES.values() and ref_code in REF_COMMENTARY:
            flags.append(f"【{ref}】属评析/编选材料，不是法院在个案中的裁判说理，"
                         f"不得表述为「法院认为」，不得作为裁判依据")
        elif ref_code in ("01", "02", "0301"):
            flags.append(f"【{ref}】指导性案例，应当参照")
        elif ref_code in ("04", "06"):
            flags.append(f"【{ref}】权威发布案例")
        elif it.get("source") == "rkal":
            flags.append("入库案例（精选），参考价值较高")

        if any(x in doc_type for x in NON_JUDGMENT_DOCTYPE):
            flags.append(f"文书类型为{doc_type}，非裁判文书，不得作为裁判依据引用")

        rt = it.get("result_type")
        if rt and any(x in str(rt) for x in ("调解", "撤诉", "终止审理", "中止审理")):
            flags.append(f"裁判结果为{rt}，无实体裁判说理，不得用于提炼裁判规则")

        rec = {
            "id": cid,
            "case_id": it.get("case_id") or "",
            "court_name": it.get("court_name") or "",
            "court_level": it.get("court_level") or "",
            "procedure": procedure,
            "document_type": doc_type,
            "judgment_date": it.get("judgment_date") or "",
            "reference_level": it.get("reference_level"),
            "result_type": it.get("result_type"),
            "source": it.get("source") or "",
            "score": it.get("score"),
            "_link": DETAIL_URL.format(cid),
            "_cite": "[{}]({})".format(it.get("case_id") or cid, DETAIL_URL.format(cid)),
            "_flags": flags,
        }

        # 正文字段：截断长文本
        for f in LONG_TEXT_FIELDS:
            v = it.get(f)
            if not v:
                continue
            if isinstance(v, list):
                v = "\n".join(v)
            if args.max_field_chars and len(v) > args.max_field_chars:
                v = v[: args.max_field_chars] + "……〔已截断〕"
            rec[f] = v

        if it.get("convictions"):
            # 结构化量刑对照：defendant / charged_crime(指控) / involved_amount /
            # convicted_crime(审定) / sentence / penalty_type /
            # prison_term_months(数值刑期) / fine_amount / is_final_sentence
            # prison_term_months 是数值，量刑统计直接算即可，不必解析裁判文书文本
            rec["convictions"] = it["convictions"]
            if any(c.get("is_final_sentence") is False for c in it["convictions"]):
                flags.append("含非最终决定刑的单罪量刑记录，统计刑期时须按 is_final_sentence 区分")

        # 合规红线：审判人员字段一律不输出
        for f in FORBIDDEN_FIELDS:
            rec.pop(f, None)

        rec_chars = len(json.dumps(rec, ensure_ascii=False))
        if args.max_total_chars and total_chars + rec_chars > args.max_total_chars:
            warnings.append(
                f"已达字符上限 {args.max_total_chars}，截断于第 {len(out)} 条；"
                f"请缩小范围或分维度多次调用后自行汇总"
            )
            break
        total_chars += rec_chars
        out.append(rec)

    if dropped_no_id:
        warnings.append(f"剔除 {dropped_no_id} 条无 id 的结果（平台硬约束：无 ID 禁止引用）")
    check_terminality(out, args, warnings)

    # 按 reference_level 位阶排序（该字段不是入参，只能在应用层排）
    def ref_rank(rec):
        ref = str(rec.get("reference_level") or "")
        code = next((c for c, n in REF_NAMES.items() if n == ref), None)
        if code in REF_COMMENTARY:
            return 99
        return REF_ORDER.index(code) if code in REF_ORDER else 50
    out.sort(key=ref_rank)

    warnings.append("分析必须取 court_opinion（本院认为）；complaint 是当事人主张，不得当作法院观点")
    warnings.append("输出时只引用案号并包装为超链接，禁止输出案件标题；禁止以审判人员为维度做任何统计")

    return out


def main():
    p = argparse.ArgumentParser(description="智合AI搜案（旧版下划线参数名仍兼容）", formatter_class=argparse.RawDescriptionHelpFormatter)

    g1 = p.add_argument_group("语义检索（v2 分维度，推荐）")
    g1.add_argument("--summary", help="整体案情，≥100字陈述句")
    g1.add_argument("--discovery", help="本院查明的关键事实")
    g1.add_argument("--claim", help="诉讼请求 / 原告诉称")
    g1.add_argument("--focus", help="争议焦点（类案检索的核心维度）")
    g1.add_argument("--summary-weight", dest="summary_weight", type=float)
    g1.add_argument("--discovery-weight", dest="discovery_weight", type=float)
    g1.add_argument("--claim-weight", dest="claim_weight", type=float)
    g1.add_argument("--focus-weight", dest="focus_weight", type=float)

    g2 = p.add_argument_group("其他检索入口")
    g2.add_argument("--cvector", help="v1 单一语义向量，≥100字陈述句，禁含「案例/问题/分析」")
    g2.add_argument("--weight", nargs=3, type=float, metavar=("COMPLAINT", "DISCOVERY", "FOCUS"),
                    help="v1 三维权重，依次为诉称/查明/焦点。类案建议 0.15 0.25 0.60")
    g2.add_argument("--keywords", help="关键词模式，空格分隔，走全文检索（找特定表述/条款援引时用）")
    g2.add_argument("--case-code", "--case_code", dest="case_code", help="案号，空格分隔可多个")
    g2.add_argument("--caseparties", help="当事人（语义检索，容错好但可能命中近似主体，须核对）")
    g2.add_argument("--instruct-title", "--instruct_title", dest="instruct_title", help="入库案例标题检索")
    g2.add_argument("--rukuid", help="入库案例编号")

    g3 = p.add_argument_group("过滤")
    g3.add_argument("--court-name", "--court_name", dest="court_name", help="法院名称，空格分隔可多个")
    g3.add_argument("--court-level", "--court_level", dest="court_level", help="法院级别，如「最高法院 高级法院 中级法院」")
    g3.add_argument("--court-area", "--court_area", dest="court_area", help="法院地域")
    g3.add_argument("--date-start", "--judgment_date_start", dest="date_start", default="20200101", help="裁判日期起 YYYYMMDD，默认20200101")
    g3.add_argument("--date-end", "--judgment_date_end", dest="date_end", help="裁判日期止 YYYYMMDD")
    g3.add_argument("--result-type", "--result_type", dest="result_type", nargs="+", help="裁判结果类型（v2）")

    g4 = p.add_argument_group("刑事专用（v3）")
    g4.add_argument("--crime", help="罪名，空格分隔。单独使用匹配案由；配合金额匹配指控罪名；配合量刑匹配审定罪名")
    g4.add_argument("--amount-min", "--involved_amount_min", dest="amount_min", type=int, help="涉案金额下限（元，闭区间）")
    g4.add_argument("--amount-max", "--involved_amount_max", dest="amount_max", type=int, help="涉案金额上限（元，闭区间）")
    g4.add_argument("--penalty-codes", "--penalty_codes", dest="penalty_codes",
                    help="量刑编码，空格分隔。大类1-5主刑、6-9附加刑；同刑种取OR，跨刑种取AND；支持父码覆盖子码")

    g5 = p.add_argument_group("输出控制")
    g5.add_argument("--limit", type=int, default=20, help="普通案例条数，默认20，上限1000")
    g5.add_argument("--instruct-limit", "--instruct_limit", dest="instruct_limit", type=int, default=5,
                    help="入库案例条数，默认5。类案检索应同时开启两者")
    g5.add_argument("--min-score", "--minScore", dest="min_score", type=float, help="最低相似度 0-1")
    g5.add_argument("--max-field-chars", dest="max_field_chars", type=int, default=1500)
    g5.add_argument("--max-total-chars", dest="max_total_chars", type=int, default=50000)
    g5.add_argument("--endpoint", help="覆盖默认端点路径")
    g5.add_argument("--raw", action="store_true", help="输出原始响应，跳过后处理")

    args = p.parse_args()

    for name, lo, hi in [("limit", 0, 1000), ("instruct_limit", 0, 1000)]:
        v = getattr(args, name)
        if v is not None and not (lo <= v <= hi):
            print(f"错误：--{name.replace('_','-')} 需在 {lo}-{hi} 之间", file=sys.stderr)
            sys.exit(2)

    warnings = []
    tier = detect_tier(args)
    path = args.endpoint or {1: ENDPOINT_V1, 2: ENDPOINT_V2, 3: ENDPOINT_V3}[tier]

    for name, val in [("summary", args.summary), ("discovery", args.discovery),
                      ("claim", args.claim), ("focus", args.focus), ("cvector", args.cvector)]:
        if val and len(val) < 100:
            warnings.append(f"--{name} 仅 {len(val)} 字，低于建议的 100 字；"
                            f"语义向量过短会显著降低召回质量，建议补足案情要素")
        if val and re.search(r"案例|问题|分析", val):
            warnings.append(f"--{name} 含「案例/问题/分析」等元词汇，会污染语义向量，建议改写为纯案情陈述")

    check_date_milestone(args, warnings)
    check_specialized_court(args, warnings)

    if args.crime:
        for c in args.crime.split():
            if not c.endswith("罪"):
                warnings.append(
                    f"--crime「{c}」不以「罪」结尾。罪名须用案由树标准全称，"
                    f"映射失败时服务端静默返回空结果（不报错）。"
                    f"口语转换：醉驾→危险驾驶罪、非法集资→非法吸收公众存款罪或集资诈骗罪、"
                    f"帮信→帮助信息网络犯罪活动罪"
                )

    if tier == 2 and args.summary_weight is None and args.summary is None and args.cvector is None \
            and not args.keywords:
        warnings.append("v2 向量模式下 summary_vector 与 summary_weight 缺失可能导致入库案例不返回结果")

    try:
        raw = request_api(path, build_body(args))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"HTTP {e.code}: {body}", file=sys.stderr)
        if tier > 1:
            print(f"提示：v{tier} 端点 {path} 可能与后端实际路径不符，可用 --endpoint 指定", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"请求失败：{e}", file=sys.stderr)
        sys.exit(1)

    if args.raw:
        print(json.dumps(raw, ensure_ascii=False, indent=2))
        return

    results = postprocess(raw, args, warnings)
    if not results:
        warnings.append("无结果。不可据此断言「不存在此类案例」，须先排除以下原因："
                        "① 普通案例存在 version 数据守卫（契约 G-8），部分早期案例不可检索；"
                        "② 过滤条件过严（尤其 court_level 排除了专门法院）")
        if args.crime:
            warnings.append(
                f"⚠️ 本次使用了 --crime「{args.crime}」。罪名无法映射到案由编码时，"
                f"服务端会静默返回空结果。请先去掉 --crime 仅用语义检索复核一次，"
                f"再判断是否确实无此类案例"
            )

    print(json.dumps(
        {"count": len(results), "api_version": f"v{tier}", "warnings": warnings, "results": results},
        ensure_ascii=False, indent=2
    ))


if __name__ == "__main__":
    main()
