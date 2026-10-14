# 平台使用与恢复手册

核实基线：2026-10-14 Skill 中心公开活动与仓库实现；每次使用仍检查当前状态。

| 需求 | 现有入口 | 能核实的事实 |
| --- | --- | --- |
| 查看介绍 | `https://skill.zhiexa.com/?campaign=enterprise-service-season&tab=intro` | 本期说明与当前能力 |
| 查看安排及更新 | `https://skill.zhiexa.com/?campaign=enterprise-service-season&tab=schedule` | 服务端目标日期、实际已发布更新 |
| 恢复本人任务 | `https://skill.zhiexa.com/?campaign=enterprise-service-season&tab=records` | 登录后本人获授权记录 |
| 安装辅助技能 | `https://skill.zhiexa.com/skills?section=market` | 查看专区清单并一键补齐，已有及基础技能跳过 |
| 查企业公开信息 | 工作台「企业公开信息核查」及活动实际执行入口 | 独立能力开关、主体确认、预算及报告待审状态 |
| 保存与继续 | 原会话成果卡审核并保存，进入现有项目普通对话 | 以服务端成功回执为准 |
| 回看公告 | 更多 → 系统公告 | 管理员实际发布的内容 |

活动常驻入口：更多 → 我的活动 → 活动安排。关闭首页卡不会删除已有成果。
基础法律检索与核验技能无需再次安装。辅助技能安装保留已有锁版本，失败逐项重试。
一键安装完成后返回对话；安装不代表已经运行某技能，也不更改查询功能、版本验收或权益。

平台技能先 `use_skill` 再执行，长参考资料按需读取。文件与项目机制仅在当前宿主提供且
用户有权时使用。客户档案、自动按期间服务账本、风险订阅和客户专用审查口径以实际验收
为准；本技能仅组织已有机制，不提供额外后台 API，不直接持有查询凭据。

来源：平台活动页（以上链接）；
[平台设计与开放状态](https://github.com/zhihe-ai-dev/sandbox-conversation-webpage/blob/master/docs/enterprise-toolbox-prd.md)；
[运营流程](https://github.com/zhihe-ai-dev/sandbox-conversation-webpage/blob/master/docs/enterprise-service-season-operations.md)。
仓库 PRD 包含未来需求，不能全部当成已上线能力。规则与费用只取当前界面和实际回执。
