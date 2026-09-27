# 扫码签到修复与验证

新版把相机扫码、照片识别、回传、学校结果确认分为可恢复的阶段。照片只在浏览器中解码，服务器接收二维码文字。

## 页面与相机

- 解码器、页面逻辑与样式全部随 HTML 下发，不再依赖 CDN 或 ngrok 上第二次脚本请求。动态页 `no-store`，API 请求携带 ngrok 官方支持的放行头，并验证响应为 JSON。
- 中央取景十字、链接倒计时、最终学校确认、独立相册与系统拍照入口。
- 变焦调用 `MediaStreamTrack.applyConstraints({zoom})`，回读 `getSettings().zoom`；失败或被浏览器忽略会显示实际值，不使用 CSS 放大冒充相机变焦。
- 使用授权后 `enumerateDevices()` 的真实 video input，通过 `deviceId: {exact: ...}` 切换镜头；切换前释放旧相机。物理长焦是否开放、相机变焦是否为光学，由设备/浏览器决定，网页不能强制解锁隐藏镜头。
- 照片保持宽高比，独立于相机 zoom，进行整图和分块多尺度识别；包含反色二维码处理、文件/像素预算、异常恢复。解码优先在 Worker 进行。

## 服务端

会话状态：`waiting → submitting → success / failed / uncertain`；`uncertain` 只能核对结果，不能直接再次签到。链接到期后停止接受新提交，结果保留十分钟。每账号、每签到去重；最多 64 个保留会话、4 个后台提交线程，每会话最多 8 次有效提交尝试。统一清理任务替代每会话一个计时线程。

- 二维码格式、字段类型、长度、学校域名及当前 rollcallId 校验通过后才提交。错误二维码不消费会话。
- `request_id` 与内容摘要组成幂等记录；响应丢失可重试同一请求，或查询 `/status/<sid>`。确认结果才显示签到成功。
- 学校 PUT/GET 配置 `(5, 12)` 秒连接/读取超时且不跟随重定向。通过学校查询中对应记录的 `on_call_fine` 确认成功，200 HTML 不被当成成功。未能核对时明确显示待确认，`/verify/<sid>` 只查询、不重复 PUT。
- 扫码等待从轮询线程移出，工作线程使用独立 requests.Session。通知失败可限次重试；刷新扫码会话不会重复执行数字/雷达签到。
- 移除公开 `/diag*` 路由。正式页遥测只接收白名单事件及有限数值，单会话 30 条；不保存二维码原文、照片、文件名、错误全文或原始会话标识。限制请求体为 20 KiB，检查 POST Origin。应用访问日志隐藏会话路径。

可设置 `XMU_ROLLCALL_QR_PUBLIC_BASE_URL=https://scan.example.com`，或 `qr.public_base_url`，使用自有 HTTPS 反向代理；仅接受 origin，不接受路径、查询串或凭据。默认监听 `127.0.0.1:5001`。未设置公开 origin 时保留 ngrok 模式。启动监控时预热扫码服务。

## 本地验证与 Mock

Python 3.11、Node 20+：

```sh
python3 -m venv .venv
.venv/bin/pip install -e ./xmu-rollcall-cli pytest
.venv/bin/python -m pytest -q xmu-rollcall-cli/tests
npm ci --prefix scripts
npx --prefix scripts playwright install chromium
PYTHON="$PWD/.venv/bin/python" npm --prefix scripts run preview
.venv/bin/python scripts/scanner_test_server.py
```

另一终端执行 `npm --prefix scripts test`。测试服务仅监听 `127.0.0.1:8765`，使用虚构会话与学校响应，无真实账号和网络签到。独立 Mock 文件在 `artifacts/scanner-preview.html`，可直接打开；`http://127.0.0.1:8765/` 也提供该文件。Mock 可操作相机变焦、镜头、成功、断网、错误码、待确认及过期场景。真实扫码页不能通过 URL 参数开启 Mock。

若系统没有 Chromium 运行库/中文字体，需要另行准备测试依赖。生成文件与测试依赖不进入 Git。

手机真机测试可单独运行 `scripts/mobile_scanner_preview.py --origin https://your-preview-host`，并将该 HTTPS 入口代理至本机 8768 端口。先执行上述浏览器测试生成 `artifacts/synthetic-qr.png`；入口 `/` 创建十分钟测试会话，`/sample.png` 提供测试二维码。页面实际调用相机和照片解码，但只接受测试签到编号，结果由模拟回调返回，不读取学校账号。此脚本不得接入正式签到服务。

## 本轮结果与复查

- 50 项 Python 测试通过。
- 11 组 Chromium 浏览器场景通过：Mock 全流程；相机接口变焦/切镜头；扫错码后重试；偏离中心的 1800×3000 竖图；拒绝相机权限后选图；相机忽略 zoom；无 zoom 能力；服务端收到后丢响应；服务端收到前断网；损坏照片后恢复；学校拒绝后重扫及刷新保留结果；过期与窄屏布局（部分场景组合在同一组）。
- 第二轮 review 修复了：学校失败状态轮询打断用户再次扫描、请求排队后已过期仍可能提交、通知失败不再重发、复用页面时 Worker 未重建、以及刷新 QR 会话可能影响数字/雷达签到调度的问题。
- 自动化测试使用模拟相机和学校响应；另提供 HTTPS 真机入口供手机验收，预览测试不发送学校签到请求。不同浏览器可开放的物理镜头及变焦能力仍以实际设备为准。
- 学校记录不存在、接口不可用或状态尚未同步时显示“待确认”，不猜测成功。生产部署重启会使旧内存会话失效，应避开进行中的签到窗口。

参考：[ngrok 警告页机制](https://ngrok.com/docs/pricing-limits/free-plan-limits#removing-the-interstitial-page)、[相机能力与设置](https://developer.mozilla.org/en-US/docs/Web/API/MediaStreamTrack/getCapabilities)。jsQR 随附 Apache-2.0 许可证，使用已部署的解码器文件。
