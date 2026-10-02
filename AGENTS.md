# AGENTS.md — AI 代理部署与运维手册

本文件写给 AI 编程代理(ZCode / Claude Code / Codex / Copilot 等)。目标:在任何一台已运行 AstrBot 与 sub2api 的服务器上,安全地部署、验证、升级本插件,且**不弄坏账务状态**。

## 你要部署的东西

AstrBot 插件 `astrbot_plugin_sub2api`:QQ 群余额玩法(绑定/签到/打劫/查询)、`/状态` 公开分组监控卡片、LLM 脱敏工具 `query_group_success_rate`。

- 仓库根目录 = 本文件所在目录
- 插件本体在子目录 `astrbot_plugin_sub2api/`(AstrBot 要求插件为 `data/plugins/` 下的一个目录)
- 运行时状态:插件目录下的 `state.json`(绑定、签到、流水)、`pending_recalls.json`、`recovery_status.json`、`status/`(卡片图片)

## 硬性安全红线

1. **绝不删除或重建 `state.json`**。它损坏时插件会拒绝加载——这是设计行为。恢复手段是从备份还原,不是重建。
2. **凭据只存在于 AstrBot 插件配置**(`data/config/astrbot_plugin_sub2api_config.json` 或面板),绝不写入代码、仓库、命令行参数、日志或文档。
3. **不要在群聊/普通日志输出任何用户余额明文**;额度明细走撤回机制。
4. **单实例运行**。多实例共用一份状态文件会破坏账务一致性。
5. 升级/替换文件前**必须备份**整个插件目录(含状态文件)。
6. `/状态` 与 LLM 工具的脱敏口径(不展示渠道数量/请求量/吞吐)是产品要求,改动前必须获得人类明确确认。

## 部署 Runbook

### 0. 前置检查

```bash
docker ps --format "{{.Names}}: {{.Status}}"          # astrbot 与 napcat 在运行
docker logs --tail 20 astrbot | grep -i "started"      # AstrBot 已启动
# sub2api 管理 API 可达(从 astrbot 容器内):
docker exec astrbot sh -c "curl -s -o /dev/null -w '%{http_code}' http://<sub2api地址>/api/v1/health"
```

需要一个 sub2api 管理员账号(专用账号,不要用主管理员)。首次调用管理 API 返回 HTTP 423 时,让人类在面板完成合规确认。

### 1. 安装插件

```bash
# 宿主机上的插件目录(容器部署时即挂载的 data/plugins)
cd /path/to/astrbot/data/plugins
git clone https://github.com/googxi1310328414-afk/astrbot_plugin_sub2api.git
```

### 2. 配置

方式 A(推荐):AstrBot 面板 → 插件管理 → astrbot_plugin_sub2api → 配置,填写 `base_url`、`admin_email`、`admin_password`。

方式 B:直接写 `data/config/astrbot_plugin_sub2api_config.json`:

```json
{
  "base_url": "http://<插件进程可达的sub2api地址>:8080/api/v1",
  "admin_email": "<专用管理员邮箱>",
  "admin_password": "<专用管理员密码>",
  "robbery_cooldown": 600,
  "allow_bind_admin": false,
  "quota_recall_seconds": 5,
  "recovery_enabled": true,
  "recovery_interval_seconds": 30
}
```

`base_url` 的取值取决于网络拓扑:AstrBot 非容器且 sub2api 在同机 → `127.0.0.1`;两容器同网络 → 容器名;AstrBot 容器经网桥访问宿主机 → 该网桥网关 IP。

### 3. 重载与验证

```bash
docker restart astrbot        # 或面板里重载插件
sleep 20
docker logs --since 2m astrbot 2>&1 | grep -E "Plugin astrbot_plugin_sub2api|Added llm tool|Traceback"
docker logs --since 2m astrbot 2>&1 | grep "适配器已连接"   # NapCat 已重连
```

必须同时满足:插件版本行出现、`Added llm tool: query_group_success_rate` 出现、无 Traceback、适配器已连接。

### 4. 冒烟测试(让人类在 QQ 里执行)

1. `/绑定 <邮箱>` → 回复"绑定成功"
2. `/查询` → 回复余额且数秒后撤回明细
3. `/状态` → 发出公开分组监控卡片
4. 私聊问机器人"现在哪个分组不太稳定?" → AI 调用工具并转述(百分比口吻)

### 5. 常见故障

| 现象 | 处置 |
| --- | --- |
| `/签到` 无反应 | AstrBot `wake_prefix` 缺 `/`;加回后重启 |
| 提示"管理员登录失败" | 检查专用账号密码;首次使用需在面板完成合规确认(423) |
| 提示"账务结果待核对(流水 …)" | 正常保护机制;等后台核账,勿手动改 state.json;长期不恢复按流水人工核对 |
| `/状态` 报字体缺失 | 容器安装 Noto Sans CJK |

## 升级 Runbook

```bash
cd /path/to/astrbot/data/plugins/astrbot_plugin_sub2api
cp -a . /path/to/backup/astrbot_plugin_sub2api-$(date +%F-%H%M%S)   # 1. 全量备份(含状态文件)
git pull                                                            # 2. 拉取新版
python -m py_compile main.py recall.py recovery.py                  # 3. 语法自检
docker restart astrbot                                              # 4. 重启
# 5. 按上面"重载与验证"检查;失败则回滚:rm -rf 新目录 && cp -a 备份目录 原路径 && 重启
```

版本号位于 `astrbot_plugin_sub2api/metadata.yaml` 与 `main.py` 的 `@register(...)` 第 4 参数,两处必须一致。

## 修改代码的约定

- 金额一律 `Decimal`,千分位 `0.001`;比较用 `quantize`,禁止 float。
- 一切余额写入走 `_new_tx` → `_execute_tx` → `_complete_tx` 事务链;禁止直接调用 `client.balance_op` 绕过流水。
- 结果未知的异常(`OutcomeUnknown`)只能查询证据,不能重发;这是账务安全的根基。
- 面向用户的固定提示用 `UserError` 抛出,统一回复;不要把内部异常文本直接给用户。
- 新增配置项要同步 `_conf_schema.json` 与 `main.py` 顶部 `DEFAULT_CONFIG`。
- 改动后跑 `python -m unittest discover -s tests -v`,144 个用例全绿再谈部署。

## 测试

```bash
python -m pip install -r requirements-test.txt
python -m unittest discover -s tests -v
```

测试是隔离的:假事件、假账户、`127.0.0.1` 临时端口,不连任何真实服务,可随时运行。
