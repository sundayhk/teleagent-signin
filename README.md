# TeleAgent 每日积分签到系统（WebUI 版）

TeleAgent 星辰超级智能体（https://ta.teleai.com.cn/）多账号每日积分自动签到，带密码 WebUI、签到日志、Telegram 推送。单文件 Python、零第三方依赖，Docker 一键部署。

> 签到接口逆向自 ta-web 前端 bundle：`v2/checkDayTask` 查状态 → `checkTask?type=daily_login` 领取 → 复核 `claimed_today` 确认到账（领取接口静默幂等，无证据不报成功）。

## 功能

- **多账号**：每个账号 = 备注 + X-Token，可任意增删
- **WebUI 密码访问**：账号管理、手动签到、签到日志（实时刷新，持久化落盘）
- **暂停**：单个账号暂停/恢复，或一键暂停/恢复全部
- **替换 Token**：Token 失效后在 WebUI 点「换Token」直接替换，无需重启
- **Telegram 推送**（按时段静默，防夜间刷屏）：
  - 0:00–1:00 当天只推第一轮
  - 1:00–8:00 静默，只写日志（结果缓存，8 点后补报）
  - 8:00–24:00 正常推送
- **定时**：每天 0:05 自动签全部启用账号；服务当天中途重启会自动补签

## 部署（docker-compose）

```bash
git clone <你的仓库地址> teleagent-signin
cd teleagent-signin
cp .env.example .env
vim .env                 # 修改 WEBUI_PASSWORD（务必！），确认 TG token/chat_id
docker compose up -d --build
```

访问 `http://服务器IP:8002` 登录即可。

## 配置（.env）

| 变量 | 说明 | 默认 |
|---|---|---|
| `WEBUI_PASSWORD` | WebUI 登录密码 | `admin123`（**务必修改**） |
| `TELE_PORT` | 容器内监听端口 | `8002` |
| `TELE_SIGN_HOUR` | 每日自动签到小时（0-23），固定在该小时第 5 分触发 | `0` |
| `TELE_TOKEN` | Telegram Bot Token | 已内置 |
| `TELE_CHAT_ID` | Telegram Chat ID | 已内置 |
| `TELE_PROXY` | TG 推送代理（可直连 TG 则不填） | 空 |
| `TELEAGENT_PROXY` | TeleAgent 站点访问代理 | 空 |
| `TELEAGENT_APP_VERSION` | 客户端版本号伪装 | `2.7.1` |

`.env` 已被 `.gitignore` / `.dockerignore` 排除，不会进仓库和镜像。

## 获取 Token

**关键：必须先访问签到活动页面 `https://agent.teleai.com.cn/teleagent`**，`x-token` 只在访问这个页面后才会下发。在 `ta.teleai.com.cn` 登录页或站点首页的 Cookies 里是看不到 `x-token` 的，能看到的是 `operToken` 等其他 cookie，那些不是本脚本要的凭证。

步骤：

1. 浏览器登录后，打开 https://agent.teleai.com.cn/teleagent
2. F12 打开开发者工具 → **Application** → **Cookies** → `https://agent.teleai.com.cn` → 找到名为 **`x-token`** 的行，复制其 Value
   - 正常的 x-token 是 JWT 格式：`xxx.yyy.zzz` 三段、两个点、几百个字符
   - 如果列表里没有 `x-token`，先刷新一下该页面（登录后会重新下发）
3. WebUI「添加账号」→ 填备注 → 粘贴 Token → 保存
4. 点该账号行的「签到」测试一次

> 也支持直接粘贴整串 Cookie（脚本自动抽取其中的 `x-token=`），但同样必须在活动页复制，且整串里要确实包含 `x-token` 这一项。

常见坑：从别的页面复制了一串 cookie 粘进去，结果存进去的是 `UM_distinctid / operToken / CNZZDATA...` 那种，签到会报 `INVALID（JWT 解析失败）`——这就是没拿到 x-token，回活动页按上面步骤重抓即可。

Token 是登录态凭证，会过期。过期后账号状态显示 `INVALID`，抓新的 Token 点「换Token」替换即可。

## WebUI 操作

- **▶ 立即签到全部**：手动跑一轮全部启用账号
- **⏸ / ▶ 暂停、恢复全部**：一键切换，暂停的账号不参与定时与手动签到
- **账号行**：暂停/恢复、换Token、单账号签到、删除
- **✈ TG 测试**：发一条测试消息验证推送链路
- **签到日志**：最近 200 条，账号、结果码、积分到账详情；持久化在 `data/logs.json`

状态码含义：`OK` 成功/已领 · `INVALID` Token 失效 · `ERROR` 接口异常 · `NETWORK` 网络不可达 · `INACTIVE` 今日无日签任务 · `UNVERIFIED` 已受理未复核到 · `EXC` 脚本异常

## 数据与更新

- 账号与日志持久化在 `./data/`（compose 已挂载卷），重建容器不丢失
- 更新脚本：

```bash
git pull
docker compose build --no-cache && docker compose up -d
```

## 命令行模式（可选，不用 WebUI 时）

```bash
python3 teleagent_webui.py run      # 只跑一轮签到，可挂 crontab
python3 teleagent_webui.py status   # 命令行看账号状态
```

## 风险说明

签到接口为个人逆向所得，官方改版（接口路径、字段、签名校验加强）可能导致失效；请勿高频手动触发，避免账号风控。
