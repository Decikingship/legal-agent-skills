# Legal Agent Skills

面向中文法律工作的 AI 技能与专家工作流集合，包含 **70 个技能、34 个专家配置**，以及 Python 辅助脚本、模板、评估用例和可部署包。技能覆盖法律检索、诉讼分析、合同审查、企业合规、外国企业来华业务与文书处理。

## 内容概览

| 分组 | 数量 | 主要能力 |
| --- | ---: | --- |
| [底座技能](skills/core/) | 6 | 法规与案例检索、法条核验、案例援引与论证审查、法律计算 |
| [诉讼技能](skills/litigation/) | 27 | 接案研判、类案检索、请求权与证据分析、文书起草、庭审、执行及专项争议 |
| [非诉技能](skills/nonlitigation/) | 24 | 合同审查、尽职调查、公司治理、劳动与数据合规、监管、来华业务及交易文件 |
| [工具技能](skills/utility/) | 13 | 脱敏、编目、合同对比、文书排版、结构图、翻译、培训、企业服务季协作与进博宣讲 |
| [专家工作流](experts/) | 34 | 按业务场景组合技能，定义任务前提、执行顺序和交付要求 |

完整技能名称见 [技能命名对照表](conventions/skill-naming.md)。目录名是稳定的技术标识，`SKILL.md` 中的 `name` 是中文展示名称。

## 企业服务季

[企业服务季全程协作](skills/utility/util-enterprise-service-season/) 从已有任务恢复，组织了解企业、首次联系、顾问洽谈、真实服务、客户跟进与续约报价。技能配有平台使用手册、工作簿及三类评估用例；在其他宿主运行时使用其已有文件、对话和项目机制。

[市场安装入口](https://skill.zhiexa.com/skills?section=market&tag=enterprise-service-season) 提供标签筛选和可选辅助技能的一键补齐。安装不改变活动资格、费用、查询开放状态或已有锁定版本。客户联系、委托、法律意见和报价由律师决定。

## 外国企业来华法律服务

官方九项技能组成“准入—落地—经营—展会—持续服务”的工作流，并配有[来华经营法律服务专家](experts/expert-china-business/)。这是官方技能开源活动的来华业务专题，也是 Skill 中心参与进博会宣讲的演示套件；具体参展审核、展位与宣讲时间以实际安排为准。

| 技能 | 主要交付 |
| --- | --- |
| [来华业务统筹](skills/nonlitigation/nonlit-orchestrate-china-business/) | 受理事实、跨专项路线图、律师签核与交接 |
| [外资准入研判](skills/nonlitigation/nonlit-assess-china-investment-access/) | 地域/行业准入、许可及不同监管路径 |
| [来华设立规划](skills/nonlitigation/nonlit-plan-china-entity/) | 实体方案、治理、登记材料与设立后义务 |
| [来华合同审查](skills/nonlitigation/nonlit-draft-china-contracts/) | 双语条款、修改建议、谈判与争议安排 |
| [中国知产布局](skills/nonlitigation/nonlit-plan-china-ip/) | 权利盘点、检索与申请计划、商业秘密及展前披露 |
| [来华用工规划](skills/nonlitigation/nonlit-plan-china-employment/) | 雇佣模式、制度程序、派驻与工作居留事项 |
| [来华税关汇筹划](skills/nonlitigation/nonlit-plan-china-tax-customs-fx/) | 交易/货流/款流/票据核对与专业人员交接 |
| [来华数据合规](skills/nonlitigation/nonlit-assess-china-data/) | 数据流、角色、影响评估及出境路径证据 |
| [进博法律宣讲](skills/utility/util-run-ciie-legal-demo/) | 展前/展中/展后任务、虚构企业演示与宣讲材料 |

[一键安装专题](https://skill.zhiexa.com/skills?section=market&tag=foreign-enterprise-china) · [开源学习活动](https://skill.zhiexa.com/static/open-source-skills.html#foreign-enterprise-china)

每项技能提供来源台账、交付模板和正常/缺事实/规则变化评估用例。每次运行重新核验业务、地域和日期适用的官方规则，区分法律、办事指南、问答和征求意见稿；未取得依据的结论保持待核。研究草案、最终法律意见与实际申报状态分别记录。税务、关务、专利等事项按需要交相应专业人员协作。

源码及模板不包含客户材料、服务凭据或数据库访问权。“官方”表示 Skill 中心制作维护，不表示政府或进博会背书，也不承诺审批、税务优惠、获客或成交结果。

## 目录结构

```text
.
├── skills/             # 技能源文件：SKILL.md、scripts、assets、references、evals
├── experts/            # 专家配置与技能清单：EXPERT.md
├── conventions/        # 共享规范的维护来源
├── infra/              # 法律检索 API 代理示例
├── dist/               # 已注入共享规范与依赖的独立技能目录
├── dist-experts/       # .expert 专家包（ZIP 格式）
├── build_skills.py     # 校验、构建与专家打包
└── LICENSE             # MIT 许可证
```

本仓库根目录即原技能包的内容，无需进入额外的 `pkg/` 层级。源码、构建产物和专家包均随仓库提供。

## 获取与构建

使用 Python 3；构建脚本仅依赖 Python 标准库。

```bash
git clone https://github.com/Decikingship/legal-agent-skills.git
cd legal-agent-skills

# 只校验规范引用、专家技能清单与名称对应关系，不写文件
python build_skills.py --check

# 重新生成 dist/，注入共享规范及跨技能脚本依赖
python build_skills.py

# 使用 dist/ 中的技能重新生成 dist-experts/
python build_skills.py --expert
```

部分系统的命令名为 `python3`。构建会重建对应输出目录，因此修改技能请编辑 `skills/`，修改共享规范请编辑 `conventions/`，再依次校验、构建和打包。

部署单个技能时使用 `dist/<技能目录>/`：构建后的 `SKILL.md` 引用本目录的 `references/`，不再依赖仓库级的 `conventions/`。专家包包含 `EXPERT.md` 和该专家声明的技能，采用 `zhihe-expert/1` 格式；导入需要支持该格式的宿主平台。其他 Agent 系统可根据各自的技能加载方式接入 `SKILL.md`，并适配专家编排。

## 运行依赖与检索配置

多数辅助脚本只使用 Python 标准库。按需安装以下依赖：

```bash
# 仅法律文书 Word 排版需要
python -m pip install python-docx

# 仅运行 infra/zhiexa_proxy.py 代理需要
python -m pip install Flask requests
```

法律检索及依赖检索的核验、分析功能，需要使用者自行提供**已获授权的兼容 API 服务与凭据**。仓库不提供在线服务账号或法律数据库访问权，示例地址不能直接用于检索。

| 环境变量 | 使用位置 | 配置内容 |
| --- | --- | --- |
| `ZHIHE_BASE_URL` | 技能中的检索客户端 | 自有或已获授权的法律 API 基础地址；公开示例为 `https://your-legal-api.example.com` |
| `ZHIHE_TOKEN` | 技能中的检索客户端 | 服务要求的访问令牌；脚本将其用于 `Authorization` 请求头 |
| `ZHIEXA_API_ROOT` | `infra/zhiexa_proxy.py` | 已获授权的上游 API 根地址；公开示例为 `https://your-upstream-api.example.com` |
| `ZHIEXA_TOKEN` | `infra/zhiexa_proxy.py` | 上游服务访问令牌 |

在运行环境中设置这些变量；脚本通过 `os.environ` 读取配置，不会自动加载 `.env` 文件。接口路径、输入输出约定见 [检索协议](conventions/retrieval-contract.md) 和 [代理实现](infra/zhiexa_proxy.py)。具体技能的参数与输入格式见其 `SKILL.md` 和脚本 `--help`。

## 开发资料

- [部署说明](DEPLOY.md)
- [输出格式约定](conventions/output-formats.md)
- [技能模板](conventions/skill-template.md)与[专家模板](conventions/expert-template.md)
- [构建计划](LEGAL_AGENT_BUILD_PLAN.md)与[开发状态记录](BUILD_STATE.md)

## 许可证

本仓库采用 [MIT 许可证](LICENSE)。
