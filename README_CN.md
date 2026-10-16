

<div align="center">

  <img src="https://socialify.git.ci/KrsMt-0113/XMU-Rollcall-Bot/image?font=JetBrains+Mono&forks=1&language=1&name=1&owner=1&pattern=Plus&stargazers=1&theme=Light" />

</div>

<div align="center">

  <img src="https://img.shields.io/github/directory-file-count/KrsMt-0113/XMU-Rollcall-Bot" />
  <img src="https://img.shields.io/github/languages/code-size/KrsMt-0113/XMU-Rollcall-Bot" />

</div>

[English](README.md)

[使用手册](https://krsmt.notion.site/cli-doc)

> [!WARNING]
> 2026-05-12 之前的登录方式已被弃用。使用 `pip install -U xmulogin` 更新相关组件到最新版本。
>
> 2026-05-15 ~登录组件出现新的问题，等待进一步排查修复。~
>
> 2026-05-15 降级`xmulogin`组件以恢复正常: `pip install xmulogin==1.0.0`

## 快速开始

```bash
pip install xmu-rollcall-cli
```

## 使用方法

```bash
xmu add-account  # 快速添加一个账号。
xmu config       # 管理账号。使用统一身份认证账号密码。
xmu start        # 启动当前账号监控。
xmu start-all    # 同时监控所有已配置账号。
```

## 当前能力

- 使用 XMU 统一身份认证登录，并缓存 session cookie。
- 每 1 秒轮询一次 `https://lnt.xmu.edu.cn/api/radar/rollcalls`。
- 支持多账号配置、当前账号切换和多账号同时监控。
- 支持数字码签到、雷达签到。
- 集成版支持二维码签到：检测到二维码签到后，会通过当前通知渠道推送一次性扫码链接。
- 所有成功签到方式都会发送成功反馈通知。

## 签到类型判断逻辑

监控进程发现新的 rollcall 后，按以下顺序处理：

1. `status == on_call_fine`：认为已经签到，不重复提交。
2. `is_expired == true`：已过期，不提交。
3. `status != absent`：不是待签到状态，不提交。
4. `status == absent && is_radar == true`：人数门槛通过后执行雷达签到。
5. `status == absent && is_number == true`：人数门槛通过后执行数字码签到。
6. `status == absent && !is_radar && !is_number`：按二维码签到处理。

数字码、雷达、二维码任一方式签到成功后，都会通过当前通知渠道发送成功反馈。

## 数字与雷达签到门槛

发现待签到场次后，数字签到使用 `/api/timetable_rollcalls?course_ids=<course_id>&rollcall_date=<yyyy-mm-dd>`
读取签到码和学生签到状态，按课程 ID、UTC 日期和签到 ID 精确匹配，不使用其他场次的签到码。
雷达签到使用 `/api/rollcall/<rollcall_id>` 读取该场次的学生签到状态，并校验签到 ID、课程 ID、
雷达类型及时间信息；不依赖可能缺少雷达场次的课表结果。两种方式共用下面的人数门槛。

数字和雷达签到均须同时满足以下条件：

- 排除本服务托管账号后，其他同学已签到比例 **至少 30%**。
- 排除本服务托管账号后，其他同学已签到人数 **至少 10 人**。

例如，其他同学共 33 人时需要 10 人已签到；共 34 人时需要 11 人；共 40 人时需要 12 人；不足 10 人的班级不会自动提交。
仅 `on_call` 和 `on_call_fine` 计入已签到人数，未识别状态不计入。
托管账号从分子、分母同时排除，包括完整配置但未通过 `--account-id` 选中的账号。
通过 `/api/profile` 的学生 ID 识别这些账号；身份无法确认时暂停提交，后续轮询继续尝试。
未启动账号需要有有效的缓存会话，否则需先登录该账号，再等待监控重试。

未达标时每轮刷新人数，即使签到发现接口内容未变化也会重查。达标即提交，没有额外固定延时。
已结束或过期的场次跳过；接口错误、缺少签到码、名单缺失、重复学生 ID 等情况均不提交。
人数和放行结果不缓存，接口异常时不会沿用上次允许提交的结果。
二维码签到保持原有扫码流程。成功通知保持原有渠道；等待门槛及失败原因输出到日志。

雷达签到优先执行原来的距离探测及两圆求交流程。原流程失败时（包括请求异常、JSON 解析失败、
距离缺失或无效、无法求交点、两个交点均被拒绝），依次尝试两个固定位置：
**纬度 24.6082、经度 118.2953**，然后是 **纬度 24.6087、经度 118.3090**。
保留提供的四位小数；固定位置的响应无需包含距离字段。任一步返回 HTTP 200 即结束本轮并判定成功。
每轮最多追加两个固定位置请求，总计最多六次；第一个固定位置请求异常仍会尝试第二个。
人数门槛、请求参数字段及超时、后续轮询重试方式保持不变。

雷达提交的 `[Radar]` JSON 日志记录签到编号、账号线程（如 `xmu-account-1`）、
请求位置（`probe_1/2`、`candidate_1/2` 或 `fixed_1/2`）、处理阶段和 HTTP 状态码；
请求未收到响应时状态码为 `null`。异常另记具体类型和固定原因，区分请求失败、
JSON 解析失败、距离字段缺失或无效、无法求交点及候选请求被拒绝。
JSON 解析异常附行列号，距离异常附字段类型；不保存异常原文、响应正文、Cookie、设备标识或坐标。
非 200 探测响应可能是正常测距结果，须结合后续 `exception` 或 `failed` 判断失败。
`fallback_start` 标记开始尝试固定位置，固定位置返回 HTTP 200 时记录 `success`，
两处均失败时记录 `all_fixed_locations_rejected`；原流程失败日志仍保留，便于追查原因。

## 多账号监控

多账号配置仍然保存在同一个本地配置文件中：

```bash
xmu add-account
xmu add-account
```

启动所有完整账号：

```bash
xmu start-all
```

只启动指定账号：

```bash
xmu start-all --account-id 1 --account-id 2
```

资源模型：

- 单个 Python 进程内运行，不为每个账号复制一个进程。
- 每个账号一个轻量线程和一个 `requests.Session`。
- 每个账号独立 cookie 缓存，文件名为 `<account_id>.json`。
- 默认每个账号 1 秒轮询一次；多个账号会错峰启动，降低瞬时请求尖峰。
- 二维码扫码服务仍然懒启动且全局复用，只在检测到二维码签到时开启。

## 二维码签到与通知

二维码签到默认关闭，需要在本机配置文件中显式开启。配置文件默认位置：

```bash
~/.xmu_rollcall/config.json
```

示例配置片段：

```json
{
  "qr": {
    "enabled": true,
    "ngrok_token": "YOUR_NGROK_TOKEN",
    "session_timeout": 180,
    "flask_port": 5001
  },
  "notification": {
    "provider": "telegram",
    "telegram_bot_token": "YOUR_TELEGRAM_BOT_TOKEN",
    "telegram_chat_id": "YOUR_TELEGRAM_CHAT_ID"
  }
}
```

通知渠道支持：

- `log`：输出到本机日志。
- `telegram`：通过 Telegram Bot API 推送。
- `feishu`：通过飞书群聊自定义机器人 Webhook 推送。
- `wechat`：通过微信通知机器人推送。

飞书群聊自定义机器人配置示例：

```json
{
  "notification": {
    "provider": "feishu",
    "feishu_webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/YOUR_WEBHOOK_ID",
    "feishu_sign_secret": "YOUR_FEISHU_SIGN_SECRET"
  }
}
```

`feishu_sign_secret` 为可选项；只有在飞书自定义机器人开启签名校验时才需要填写。

支持通过环境变量覆盖通知配置：

- `XMU_ROLLCALL_NOTIFICATION_PROVIDER`
- `XMU_ROLLCALL_TELEGRAM_BOT_TOKEN`
- `XMU_ROLLCALL_TELEGRAM_CHAT_ID`
- `XMU_ROLLCALL_FEISHU_WEBHOOK_URL`
- `XMU_ROLLCALL_FEISHU_SIGN_SECRET`
- `XMU_ROLLCALL_FEISHU_TIMEOUT`

安全建议：

- 不要把 `telegram_bot_token`、`telegram_chat_id`、`feishu_webhook_url`、`feishu_sign_secret`、`ngrok_token` 提交到 git。
- 配置完成后建议执行：

```bash
chmod 600 ~/.xmu_rollcall/config.json
```

二维码签到流程：

1. 监控到二维码签到。
2. 本机懒启动 Flask 扫码服务。
3. 使用 ngrok 暴露临时 HTTPS 链接。
4. 当前通知渠道推送课程名、发起人、一次性扫码链接和有效期。
5. 手机打开链接并扫描课堂二维码。
6. 本机复用已登录 `requests.Session` 调用 `answer_qr_rollcall` 完成提交。
7. 成功后发送通知反馈。

## macOS 开机自动运行

本仓库包含本机 `launchd` 配置脚本：

- `scripts/run-xmu-rollcall.sh`：实际运行包装脚本，默认执行 `xmu start-all`。
- `scripts/com.lifuyue.xmu-rollcall.plist`：LaunchAgent 配置。
- `scripts/install-launch-agent.sh`：安装、启用并拉起服务。

安装或重新拉起：

```bash
/Users/lifuyue/Projects/XMU-Rollcall-Bot/scripts/install-launch-agent.sh
```

查看运行状态：

```bash
launchctl print gui/$(id -u)/com.lifuyue.xmu-rollcall
pgrep -af 'xmu_rollcall.cli|run-xmu-rollcall'
```

查看日志：

```bash
tail -f ~/.xmu_rollcall/logs/launchd.out.log
tail -f ~/.xmu_rollcall/logs/launchd.err.log
```

手动重启服务：

```bash
launchctl kickstart -k gui/$(id -u)/com.lifuyue.xmu-rollcall
```

卸载当前用户 LaunchAgent：

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.lifuyue.xmu-rollcall.plist
```

## 本机开发/验证命令

安装当前工作区包和依赖：

```bash
uv pip install --python .venv/bin/python -e ./xmu-rollcall-cli
```

运行检查：

```bash
.venv/bin/python -m compileall -q xmu-rollcall-cli/xmu_rollcall
.venv/bin/python -m unittest discover -s xmu-rollcall-cli/tests -v
uv pip check --python .venv/bin/python
```

## 其他

[数字化教学平台附件下载器](https://chromewebstore.google.com/detail/imannochailfofibofphcpmlddlbbhao?utm_source=item-share-cb)
