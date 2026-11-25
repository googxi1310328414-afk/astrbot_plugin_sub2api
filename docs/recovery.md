# 账务核对与人工恢复手册

本文写给 AstrBot 管理员与插件维护者:当用户看到"账务待核对(流水 …)"、或 `recovery_status.json` 长期有挂起流水时,按本文核对证据并处理。

> AI 代理只能**读**本文、帮人类整理证据;第 5 节的 `/核账` 裁决与第 6 节的人工终局必须由人类管理员亲自执行。

## 1. 先确认自动核账在工作

绝大多数挂起流水不需要人工处理:后台任务每轮最多核对 5 笔到期流水,按证据自动补记、补发或退款。先看插件目录下的 `recovery_status.json`(只含脱敏摘要,不含 QQ、邮箱、金额):

| 字段 | 含义 |
| --- | --- |
| `checked_at` | 最近一轮核对时间(Unix 秒) |
| `worker_enabled` | 自动核账是否开启(`recovery_enabled`) |
| `storage_failed` | `state.json` 写入失败;此时所有余额指令暂停,存储恢复后下一轮会自动补写并解除 |
| `pending_count` | 挂起流水数 |
| `recovered_count` | 仍留在 `state.json` 中、由自动核账终局的流水数(归档后不再计入) |
| `history_near_limit` | 本次运行中余额历史接近上限(≥1800 条)的账号数,见第 4 节 |
| `transactions[]` | 每笔挂起流水:`id`、`kind`、`phase`、`age_seconds`、`result`、`last_error`、`next_check_at` |

同一笔流水的重试间隔按 `recovery_interval_seconds` 指数退避(默认 30→60→120→…),最长 15 分钟。`checked_at` 长时间不更新时,先按 AGENTS.md 的"重载与验证"检查插件是否正常加载。

## 2. 流水与证据的对应关系

流水保存在 `state.json` 的 `transactions.<流水号>` 中,流水号是 32 位十六进制。关键字段:

- `kind`:`checkin`(签到,1 步)、`rob_success` / `rob_failure`(打劫,2 步)
- `qq`:发起人 QQ;打劫另有 `target_qq`(被打劫者)
- `amount`:金额(3 位小数);`steps`:每步的 `uid` 与 `operation`(`add`/`subtract`)
- `step_outcomes`:每步结果,`not_sent` / `inflight` / `applied` / `not_applied` / `unknown`
- `recovery`:自动核账记录(`result`、`last_error`、`decision`、`refund` 等)

| kind | 第 1 步 | 第 2 步 |
| --- | --- | --- |
| `checkin` | 给签到人 `add` 奖励 | — |
| `rob_success` | 从被打劫者 `subtract` | 给打劫者 `add` |
| `rob_failure` | 从打劫者 `subtract` 0.500 | 给被打劫者 `add` 0.500 |

每步余额请求都带固定备注(notes),这是在 sub2api 中查找证据的唯一依据:

```text
QQ互动 tx=<流水号> step=<1|2> kind=<kind> qq=<发起人QQ>     # 原始步骤
QQ互动 tx=<流水号> recovery=refund step=1                  # 自动退款(仅打劫)
```

证据来源是该步 `uid` 的 sub2api 余额变动记录:管理面板中该用户的余额记录,或管理 API `GET <base_url>/admin/users/<uid>/balance-history?type=admin_balance&page=<n>&page_size=100`。凭据只走面板或插件配置,不要写进命令行。

自动核账只采纳**恰好一条**同时满足下列条件的记录;人工核对请用同一口径:

- `notes` 与上面的备注**逐字一致**
- `type` 为 `admin_balance`、`status` 为 `used`、`used_by` 等于该步 `uid`
- `value` 为 `+amount`(add)或 `-amount`(subtract)
- 记录时间(`used_at`,缺失时用 `created_at`)不早于流水 `created_at` 前 5 秒(退款以 `recovery.refund.created_at` 为准)

## 3. 自动核账会自行处理的情形

| 情形 | 自动处理结果 |
| --- | --- |
| 意图已记录、请求从未发出 | 流水记为 `failed`(`not_sent`),打劫冷却一并撤销 |
| 第 1 步被服务端明确拒绝 | 流水记为 `failed`(`not_applied`),可安全重试 |
| 结果未知,但查到唯一匹配记录 | 补记为已执行;全部步骤确认后流水 `completed`,签到标记按流水日期补记 |
| 打劫第 1 步已执行、第 2 步确认未发出 | 用**原备注、原幂等键**补发第 2 步,完成整笔 |
| 打劫第 1 步已执行、第 2 步被明确拒绝 | 按原金额退回第 1 步扣款方,流水 `resolved`(`refunded`),退款证据存于 `recovery.refund` |

结果未知且**查无记录**时,自动核账只会等待(`waiting_evidence`),永远不会重发,也不会自行判定为未执行:没有记录不能证明请求没有执行。

## 4. 需要人工介入的情形

| `recovery_status.json` 表现 | 含义 | 处理 |
| --- | --- | --- |
| `result=waiting_evidence`、`last_error=null`,持续多轮 | 服务端查无该步记录 | 签到流水按第 5 节裁决;打劫流水按第 6 节 |
| `last_error=history_read_failed`,持续多轮 | 余额历史读取失败或证据不一致:sub2api 不可用、分页异常、同一备注有多条记录或字段不符,或该账号记录超过 2000 条(自动核账只读 20 页 × 100 条) | 先排除服务与网络问题;仍失败时按第 2 节口径人工翻查全部记录 |
| `history_near_limit` > 0 | 有账号的余额记录接近 2000 条,超过后该账号的流水无法再自动核对 | 提前告知维护者;出现挂起时按人工口径核对 |
| `result=conflicting_evidence` | 后一步查到记录、前一步却未确认,证据自相矛盾 | 不要裁决;保留现场并联系维护者 |
| `result=invalid_transaction` | 流水结构不合法(常见于手工改动过 `state.json`),自动核账不会处理它 | 对照改动前的备份查明差异并联系维护者,不要自行"修好" |
| `storage_failed=true` | `state.json` 无法写入 | 修复磁盘空间或权限,不要删除状态文件 |

## 5. 签到流水:`/核账` 裁决

仅 AstrBot 管理员可用,只受理**结果未知的单步签到流水**(状态为 `pending`,步骤结果为 `unknown`/`inflight`)。提示"等待自动核账补齐的旧格式"时,等自动核账跑过一轮再试。

| # | 核对结论 | 裁决 | 指令 |
| --- | --- | --- | --- |
| 1 | 按第 2 节口径**翻查全部记录**仍无该备注,且距流水创建已远超 `write_timeout_seconds`、自动核账已多轮 `waiting_evidence`,并至少有一项旁证表明请求未执行(如 sub2api/反向代理日志里该时段没有这个 uid 的余额写入成功记录) | 确认未执行 | `/核账 <流水号> not_applied` |
| 2 | 恰好有一条满足第 2 节全部条件的记录(自动核账因记录超过 2000 条等原因未能采纳) | 确认已到账 | `/核账 <流水号> applied` |
| 3 | 同一备注有多条记录,或金额、账号不符 | 证据冲突,不能直接裁决 | 先查明原因,并在 sub2api 面板手动调整到"恰好执行一次"(调整备注不要复用原 notes),再按第 2 行裁决 |
| 4 | 无法判断 | 不裁决 | 保持挂起即可:只暂停相关账号的余额玩法,不影响其他用户 |

裁决效果:

- `not_applied`:流水标记 `resolved`,不补签到标记;流水若是当天的,用户当天可重新 `/签到`,往日流水不会自动补发奖励。证据不足时误判会让用户重复领奖,宁可保持挂起。
- `applied`:流水标记 `resolved`,按流水日期补记签到标记(取较新日期,不回退)。

`resolution_note` 会自动记录裁决、处理人 QQ 与时间;证据记录 id 等细节请另行留存在自己的运维记录里。

## 6. 打劫流水:人工终局(最后手段)

打劫流水含两步资金操作,`/核账` 不受理。只有自动核账长期无法结论时才做,由人类管理员执行:

1. **收集证据**:按第 2 节口径分别核对第 1 步(`steps[0].uid`,`-amount`)、第 2 步(`steps[1].uid`,`+amount`);`recovery.refund` 存在时再核对退款记录(`steps[0].uid`,`+amount`)。
2. **定结论**:整笔只能落到两种结果之一:
   - 整笔完成:第 1、2 步各恰好执行一次,且没有退款;
   - 整笔撤销:净效果为零(都未执行,或第 1 步已执行且已等额退回)。
3. **修正余额**:在 sub2api 面板手动调整双方余额,使实际结果符合上面的结论。调整备注写成 `人工核账 tx=<流水号> …`,**不要**复用插件的原备注,以免被当作原请求的证据。
4. **停机并备份**:停止 AstrBot(保证没有并发写入),全量备份插件目录(含 `state.json` 与 `ledger_archive.json`)。
5. **只改这一笔流水**:把 `status` 改为 `resolved`,并写入 `resolution_note`(结论、处理人、时间、证据记录 id、手动调整内容)。不要删除流水,不要改动其他字段。可在插件目录执行(先替换两处占位符):

```bash
cd /path/to/astrbot/data/plugins/astrbot_plugin_sub2api
python3 - <<'EOF'
import json, os
TXID = "<流水号>"
NOTE = "人工核账:<结论>;处理人 <姓名或QQ>;<日期时间>;证据 <记录 id>;调整 <手动调整内容>"
with open("state.json", encoding="utf-8") as f:
    state = json.load(f)
tx = state["transactions"][TXID]
assert tx["status"] == "pending", "只处理 pending 流水"
tx["status"] = "resolved"
tx["resolution_note"] = NOTE
with open("state.json.tmp", "w", encoding="utf-8") as f:
    json.dump(state, f, ensure_ascii=False, indent=2)
os.replace("state.json.tmp", "state.json")
EOF
```

6. **启动并验证**:启动 AstrBot,确认插件正常加载(状态文件结构不合法时插件会拒绝加载,此时用第 4 步的备份还原后重做),`recovery_status.json` 中该流水消失,双方 `/查询` 不再提示待核对。

## 7. 禁止事项

- 不要用原备注手工补发或"重放"原请求;结果未知只能查证据,不能重发。
- 不要删除或重建 `state.json`、`ledger_archive.json`;损坏时从备份还原。
- 不要删除挂起流水来"解锁"账号。
- 不要同时运行多个实例共用状态文件。
- 不要在群聊里贴余额明细或流水全文(流水号本身可以公开)。
