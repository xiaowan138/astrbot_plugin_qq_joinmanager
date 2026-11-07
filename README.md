# QQ官机入群管理器

基于 QQ 官方机器人 API 的 AstrBot 插件，用于自动审核入群申请、发送欢迎语、记录入群来源并提供人工审核入口。

> 本插件使用 `qq_official`（WebSocket）平台适配器，不依赖 OneBot，也不依赖 QQ 个人号框架。

## 功能

- 按验证消息 / 问答答案关键词自动通过、自动拒绝，不区分大小写
- 拒绝优先级高于同意；命中拒绝关键词时可同时加入群黑名单
- 分群规则：`default` 默认规则 + 具体群专属规则
- 群黑名单 / 白名单控制
- 自动通过欢迎语、手动审核欢迎语、退群提示
- 审核结果通知到指定群，`origin` 表示申请人所在群
- `/入群统计`：总入群、今日入群、自动 / 人工审核、来源分类、最近成员
- `/入群审核`：查看待处理申请、手动通过、手动拒绝
- 识别 QQ 原生自动审批策略通过的申请，避免重复审批
- 入群申请 ID 去重，记录文件原子写入

## 前置条件

1. AstrBot `>= 4.12.0`
2. 使用 `qq_official`（WebSocket）平台适配器，**不支持** `qq_official_webhook`
3. 机器人必须是目标群的**群管理员**，否则收不到 `GROUP_JOIN_REQUEST` 事件，审批接口也会失败
4. AstrBot 自带的 `qq-botpy`（AstrBot 依赖中为 1.2.1）即可，无需额外安装依赖

## 安装

将整个 `astrbot_plugin_qq_joinmanager` 目录放入：

```text
AstrBot/data/plugins/astrbot_plugin_qq_joinmanager
```

重启 AstrBot，或在 WebUI 的插件页面重载插件。

如果插件是在 AstrBot 运行期间才启用，插件会检测到 QQ 官方平台已连接但缺少群成员事件订阅，并自动重载一次该平台。

## 配置

在 WebUI 的插件配置页填写。主要配置项：

| 配置项 | 类型 | 说明 |
| --- | --- | --- |
| `delay` | float | 多条通知之间的发送间隔（秒） |
| `control.block_method` | string | `blacklist` 或 `whitelist` |
| `control.control_list` | list | 群列表，填 `group_openid` 或群名称 |
| `reject_blacklist` | bool | 拒绝时是否同时加入群黑名单 |
| `accept_rules` | template_list | 同意关键词规则 |
| `reject_rules` | template_list | 拒绝关键词规则 |
| `message_templates` | object | 欢迎语、拒绝理由、退群提示、通知模板 |
| `notice` | object | 各类审核通知的目标群 |
| `admin_openids` | list | 允许使用人工审核命令的成员 openid |
| `record_limit` | int | 每个群保留的最近成员记录数 |

### 分群规则

- `group_ids` 填 `default` 表示默认规则。
- 也可以填 `group_openid`；如果机器人有群信息接口权限，也可以填群名称。
- 某个群配置了专属规则后，该群不再继承 `default`。
- 给某个群添加一条关闭状态的规则，可以让该群不自动通过 / 不自动拒绝。

### 关键词匹配

匹配范围包括：

- `verify_info.verify_message`（验证消息）
- `verify_info.review_qa_list` 中的问答答案

拒绝规则先于同意规则检查，命中拒绝词直接拒绝。

### 通知目标

`notice` 下每个列表填 `group_openid`，特殊值 `origin` 表示申请人所在的群。留空表示不发送通知。

## 命令

| 命令 | 说明 |
| --- | --- |
| `/入群统计` / `/加群统计` | 查看本群入群统计 |
| `/入群审核 列表` | 查看待处理入群申请 |
| `/入群审核 通过 <序号或member_openid>` | 手动通过申请 |
| `/入群审核 拒绝 <序号或member_openid> [理由]` | 手动拒绝申请 |
| `/入群审核 我是谁` | 查看自己的 openid 和当前群 openid |

人工审核命令需要发送者满足以下任一条件：

- 是 AstrBot 全局管理员
- openid 已填入插件配置的 `admin_openids`

QQ 官方群聊中，通常需要 @机器人 才能触发命令（除非平台开启了全量群消息）。

## 占位符

消息模板支持以下占位符：

| 占位符 | 含义 |
| --- | --- |
| `%group_id%` | 群 `group_openid` |
| `%group_name%` | 群名称，获取不到时回退为 `group_openid` |
| `%user_id%` | 申请人 `member_openid` |
| `%user_name%` | 申请人昵称，未获取到时回退为 openid |
| `%comment%` | 验证消息 / 问答答案 |
| `%category%` | 命中的来源分类 |
| `%key%` | 命中的关键词 |
| `%apply_source%` | 申请来源：主动申请 / 被邀请 |
| `%invited_by%` | 邀请人 openid |
| `%risk_tips%` | QQ 返回的风控提示 |
| `%result%` | 审核结果，例如“自动通过”“待人工处理” |
| `%reason%` | 拒绝理由 |

## 数据存储

入群记录保存在：

```text
AstrBot/data/plugin_data/astrbot_plugin_qq_joinmanager/join_records.json
```

每个群保存累计入群数、来源分类计数、每日计数和最近成员记录；成员记录超过 `record_limit` 后自动清理最旧记录。

## 工作原理

AstrBot 当前 `qq_official` 适配器基于 `qq-botpy` 1.2.1，只订阅了消息类 intent，没有入群申请事件的解析器。插件在平台适配器实例化之前做了最小补丁：

1. 构造 `botpy.Client` 时追加 `GROUP_MEMBER_EVENT`（`1 << 24`）intent
2. 为 `ConnectionState` 增加 `GROUP_JOIN_REQUEST`、`GROUP_MEMBER_ADD`、`GROUP_MEMBER_REMOVE` 解析器
3. 拦截 `ws_dispatch`，把上述事件转交给插件回调

审批通过/拒绝、待处理列表、群信息分别调用：

- `POST /v2/groups/{group_openid}/approval_join_request/{member_openid}`
- `GET /v2/groups/{group_openid}/join_request_list`
- `GET /v2/groups/{group_openid}/info`

如果未来 AstrBot / qq-botpy 原生支持这些事件，插件会让位给原生 handler。

## 已知限制

- QQ 官方 API 不提供 QQ 号、群号、QQ 等级；群和用户分别使用 `group_openid` 和 `member_openid`，因此原版插件的“QQ 等级限制”无法移植。
- `GET /v2/groups/{openid}/info` 和群成员信息接口目前是白名单能力，普通机器人可能拿不到群名，模板中的 `%group_name%` 会回退为 `group_openid`。因此分群配置建议直接使用 `group_openid`。
- `/入群统计` 目前为文本统计，未实现原版的 matplotlib 饼图。
- 欢迎语和审核通知属于主动群消息，受 QQ 主动消息配额影响；发送失败只记录日志，不影响审批结果。
- QQ 官方接口或权限策略可能调整，如遇 11253 等错误，请检查机器人是否具有对应接口权限。

## 灵感来源

本插件的功能设计参考了：

- [WhiteCloudOL/astrbot_plugin_joinmanager](https://github.com/WhiteCloudOL/astrbot_plugin_joinmanager)
- [qiqi55488/astrbot_plugin_appreview](https://github.com/qiqi55488/astrbot_plugin_appreview)

插件针对 QQ 官方机器人 API 重新实现，未复用上述项目的代码。

## 许可

本项目使用 [GNU Affero General Public License v3.0](LICENSE)。
