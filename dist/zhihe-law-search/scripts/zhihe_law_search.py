#!/usr/bin/env python3
"""
智合AI搜法 CLI — 法律法规检索

对接 LawVectorService。相对旧版新增：
  - v2 能力：发布/实施日期范围过滤、书籍库（法条释义）召回
  - 精确法规名定位（客户端复刻 search_law 的标题归一化匹配）
  - 内置确定性防护：无 lawId 剔除、草案识别、时效性标注、超链接生成、字符量控制

用法示例：
    # 主题语义检索（最常用）
    zhihe_law_search.py --vector "用人单位单方解除劳动合同的法定情形" \
        --xls "001 002" --area "全国 上海" --current-only --rows 10

    # 按法规名定位（自动还原简称需调用方完成）
    zhihe_law_search.py --law-name-exact "中华人民共和国民法典"

    # 取指定法规的指定条文
    zhihe_law_search.py --law-id "<lawId>" --rawnumber "第五百八十五条"

    # 取某法条的释义/评注（书籍库 exact 模式）
    zhihe_law_search.py --vector "违约金调减" --rawnumber "第五百八十五条" \
        --recall-mode exact --top-k 5

    # 新规追踪：某时间窗内施行的规定
    zhihe_law_search.py --vector "数据出境安全评估" \
        --implement-date-start 20250101 --current-only
"""

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
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

DETAIL_URL = "https://www.zhiexa.com/fuxi/legislation/detail?id={}"

# 代理层路径（代理再映射到上游 /search/law/vector 等）。
# v1=GET，v2=POST，verify=POST，方法不同不可混用。
ENDPOINT_V1 = "/law/vector"
ENDPOINT_V2 = "/law/vector2"
ENDPOINT_VERIFY = "/law/verify"

TIMELINESS_CURRENT = "1"  # 现行有效
TIMELINESS_AMENDED = "4"  # 已修改

# 书籍库可选返回字段（服务端 BOOK_ALLOWED_FIELDS）。
# 注意：模型 example 里写的 law_name_full 是数据库列名，不是 API 字段名，传了会被判非法。
BOOK_ALLOWED_FIELDS = [
    "lawName", "rawnumber", "content",
    "article_gist",              # 条文主旨
    "article_interpretation",    # 条文理解
    "judicial_practice_notes",   # 审判实践中应注意的问题
    "page_start", "page_end",
    "book_name", "editor",
]

# 草案类标题特征。服务端仅在切片模式过滤，标题模式需客户端兜底（契约 G-7）
DRAFT_RE = re.compile(r"草案|征求意见稿|试行稿|送审稿")
AMEND_DECISION_RE = re.compile(r"关于修改.*决定")

_HTML_TAG = re.compile(r"<[^>]+>")
_REV_TAIL = re.compile(r"[(（][^（）()]{0,40}(?:修订|修正|改正)[^（）()]{0,20}[)）]\s*$")
_YEAR_TAIL = re.compile(r"[(（][0-9]{4}.{0,10}[)）]\s*$")


def strip_marks(name: str) -> str:
    t = (name or "").strip()
    if t.startswith("《") and "》" in t:
        i = t.index("》")
        t = t[1:i] + t[i + 1:]
    return t.replace("中华人民共和国国务院令", "国务院令").strip()


def norm_title(name: str) -> str:
    """标题比对键：去 HTML / 书名号 / 空白 / 结尾修订后缀。不做缩写模糊匹配。"""
    t = _HTML_TAG.sub("", name or "")
    t = strip_marks(t).strip("《》").strip()
    t = _REV_TAIL.sub("", t).strip()
    t = _YEAR_TAIL.sub("", t).strip()
    return re.sub(r"\s+", "", t)


V1_FIELDS = [
    ("vector", "vector"), ("keyword", "keyword"), ("law_name", "lawName"),
    ("law_id", "lawId"), ("filenum", "filenum"), ("rawnumber", "rawnumber"),
    ("rows", "rows"), ("xls", "xls"), ("xls_excl", "xls_excl"),
    ("topics", "topicsFacet"), ("area", "areaFacet"), ("dept", "fbdwFacet"),
    ("timeliness", "lawstatexlsFacet"), ("vector_target", "vector_target"),
    ("min_score", "minScore"), ("max_per_law", "maxSingleLawItemCount"),
]

V2_FIELDS = [
    ("publish_date_start", "publish_date_start"), ("publish_date_end", "publish_date_end"),
    ("implement_date_start", "implement_date_start"), ("implement_date_end", "implement_date_end"),
    ("recall_mode", "recall_mode"), ("top_k", "top_k"),
    ("book_min_score", "book_min_score"), ("fields", "fields"),
    ("return_mode", "return_mode"),
]


def build_params(args, use_v2):
    params = {}
    for attr, key in V1_FIELDS:
        v = getattr(args, attr, None)
        if v is not None:
            params[key] = v
    if args.current_only and "lawstatexlsFacet" not in params:
        params["lawstatexlsFacet"] = TIMELINESS_CURRENT
    if use_v2:
        for attr, key in V2_FIELDS:
            v = getattr(args, attr, None)
            if v is not None:
                params[key] = v
    return params


def request_api(path, params, timeout=90):
    """v1 是 GET（查询参数），v2 是 POST（请求体）——上游路由声明不同，不可混用。"""
    if path == ENDPOINT_V2:
        req = urllib.request.Request(
            f"{BASE_URL}{path}",
            data=json.dumps(params).encode("utf-8"),
            headers=auth_headers(),
            method="POST",
        )
    else:
        req = urllib.request.Request(
            f"{BASE_URL}{path}?{urllib.parse.urlencode(params)}",
            headers=auth_headers(),
            method="GET",
        )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = json.loads(resp.read().decode("utf-8"))

    # 上游 law 路由捕获所有异常后返回 code="500" 但 HTTP 状态仍是 200。
    # 不显式检查的话，内部错误会被当成「检索无结果」静默吞掉——这是最危险的一类失败。
    code = str((raw or {}).get("code", "200"))
    if code != "200":
        raise RuntimeError(
            f"上游返回错误 code={code} msg={(raw or {}).get('msg')}；"
            f"这是服务端内部错误，不是「无检索结果」，不得据此断言法规不存在"
        )
    return raw


def postprocess(raw, args, warnings):
    """确定性防护：剔除无 lawId、标注草案与失效、生成超链接、控制字符量。"""
    data = (raw or {}).get("data") or {}
    items = data.get("result") or []

    dropped_no_id = 0
    out = []
    total_chars = 0

    for it in items:
        law_id = it.get("lawId")
        if not law_id:
            dropped_no_id += 1
            continue

        name = it.get("lawName") or ""
        flags = []

        if DRAFT_RE.search(name) or AMEND_DECISION_RE.search(name):
            flags.append("疑似草案/征求意见稿/修改决定，不得作为裁判依据")

        timeliness = it.get("timeliness") or ""
        if not timeliness:
            flags.append("时效性字段缺失，效力状态需人工复核")
        elif "现行有效" not in timeliness:
            flags.append(f"非现行有效：{timeliness}")

        hisgroup = it.get("hisgroup")
        if hisgroup:
            flags.append("该法规存在历史版本，涉及历史行为时须做行为时法/现行法双轨判断")

        content = it.get("content") or ""
        if args.max_field_chars and len(content) > args.max_field_chars:
            content = content[: args.max_field_chars] + "……〔已截断〕"

        rec = {
            "lawId": law_id,
            "lawName": name,
            "rawnumber": it.get("rawnumber") or "",
            "content": content,
            "timeliness": timeliness,
            "xls": it.get("xls") or "",
            "issuingOrgan": it.get("issuingOrgan") or "",
            "filenum": it.get("filenum") or "",
            "publishDate": it.get("releaseYearMonthDate") or "",
            "implementDate": it.get("implementYearMonthDate") or "",
            "topName": it.get("topName") or "",
            "hisgroup": hisgroup,
            "score": it.get("score"),
            "source": it.get("source") or "law",
            "article_gist": it.get("article_gist"),
            "article_interpretation": it.get("article_interpretation"),
            "judicial_practice_notes": it.get("judicial_practice_notes"),
            "book_name": it.get("book_name"),
            "editor": it.get("editor"),
            "page_start": it.get("page_start"),
            "page_end": it.get("page_end"),
            "_link": DETAIL_URL.format(law_id),
            "_cite": "[《{}》]({}){}".format(
                norm_title(name), DETAIL_URL.format(law_id), it.get("rawnumber") or ""
            ),
            "_flags": flags,
        }

        rec_chars = len(json.dumps(rec, ensure_ascii=False))
        if args.max_total_chars and total_chars + rec_chars > args.max_total_chars:
            warnings.append(
                f"已达字符上限 {args.max_total_chars}，截断于第 {len(out)} 条；"
                f"如需更多请缩小检索范围或分次调用"
            )
            break
        total_chars += rec_chars
        out.append(rec)

    if dropped_no_id:
        warnings.append(f"剔除 {dropped_no_id} 条无 lawId 的结果（平台硬约束：无 ID 禁止引用）")

    if args.vector_target == "title" and not args.rawnumber:
        warnings.append(
            "标题模式下服务端不过滤草案与征求意见稿（契约 G-7），已按标题特征标记，请逐条核对"
        )

    book_n = sum(1 for r in out if r.get("source") == "book")
    if book_n:
        warnings.append(
            f"含 {book_n} 条书籍库（法条释义/评注）结果：属第三方出版物，"
            f"引用须转述而非整段照搬，且不得作为裁判依据"
        )
    if getattr(args, "recall_mode", None) and not book_n:
        warnings.append("已请求书籍库召回但无结果，可能是书籍库降级或该条无收录释义")

    return out


def exact_law_lookup(args, warnings):
    """客户端复刻 search_law：按名称拉候选后做标题归一化精确对齐。"""
    query_key = norm_title(args.law_name_exact)
    if not query_key:
        return []
    params = {
        "lawName": strip_marks(args.law_name_exact),
        "rows": min(max(args.rows * 10, args.rows), 100),
        "lawstatexlsFacet": args.timeliness or TIMELINESS_CURRENT,
        "vector_target": "title",
    }
    raw = request_api(ENDPOINT_V1, params)
    matched = []
    for it in (raw.get("data") or {}).get("result") or []:
        if norm_title(it.get("lawName") or "") == query_key:
            matched.append(it)
    if not matched:
        warnings.append(
            f"未找到与「{args.law_name_exact}」标题完全一致的法规。"
            f"精确定位不做缩写与模糊匹配，请确认是否已将简称还原为全称"
        )
    return {"code": "200", "data": {"result": matched[: args.rows]}}


def verify_laws(args, warnings):
    """批量法规存在性/时效性核查。

    走 internal/search/law/verify：不做 embedding、不查切片库，
    按法规名 + 时效性直查主表，且带兜底源（主库无结果时回落）。
    比自行读 timeliness 判断可靠，是 core-verify-statute-validity 的首选路径。
    """
    body = {
        "queries": args.verify,
        "rows": args.verify_rows,
        "lawstatexlsFacet": args.timeliness or TIMELINESS_CURRENT,
        "source": args.verify_source,
    }
    req = urllib.request.Request(
        f"{BASE_URL}{ENDPOINT_VERIFY}",
        data=json.dumps(body).encode("utf-8"),
        headers=auth_headers(),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        raw = json.loads(resp.read().decode("utf-8"))

    if str(raw.get("code", "200")) != "200":
        raise RuntimeError(f"核查失败 code={raw.get('code')} msg={raw.get('msg')}")

    out = []
    for item in raw.get("items") or []:
        results = ((item.get("data") or {}).get("result")) or []
        hits = []
        for r in results:
            if not r.get("lawId"):
                continue
            hits.append({
                "lawId": r.get("lawId"),
                "lawName": r.get("lawName"),
                "timeliness": r.get("timeliness"),
                "xls": r.get("xls"),
                "issuingOrgan": r.get("issuingOrgan"),
                "filenum": r.get("filenum"),
                "publishDate": r.get("releaseYearMonthDate"),
                "implementDate": r.get("implementYearMonthDate"),
                "hisgroup": r.get("hisgroup"),
                "_link": DETAIL_URL.format(r["lawId"]),
            })
        verdict = "存在" if hits else "未核实到"
        if not hits:
            warnings.append(
                f"「{item.get('query')}」未核实到：可能名称有误、已废止未收录，或该法规并不存在。"
                f"不得据此认定其内容，也不得凭记忆补充该法规的条文"
            )
        out.append({
            "query": item.get("query"),
            "verdict": verdict,
            "hit_count": len(hits),
            "hits": hits,
        })
    return out


def main():
    p = argparse.ArgumentParser(description="智合AI搜法（旧版驼峰参数名仍兼容）", formatter_class=argparse.RawDescriptionHelpFormatter)

    g1 = p.add_argument_group("检索入口")
    g1.add_argument("--vector", help="语义检索词（≥15字，禁含法规名称与条文编号）")
    g1.add_argument("--vector-target", "--vector_target", dest="vector_target", default="slice_content",
                    choices=["slice_content", "title"], help="向量化对象，默认按法条内容")
    g1.add_argument("--keyword", help="标题短语匹配，空格分隔多词取交集（注意：只搜标题不搜正文）")
    g1.add_argument("--law-name", "--lawName", dest="law_name", help="法规名称短语匹配")
    g1.add_argument("--law-name-exact", dest="law_name_exact",
                    help="法规名精确定位：标题归一化后完全相等才返回，不做缩写模糊匹配")
    g1.add_argument("--law-id", "--lawId", dest="law_id", help="法规ID，空格分隔可传多个")
    g1.add_argument("--rawnumber", help="条号，如「第五百八十五条」，中文/阿拉伯数字均可")
    g1.add_argument("--filenum", help="文号，如「法释[2018]10号」")

    g2 = p.add_argument_group("过滤")
    g2.add_argument("--xls", help="效力级别，路径数组匹配，传父码即覆盖子级。001法律 002司法解释 003行政法规 005部门规章 011地方性法规")
    g2.add_argument("--xls-excl", "--xls_excl", dest="xls_excl", help="排除的效力级别")
    # 注：请求模型另有 xlsFirst3 字段，但服务层过滤器并未使用，属无效参数，故不暴露
    g2.add_argument("--timeliness", "--lawstatexlsFacet", help="时效性代码，1=现行有效")
    g2.add_argument("--current-only", action="store_true", help="仅现行有效（等价 --timeliness 1）")
    g2.add_argument("--area", "--areaFacet", help="地域，名称包含匹配。建议「全国 上海」同时给全国与地方")
    g2.add_argument("--topics", "--topicsFacet", help="行业分类")
    g2.add_argument("--dept", "--fbdwFacet", help="发文机关")
    g2.add_argument("--publish-date-start", dest="publish_date_start", help="发布日期起 YYYYMMDD")
    g2.add_argument("--publish-date-end", dest="publish_date_end", help="发布日期止 YYYYMMDD")
    g2.add_argument("--implement-date-start", dest="implement_date_start", help="施行日期起 YYYYMMDD")
    g2.add_argument("--implement-date-end", dest="implement_date_end", help="施行日期止 YYYYMMDD")

    g3 = p.add_argument_group("书籍库（法条释义/评注）")
    g3.add_argument("--recall-mode", dest="recall_mode", choices=["exact", "semantic"],
                    help="exact 需配合 --rawnumber，取该条释义；semantic 按内容语义召回")
    g3.add_argument("--top-k", dest="top_k", type=int, help="书籍召回条数")
    g3.add_argument("--book-min-score", dest="book_min_score", type=float, help="书籍相似度阈值")
    g3.add_argument("--fields", help="书籍返回字段，空格分隔")
    g3.add_argument("--return-mode", dest="return_mode", help="full 返回全部书籍字段")

    gv = p.add_argument_group("批量核查（法规存在性与时效性）")
    gv.add_argument("--verify", nargs="+", metavar="LAWNAME",
                    help="批量核查法规名是否存在及其时效性，1-50 个。用于校验他人文书或模型输出中的法规引用")
    gv.add_argument("--verify-rows", dest="verify_rows", type=int, default=5,
                    help="每个查询返回条数，1-100，默认5")
    gv.add_argument("--verify-source", dest="verify_source", default="auto",
                    choices=["auto", "primary", "fallback"],
                    help="auto 主库优先无结果兜底（默认）；primary 仅主库；fallback 仅兜底源")

    g4 = p.add_argument_group("输出控制")
    g4.add_argument("--rows", type=int, default=10, help="返回条数，默认10，上限1000")
    g4.add_argument("--min-score", "--minScore", dest="min_score", type=float, help="最小相似度")
    g4.add_argument("--max-per-law", "--maxSingleLawItemCount", dest="max_per_law", type=int, help="单部法规最多返回几条，防霸屏")
    g4.add_argument("--max-field-chars", dest="max_field_chars", type=int, default=2000)
    g4.add_argument("--max-total-chars", dest="max_total_chars", type=int, default=50000)
    g4.add_argument("--endpoint", help="覆盖默认端点路径")
    g4.add_argument("--raw", action="store_true", help="输出原始响应，跳过后处理")

    args = p.parse_args()

    if args.rows is not None and not (0 <= args.rows <= 1000):
        print("错误：--rows 需在 0-1000 之间", file=sys.stderr)
        sys.exit(2)

    if args.recall_mode == "exact" and not args.rawnumber:
        print("错误：--recall-mode exact 需同时提供 --rawnumber，否则书籍库不返回结果", file=sys.stderr)
        sys.exit(2)

    # return_mode 缺省时服务端取 request.fields.split()，为空列表 → 书籍结果只剩空壳。
    # 既然请求了书籍库，就默认给 full，避免拿到一堆没有正文的记录。
    if args.recall_mode and not args.return_mode:
        args.return_mode = "full"
    if args.return_mode == "fields" and not args.fields:
        print("错误：--return-mode fields 需同时提供 --fields，否则书籍结果为空",
              file=sys.stderr)
        sys.exit(2)
    if args.fields:
        bad = [f for f in args.fields.split() if f not in BOOK_ALLOWED_FIELDS]
        if bad:
            print(f"错误：--fields 含非法字段 {bad}；可选值：{' '.join(BOOK_ALLOWED_FIELDS)}",
                  file=sys.stderr)
            sys.exit(2)

    warnings = []

    if args.verify:
        if not (1 <= len(args.verify) <= 50):
            print("错误：--verify 需 1-50 个法规名", file=sys.stderr)
            sys.exit(2)
        try:
            items = verify_laws(args, warnings)
        except RuntimeError as e:
            print(f"上游内部错误：{e}", file=sys.stderr)
            sys.exit(3)
        except Exception as e:
            print(f"请求失败：{e}", file=sys.stderr)
            sys.exit(1)
        print(json.dumps({"mode": "verify", "count": len(items),
                          "warnings": warnings, "items": items},
                         ensure_ascii=False, indent=2))
        return

    use_v2 = any(getattr(args, a) is not None for a, _ in V2_FIELDS)
    path = args.endpoint or (ENDPOINT_V2 if use_v2 else ENDPOINT_V1)

    try:
        if args.law_name_exact:
            raw = exact_law_lookup(args, warnings)
        else:
            raw = request_api(path, build_params(args, use_v2))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"HTTP {e.code}: {body}", file=sys.stderr)
        if use_v2:
            print("提示：v2 走 POST /law/vector2，请确认代理已放行该路由", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as e:
        print(f"上游内部错误：{e}", file=sys.stderr)
        sys.exit(3)
    except Exception as e:
        print(f"请求失败：{e}", file=sys.stderr)
        sys.exit(1)

    if args.raw:
        print(json.dumps(raw, ensure_ascii=False, indent=2))
        return

    results = postprocess(raw, args, warnings)
    print(json.dumps(
        {"count": len(results), "warnings": warnings, "results": results},
        ensure_ascii=False, indent=2
    ))


if __name__ == "__main__":
    main()
