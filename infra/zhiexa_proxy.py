import os

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

# 上游应用根。注意 API 根之下有两个并列前缀：
#   search/...           公开检索路由
#   internal/search/...  内部核查路由（include_in_schema=False，但同样校验 public token）
# 因此不能用单一前缀拼接，必须显式映射。
API_ROOT = os.environ.get(
    "ZHIEXA_API_ROOT", "https://your-upstream-api.example.com"
)
TOKEN = os.environ.get("ZHIEXA_TOKEN", "")
HEADERS = {"Content-Type": "application/json"}

# 代理对外路径 -> (HTTP 方法, 上游路径)
# 方法逐条声明：law/vector 是 GET（查询参数依赖注入），law/vector2 是 POST（请求体模型），
# 两者不一致，靠前缀推断必然出错。
ROUTES = {
    "law/vector":   ("GET",  "search/law/vector"),
    "law/vector2":  ("POST", "search/law/vector2"),
    "law/verify":   ("POST", "internal/search/law/verify"),
    "case/vector":  ("POST", "search/case/vector"),
    "case/vector2": ("POST", "search/case/vector2"),
    "case/vector3": ("POST", "search/case/vector3"),
}

# 上游耗时分级：
#   law/vector, law/verify   单次向量检索或批量核查，通常 <10s
#   case/vector              embedding + 检索 + 重排，5-15s
#   case/vector2, vector3    最多 4 次 embedding + hybrid_search 多路召回，可达 30-60s
#   law/vector2              法规检索叠加书籍库召回
# 原 timeout=30 会在 vector2/vector3 上大概率截断。
HEAVY = {"case/vector2", "case/vector3", "law/vector2"}
TIMEOUT_DEFAULT = 30
TIMEOUT_HEAVY = 90


def _forward(alias: str):
    route = ROUTES.get(alias)
    if not route:
        return (
            jsonify({"error": "unknown path", "path": alias,
                     "known_paths": sorted(ROUTES)}),
            404,
        )

    method, upstream_path = route
    if request.method != method:
        return (
            jsonify({"error": "method not allowed", "path": alias,
                     "expected": method, "got": request.method}),
            405,
        )

    url = f"{API_ROOT}/{upstream_path}"
    timeout = TIMEOUT_HEAVY if alias in HEAVY else TIMEOUT_DEFAULT

    try:
        if method == "GET":
            params = dict(request.args)
            params["token"] = TOKEN
            resp = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
        else:
            body = request.get_json(silent=True) or {}
            resp = requests.post(url, params={"token": TOKEN}, json=body,
                                 headers=HEADERS, timeout=timeout)
    except requests.Timeout:
        # 与其它错误区分开：调用方据此判断是该减少检索维度还是重试
        return (
            jsonify({"error": "upstream timeout", "path": alias,
                     "timeout_seconds": timeout,
                     "hint": "分维度检索维度过多或 limit 过大，建议减少维度或降低 limit 后重试"}),
            504,
        )
    except requests.RequestException as e:
        return jsonify({"error": "upstream request failed", "detail": str(e)}), 502

    return (resp.content, resp.status_code, {"Content-Type": "application/json"})


@app.route("/case/vector", methods=["POST"])
def case_vector():
    return _forward("case/vector")


@app.route("/case/vector2", methods=["POST"])
def case_vector2():
    return _forward("case/vector2")


@app.route("/case/vector3", methods=["POST"])
def case_vector3():
    return _forward("case/vector3")


@app.route("/law/vector", methods=["GET"])
def law_vector():
    return _forward("law/vector")


@app.route("/law/vector2", methods=["POST"])
def law_vector2():
    return _forward("law/vector2")


@app.route("/law/verify", methods=["POST"])
def law_verify():
    """批量法规存在性与时效性核查，供 core-verify-statute-validity 使用。"""
    return _forward("law/verify")


@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "ok": True,
        "token_configured": bool(TOKEN),
        "api_root": API_ROOT,
        "routes": {k: {"method": v[0], "upstream": v[1]} for k, v in ROUTES.items()},
    })


@app.route("/<path:path>", methods=["GET", "POST", "PUT", "DELETE"])
def fallback(path):
    return jsonify({"error": "not found", "path": path,
                    "known_paths": sorted(ROUTES)}), 404


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9000)
