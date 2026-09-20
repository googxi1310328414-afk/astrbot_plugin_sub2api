# astrbot_plugin_sub2api

[AstrBot](https://github.com/AstrBotDevs/AstrBot) 插件:把 QQ 群互动与 [sub2api](https://github.com/Wei-Shaw/sub2api) 网关打通——余额玩法(绑定/签到/打劫/查询)、分组状态监控卡片,以及一个供 AI 客服自行调用的**脱敏**分组成功率查询工具。

所有指令均为机械化处理,**不经过大模型**、不消耗 token;LLM 工具只返回白名单数据(分组名 + 成功率百分比),不暴露账号数量、请求量、吞吐等集群规模信息。

## 功能一览

| 指令 / 能力 | 说明 |
| --- | --- |
| `/绑定 邮箱` | 校验 sub2api 账号状态后与 QQ 号绑定(其余玩法的前提) |
| `/签到` | 每日一次(东八区),随机获得 0.100–0.500 余额;重复签到提示已签过 |
| `/打劫 @某人` | 70% 成功转移随机 0.100–0.500(不足按可用余额向下取整);30% 失败赔偿对方 0.500;600 秒冷却并显示剩余时间;双方余额真实变动 |
| `/查询` | 查看绑定账号余额(额度明细自动撤回,见下) |
| `/状态` | 渲染 sub2api **公开分组**状态卡片并发图(仅公开启用分组,无集群规模数据) |
| LLM 工具 `query_group_success_rate` | 供机器人人格在对话中自行调用,返回各启用分组近一小时成功率(白名单脱敏) |

## 安全与账务设计

这个插件按"真钱"标准处理余额变动:

- **意图先行**:任何余额写入前先把完整意图(事务流水)持久化到 `state.json`,崩溃后可核对
- **结果未知不重发**:网络错误、超时、5xx 等"可能已执行"的请求只查询证据(balance-history),绝不盲目重发
- **幂等键**:每步余额操作携带 `Idempotency-Key`,服务端可去重
- **自动核账**:后台任务按已确认的执行证据补记完成、补齐未发送步骤或退回明确失败;未知请求保持挂起并暂停相关账号
- **负余额拦截**:签到/打劫前预检余额,负余额或不足以承担赔款时直接拒绝,不产生流水
- **额度隐私**:签到奖励、查询余额的明细消息单独发送后按 `quota_recall_seconds`(默认 5 秒)自动撤回;打劫结果不撤回
- **状态文件保护**:`state.json` 损坏时拒绝加载而不是重建,防止账务凭空消失

## 环境要求

- AstrBot v4.28+(经 NapCat/aiocqhttp 接入 QQ;其他平台未测试)
- 一套可访问管理 API 的 [sub2api](https://github.com/Wei-Shaw/sub2api) 实例
- AstrBot 容器/进程内可用 Noto Sans CJK 字体(`/状态` 卡片渲染;官方镜像自带)
- Python 3.10+(插件本体依赖 aiohttp、Pillow,均为 AstrBot 自带)

## 安装

```bash
# 进入 AstrBot 数据目录的插件文件夹(容器部署时对应宿主机挂载的 data/plugins)
cd /path/to/AstrBot/data/plugins
git clone https://github.com/googxi1310328414-afk/astrbot_plugin_sub2api.git
# 仓库内插件源码位于子目录 astrbot_plugin_sub2api/,克隆后即为插件目录
```

重启 AstrBot(或面板里重载插件),日志出现 `Plugin astrbot_plugin_sub2api` 与 `Added llm tool: query_group_success_rate` 即加载成功。

> 给 AI 代理的部署手册见 [AGENTS.md](./AGENTS.md),含逐步命令与验证清单。

## 配置

插件配置在 AstrBot 面板「插件配置」中填写(或 `data/config/astrbot_plugin_sub2api_config.json`):

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `base_url` | `http://127.0.0.1:8080/api/v1` | sub2api 管理 API 地址,**插件进程可达**,含 `/api/v1` 前缀 |
| `admin_email` | (空) | 专用管理员邮箱,**必填** |
| `admin_password` | (空) | 专用管理员密码,**必填**,勿在群聊发送 |
| `robbery_cooldown` | `600` | 打劫冷却秒数(非负整数) |
| `allow_bind_admin` | `false` | 是否允许绑定管理员角色的 sub2api 账号 |
| `quota_recall_seconds` | `5` | 额度明细自动撤回秒数(1–120) |
| `recovery_enabled` | `true` | 后台自动核账开关 |
| `recovery_interval_seconds` | `30` | 自动核账初始间隔(10–900,失败退避最长 15 分钟) |

### base_url 网络拓扑示例

| 部署形态 | base_url |
| --- | --- |
| sub2api 与 AstrBot 同宿主机,AstrBot 非容器 | `http://127.0.0.1:8080/api/v1` |
| sub2api 容器与 AstrBot 容器在同一 Docker 网络 | `http://sub2api:8080/api/v1`(容器名) |
| AstrBot 容器经宿主机网关访问宿主机端口 | `http://172.x.0.1:8080/api/v1`(AstrBot 所在网桥的网关 IP) |

### 专用管理员账号(强烈建议)

1. 在 sub2api 面板新建一个**专用管理员账号**(而不是用主管理员),例如 `bot@example.com`
2. 首次用该账号调用管理 API 时,sub2api 会要求确认《部署与运营合规承诺》(HTTP 423);在面板用该账号登录并完成确认即可
3. 专用账号随时可删除/改密,不影响主管理员;泄露面最小

## AI 工具:query_group_success_rate

插件注册的 LLM 工具,机器人人格(neuro-sama 之类)在用户询问"哪个分组不稳定 / 成功率如何"时可自行调用:

- 数据:启用分组清单(`groups/all`)+ 逐组近 1 小时快照(`snapshot-v2?group_id=N`)
- 输出**白名单**:分组名、成功率百分比(1 位小数)、"样本少"(请求 <20)、"近一小时无请求"标记
- 绝不输出:账号数量、请求量、吞吐、token、金额等可推断集群规模的数据
- 带 60 秒缓存;参数 `group_name` 可按关键词过滤

`/状态` 卡片遵循同一脱敏口径:仅展示**公开**(非 `is_exclusive`)且**启用**的分组;卡片上只有分组数、分组级状态计数、渠道可用**百分比**、成功率、健康分(0–100)与分钟级热力条。

## 测试

```bash
python -m pip install -r requirements-test.txt
python -m unittest discover -s tests -v
# 134 个用例:指令行为 / 账务核账恢复 / 额度撤回 / HTTP 模拟
```

测试全部使用假事件、假账户与 `127.0.0.1` 临时端口,不访问任何真实服务。

## 常见问题

- **群里发 `/签到` 没反应?** AstrBot 的唤醒前缀必须包含 `/`(配置 `wake_prefix` 加入 `/`),否则消息在插件匹配前就被丢弃。
- **余额操作提示"账务结果待核对"?** 出现了结果未知的请求,插件已暂停相关账号并会在后台按证据自动核账;持续未恢复时联系管理员按 `state.json` 流水与 sub2api 余额历史人工核对。
- **`/状态` 报字体缺失?** 容器需有 Noto Sans CJK;官方 `soulter/astrbot` 镜像自带。
- **能同时跑多个实例吗?** 不能。多实例共用同一份 `state.json` 会破坏账务一致性。

## 目录结构

```
├── main.py               # 插件入口:指令、账务事务、LLM 工具、卡片渲染
├── recall.py             # 额度明细自动撤回
├── recovery.py           # 后台自动核账恢复
├── metadata.yaml         # 插件元数据
├── _conf_schema.json     # 面板配置模式
├── tests/                # 134 个隔离测试用例
├── AGENTS.md             # AI 代理部署/运维手册
└── requirements-test.txt
```

仓库根目录即插件目录,`git clone` 到 `data/plugins/` 下即可被 AstrBot 识别。

## 许可证

[MIT](./LICENSE)
