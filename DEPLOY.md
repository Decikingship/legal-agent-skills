# 部署说明

## 一、目录职责

| 目录 | 是否部署到平台 | 说明 |
|---|---|---|
| `conventions/` | ❌ **不部署** | 研发期唯一事实来源。改规范只改这里 |
| `skills/` | ❌ 不直接部署 | 源码。SKILL.md 里写 `conventions/xxx.md` |
| `dist/` | ✅ **部署这个** | `build_skills.py` 产出，已注入 references、已改写路径，自包含 |
| `infra/` | ✅ 部署（云函数） | 代理层 |

**为什么不直接部署 `skills/`**：平台上每个 skill 是独立目录，跨目录的 `conventions/xxx.md` 相对路径不存在，模型读不到会当成没有该文件。构建期注入解决这个问题，同时保持规范单一来源。

## 二、构建

```bash
python3 build_skills.py           # 产出 dist/
python3 build_skills.py --check   # 只校验有无悬空引用，CI 里跑这个
```

改了 `conventions/` 之后必须重新构建，否则 dist 里是旧副本。注入的文件带校验和横幅，能看出是否过期。

## 三、上线顺序（不要跳步）

### 第 1 步：先发代理，别动 skill
部署 `infra/zhiexa_proxy.py`，配置环境变量 `ZHIEXA_TOKEN`（原代码是硬编码，现改为读环境变量）。

验证：
```bash
curl https://<fc-url>/          # 健康检查，看 token_configured 和 routes
```

此时旧 skill 仍在跑，`/case/vector`、`/law/vector` 行为不变，**零风险**。

### 第 2 步：验证 v2/v3 路由确实通
```bash
curl -X POST https://<fc-url>/case/vector2 -H 'Content-Type: application/json' \
  -d '{"search_mode":"mvector","summary_vector":"<≥100字案情>","summary_weight":1,"limit":3}'
```
返回 404 说明上游没有该路由，需找后端确认；返回 200 才能进第 3 步。
（`law/vector2` 是 POST 不是 GET，别用 GET 试。）

### 第 3 步：灰度替换 skill
两个 skill 的 `name` 字段保持为「智合AI搜法」「智合AI搜案」不变，平台内引用不受影响。
CLI 已为旧参数名（`--case_code`、`--court_name`、`--judgment_date_start`、`--lawName`、`--areaFacet` 等）保留别名，**旧调用方式全部继续可用**。

建议先小流量跑 evals 里的用例，重点看：
- 返回体里的 `warnings` 数组是否合理
- `_cite` 字段能否直接用于引注
- v1 路径（`--cvector` 单一向量）是否与旧版结果一致

### 第 4 步：全量

## 四、回滚

旧 skill 保留一份即可秒回。新版对 v1 路径是纯增强（多了防护和 warnings），不改变 v1 请求语义。

## 五、遗留的后端改动建议

| 编号 | 事项 | 影响 |
|---|---|---|
| D-19 | `penalty_codes` 字典缺失 | 已绕开，非阻塞 |
| D-20 | `reference_level` 不是请求入参 | 「只要指导性案例」做不到，低位阶结果挤占 limit |
| D-21 | 案由/罪名树无查询接口 | 罪名写错时静默返回空，客户端只能弱校验 |
| D-17 | law 路由 `try/except` 吞异常返回 HTTP 200 + code=500 | 已在 CLI 侧拦截，建议后端返回真实状态码 |

## 六、端点与鉴权

开源版已将原部署地址替换为占位地址。使用检索功能前，必须通过环境变量配置你有权访问、且兼容本项目接口的服务：

| 变量 | 默认 | 说明 |
|---|---|---|
| `ZHIHE_BASE_URL` | `https://your-legal-api.example.com` | 占位地址，使用前必须替换 |
| `ZHIHE_TOKEN` | 空 | 留空则**不发送** `Authorization` 头 |

这些环境变量由脚本直接读取；项目不会自动加载 `.env` 文件。

**调用检索服务时**：
```bash
export ZHIHE_BASE_URL="https://your-legal-api.example.com"
export ZHIHE_TOKEN="<your-access-token>"  # 仅在你的服务要求鉴权时设置；从密钥管理注入
```

代理层（`infra/zhiexa_proxy.py`）使用 `ZHIEXA_API_ROOT` / `ZHIEXA_TOKEN` 访问上游。开源版的上游默认地址为 `https://your-upstream-api.example.com`，部署前也必须配置。调用方的 `ZHIHE_TOKEN` 和代理访问上游的 `ZHIEXA_TOKEN` 属于不同层级。

本项目不包含第三方数据服务账号、访问凭据或数据授权。第三方服务的使用须遵守其各自的授权和条款。
