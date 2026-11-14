#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TeleAgent 星辰超级智能体（https://ta.teleai.com.cn/）多账号每日积分签到系统 · WebUI 版
零第三方依赖，标准库即可运行。签到接口逆向自 ta-web 前端 bundle。

功能：
  - 多账号签到（X-Token 方式，网页版 F12 → Application → Cookies → x-token 复制填入）
  - WebUI 密码访问：账号管理（添加 / 替换 Token / 暂停单个或全部）、签到日志、手动签到
  - Telegram 推送签到结果，按时段静默：
      0:00-1:00  只推送当天第一次结果
      1:00-8:00  静默（只写日志，结果缓存，8 点后补报）
      8:00-24:00 正常推送
  - 内置定时器：每天 0 点 5 分自动签到，服务重启当天会补签；也可 WebUI 手动触发

用法：
    python3 teleagent_webui.py            # 启动 WebUI + 定时签到，默认端口 5689
    python3 teleagent_webui.py run        # 只跑一轮签到（供 crontab 调用），不启 WebUI
    python3 teleagent_webui.py status     # 命令行看一遍账号状态

环境变量：
    WEBUI_PASSWORD      WebUI 登录密码，默认 admin123（务必修改）
    TELE_PORT           监听端口，默认 5689
    TELE_TOKEN / TELE_CHAT_ID  Telegram 推送（已内置默认值，可覆盖）
    TELE_PROXY          可选，Telegram 代理，如 http://127.0.0.1:7890
    TELEAGENT_PROXY     可选，TeleAgent 站点访问代理
    TELEAGENT_APP_VERSION  可选，默认 2.7.1
    DATA_DIR            数据目录，默认脚本同目录 ./data

⚠️ 领取接口静默幂等：今日已领时 checkTask 仍返回 {"code":200,"data":[]}，
   因此「领到没」只能靠领取前后重查 claimed_today 判定，脚本无证据不报成功。
"""
import html
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ------------------------------------------------------------------ 配置
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR") or os.path.join(BASE_DIR, "data")
DATA_FILE = os.path.join(DATA_DIR, "accounts.json")
LOG_FILE = os.path.join(DATA_DIR, "logs.json")

WEBUI_PASSWORD = os.environ.get("WEBUI_PASSWORD", "admin123")
PORT = int(os.environ.get("TELE_PORT", "5689"))
SIGN_HOUR = int(os.environ.get("TELE_SIGN_HOUR", "0"))
SIGN_MINUTE = 5  # 自动签到锚点：SIGN_HOUR:05

TG_TOKEN = os.environ.get("TELE_TOKEN", "9250076594:AAErH5rN_vzYaG5pQy_pRA_0Qfnv5I0zAuk")
TG_CHAT_ID = os.environ.get("TELE_CHAT_ID", "891353843")
TG_PROXY = os.environ.get("TELE_PROXY", "").strip()

# ---------------- TeleAgent 站点接口 ----------------
API_BASE = "https://agent.teleai.com.cn/superCowork/sapi"
PATH_STATUS = "/user/portal/companyStore/v2/checkDayTask"
PATH_STATUS_V3 = "/user/portal/companyStore/v3/checkDayTask?theme=dark"
PATH_CLAIM = "/user/portal/companyStore/checkTask?type=daily_login"
PATH_CENTER = "/user/portal/companyStore/userCenter"
TA_REFERER = "https://agent.teleai.com.cn/teleagent"
DEFAULT_APP_VERSION = "2.7.1"
TIMEOUT = 20
RETRY = 2
VERIFY_DELAYS = (0.8, 1.8, 3.0)  # 领取后复核间隔

MAX_LOGS = 1000

# ------------------------------------------------------------------ 存储
_LOCK = threading.RLock()

def _load(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

class Store:
    def __init__(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        self.data = _load(DATA_FILE, {"accounts": [], "notify": {}})
        self.logs = deque(_load(LOG_FILE, []), maxlen=MAX_LOGS)

    def save(self):
        with _LOCK:
            tmp = DATA_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, DATA_FILE)
            threading.Thread(target=self._flush_logs, daemon=True).start()

    def accounts(self):
        with _LOCK:
            return list(self.data.get("accounts", []))

    def find(self, acc_id):
        for a in self.data.get("accounts", []):
            if a["id"] == acc_id:
                return a
        return None

    def add_account(self, remark, token):
        with _LOCK:
            acc = {
                "id": secrets.token_hex(6),
                "remark": remark or "账号%d" % (len(self.data["accounts"]) + 1),
                "token": clean_token(token),
                "enabled": True,
                "last_time": "",
                "last_result": "",
                "last_code": "",
                "last_points": "",
            }
            self.data.setdefault("accounts", []).append(acc)
            self.save()
            return acc

    def update_account(self, acc_id, **kw):
        with _LOCK:
            a = self.find(acc_id)
            if not a:
                return False
            for k in ("remark", "token", "enabled"):
                if k in kw and kw[k] is not None:
                    a[k] = clean_token(kw[k]) if k == "token" else kw[k]
            self.save()
            return True

    def delete_account(self, acc_id):
        with _LOCK:
            self.data["accounts"] = [a for a in self.data["accounts"] if a["id"] != acc_id]
            self.save()

    def set_all_enabled(self, enabled):
        with _LOCK:
            for a in self.data["accounts"]:
                a["enabled"] = enabled
            self.save()

    def add_log(self, level, msg):
        with _LOCK:
            self.logs.append({"t": time.strftime("%Y-%m-%d %H:%M:%S"),
                              "level": level, "msg": msg})
            threading.Thread(target=self._flush_logs, daemon=True).start()

    def _flush_logs(self):
        with _LOCK:
            try:
                tmp = LOG_FILE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(list(self.logs), f, ensure_ascii=False)
                os.replace(tmp, LOG_FILE)
            except Exception:
                pass

    def get_notify_state(self):
        return self.data.setdefault("notify", {})

STORE = Store()

# ------------------------------------------------------------------ 工具
def clean_token(raw):
    """兼容：直接粘整串 cookie 时自动抽取 x-token；去掉 bearer 前缀。"""
    tok = (raw or "").strip().strip('"').strip("'")
    if not tok:
        return ""
    m = re.search(r"(?:^|[;\s])x-token=([^;\s]+)", tok, re.I)
    if m:
        tok = urllib.parse.unquote(m.group(1).strip())
    if tok.lower().startswith("bearer "):
        tok = tok[7:].strip()
    if tok.lower().startswith("x-token="):
        tok = tok.split("=", 1)[1].strip()
    return tok

def mask_token(tok):
    if not tok:
        return "(空)"
    if len(tok) <= 12:
        return tok[:2] + "***"
    return "%s...%s(%d)" % (tok[:6], tok[-4:], len(tok))

def scrub(text, token):
    """异常/回显里若混入 token 原文，替换成掩码，避免进日志和通知。"""
    if token and isinstance(text, str) and token in text:
        return text.replace(token, mask_token(token))
    return text

def now_s():
    return time.strftime("%Y-%m-%d %H:%M:%S")

# ------------------------------------------------------------------ TeleAgent HTTP
def ta_headers(token, ver):
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Token": token,
        "X-App-Version": ver,
        "X-OS-Type": "linux",
        "User-Agent": "TeleAgent/%s" % ver,
        "Referer": TA_REFERER,
        "Origin": "https://agent.teleai.com.cn",
    }

def ta_get(path, token, full=False):
    """GET TeleAgent 接口。返回 (http_status, body_dict)。网络异常返回 (-1, {...})。"""
    url = API_BASE + path
    ver = os.environ.get("TELEAGENT_APP_VERSION", DEFAULT_APP_VERSION).strip()
    headers = ta_headers(token, ver)
    # 实测服务端对「X-Nonce/X-Timestamp 存在但无配套签名」的请求直接 401，
    # 默认精简发送；若日后服务端改为强制，置 TELEAGENT_FULL_HEADERS=1 打开全量头。
    if full or os.environ.get("TELEAGENT_FULL_HEADERS", "").strip() in ("1", "true", "True"):
        headers.update({
            "X-Timestamp": str(int(time.time())),
            "X-Nonce": str(uuid.uuid4()),
            "X-SuperAgent-Device-Id": "",
            "X-Channel-Id": "",
            "X-Runtime-Env": "",
        })
    req = urllib.request.Request(url, headers=headers, method="GET")
    proxy = os.environ.get("TELEAGENT_PROXY", "").strip()
    handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})
    opener = urllib.request.build_opener(handler)
    last = None
    for i in range(RETRY + 1):
        try:
            with opener.open(req, timeout=TIMEOUT) as resp:
                return resp.status, _loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, _loads(e.read()) or {"error": "HTTP %s" % e.code}
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, e)
            if i < RETRY:
                time.sleep(1.5 * (i + 1))
    return -1, {"error": last or "unknown"}

def ta_get_adaptive(path, token):
    """自适应头策略：精简头优先；若被 401 拒绝，自动换前端全量头重试一次。"""
    code, body = ta_get(path, token, full=False)
    if code == 401:
        code2, body2 = ta_get(path, token, full=True)
        if code2 != 401 or ok_body(body2):
            return code2, body2
    return code, body

def _loads(raw):
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return {}

# ------------------------------------------------------------------ 响应解析
def _dig(node, key, depth=0):
    """宽松递归找字段，接口结构调整时不至于全盘失效。"""
    if depth > 5:
        return None
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for v in node.values():
            r = _dig(v, key, depth + 1)
            if r is not None:
                return r
    elif isinstance(node, list):
        for v in node:
            r = _dig(v, key, depth + 1)
            if r is not None:
                return r
    return None

def daily_task(body):
    task = _dig(body, "daily_task")
    return task if isinstance(task, dict) else {}

def ok_body(body):
    return isinstance(body, dict) and body.get("code") in (0, 200, "0", "200")

def already_done(task):
    for k in ("claimed_today", "claimedToday", "is_claimed", "claimed"):
        if k in task:
            v = task[k]
            return v is True or v == 1 or str(v).lower() in ("true", "1")
    return False

def tail(days, total):
    bits = []
    if isinstance(days, int):
        bits.append("本期已领 %s 天" % days)
    if isinstance(total, int):
        bits.append("累计 %s 积分" % total)
    return "（%s）" % " · ".join(bits) if bits else ""

def balance(token):
    code, body = ta_get_adaptive(PATH_CENTER, token)
    if code != 200:
        return ""
    bal = _dig(body, "current_points_balance")
    return "，当前余额 %s 积分" % bal if isinstance(bal, int) else ""

# ------------------------------------------------------------------ 签到核心
def sign_account(acc):
    """签到单个账号（查状态 → 未领才领 → 复核），写回状态并返回 result dict。"""
    token = acc.get("token", "")
    now = now_s()
    r = {"account": acc["remark"], "id": acc["id"], "time": now,
         "code": "", "msg": "", "points": ""}
    if not token:
        r.update(code="EMPTY", msg="Token 为空，请先在 WebUI 填入")
        _save_result(acc, r)
        return r
    code, body = ta_get_adaptive(PATH_STATUS, token)
    # 主接口异常时用 v3 兜底
    if code == 200 and not ok_body(body):
        code3, body3 = ta_get_adaptive(PATH_STATUS_V3, token)
        if code3 == 200 and ok_body(body3):
            body = body3
    msg = ""
    if isinstance(body, dict):
        msg = str(body.get("message") or body.get("msg") or "")
    if code == -1:
        r.update(code="NETWORK", msg="网络不可达，本次跳过：%s" % body.get("error", ""))
        _save_result(acc, r)
        return r
    if code in (401, 403) or "JWT" in msg or "令牌" in msg or "登录" in msg:
        r.update(code="INVALID",
                 msg="X-Token 无效或已过期（HTTP %s / %s），请重新抓取后在 WebUI 替换"
                     % (code, msg or "no message"))
        _save_result(acc, r)
        return r
    if code != 200 or not ok_body(body):
        r.update(code="ERROR",
                 msg="状态接口异常 HTTP %s：%s" % (code, (msg or json.dumps(body, ensure_ascii=False))[:160]))
        _save_result(acc, r)
        return r
    task = daily_task(body)
    if not task:
        r.update(code="NO_TASK", msg="响应里没有 daily_task，活动可能已下线")
        _save_result(acc, r)
        return r
    if task.get("visible") is False:
        r.update(code="INACTIVE", msg="今日无「积分福利社」日签任务，跳过")
        _save_result(acc, r)
        return r
    points = task.get("daily_claimable_points") or task.get("daily_points")
    days = task.get("total_claim_days")
    total = task.get("total_claimed_points")
    r["points"] = points or ""
    if already_done(task):
        r.update(code="OK", msg="今日已领过每日积分%s" % tail(days, total))
        _save_result(acc, r)
        return r
    # 领取
    ccode, cbody = ta_get_adaptive(PATH_CLAIM, token)
    if ccode == -1:
        r.update(code="NETWORK", msg="领取请求未送达，下次重试")
        _save_result(acc, r)
        return r
    if ccode in (401, 403):
        r.update(code="INVALID", msg="领取时登录态失效（HTTP %s），请替换 Token" % ccode)
        _save_result(acc, r)
        return r
    claim_failed = ""
    if ccode != 200 or not ok_body(cbody):
        claim_failed = str((cbody or {}).get("message") or (cbody or {}).get("msg") or "")[:120]
    # 复核：必须看到 claimed_today 才算成功
    for wait in VERIFY_DELAYS:
        time.sleep(wait)
        vcode, vbody = ta_get_adaptive(PATH_STATUS, token)
        if vcode != 200:
            continue
        vtask = daily_task(vbody)
        if already_done(vtask):
            vdays = vtask.get("total_claim_days")
            vtotal = vtask.get("total_claimed_points")
            got = vtotal - total if isinstance(vtotal, int) and isinstance(total, int) else None
            if isinstance(got, int) and got > 0:
                head = "签到成功，本次 +%s 积分" % got
            elif points:
                head = "签到成功，本次 +%s 积分" % points
            else:
                head = "签到成功"
            r["points"] = got if isinstance(got, int) and got > 0 else (points or "")
            r.update(code="OK", msg=head + tail(vdays, vtotal) + balance(token))
            _save_result(acc, r)
            return r
    if claim_failed:
        r.update(code="ERROR", msg="领取失败：%s" % claim_failed)
    else:
        r.update(code="UNVERIFIED",
                 msg="领取已受理但复核未见 claimed_today 变化（服务端延迟？），稍后再确认")
    _save_result(acc, r)
    return r

def _save_result(acc, r):
    with _LOCK:
        a = STORE.find(acc["id"])
        if a:
            a["last_time"] = r["time"]
            a["last_result"] = r["msg"]
            a["last_code"] = r["code"]
            a["last_points"] = str(r.get("points") or "")
        STORE.save()

def run_all(reason="定时"):
    """跑全部启用账号，返回结果列表。"""
    if not BUSY.acquire(blocking=False):
        STORE.add_log("info", "已有任务在跑，本轮跳过")
        return []
    try:
        enabled = [a for a in STORE.accounts() if a.get("enabled", True)]
        STORE.add_log("info", "开始签到（%s），共 %d 个账号" % (reason, len(enabled)))
        results = []
        for a in enabled:
            try:
                res = sign_account(a)
            except Exception as e:
                res = {"account": a["remark"], "id": a["id"], "code": "EXC",
                       "msg": scrub("%s: %s" % (type(e).__name__, e), a.get("token", "")),
                       "time": now_s(), "points": ""}
            STORE.add_log("ok" if res["code"] == "OK" else "error",
                          "%s：%s %s" % (res["account"], res["code"], res["msg"]))
            results.append(res)
            time.sleep(0.5)
        STORE.add_log("info", "本轮签到完成（%s）" % reason)
        notify_round(results)
        return results
    finally:
        BUSY.release()

def run_single(acc_id):
    acc = STORE.find(acc_id)
    if not acc:
        BUSY.release()
        return
    STORE.add_log("info", "手动单账号签到：%s" % acc["remark"])
    try:
        try:
            res = sign_account(acc)
        except Exception as e:
            res = {"account": acc["remark"], "id": acc_id, "code": "EXC",
                   "msg": scrub("%s: %s" % (type(e).__name__, e), acc.get("token", "")),
                   "time": now_s(), "points": ""}
        STORE.add_log("ok" if res["code"] == "OK" else "error",
                      "%s：%s %s" % (res["account"], res["code"], res["msg"]))
        notify_round([res])
    finally:
        BUSY.release()

# ------------------------------------------------------------------ Telegram 推送
def tg_send(text):
    """直发 Telegram；返回是否成功。失败写日志。"""
    url = "https://api.telegram.org/bot%s/sendMessage" % TG_TOKEN
    payload = json.dumps({"chat_id": TG_CHAT_ID, "text": text,
                          "disable_web_page_preview": True}).encode()
    headers = {"Content-Type": "application/json"}
    handler = urllib.request.ProxyHandler(
        {"http": TG_PROXY, "https": TG_PROXY} if TG_PROXY else {})
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.build_opener(handler).open(req, timeout=TIMEOUT) as resp:
            j = _loads(resp.read())
            if isinstance(j, dict) and j.get("ok"):
                return True
            STORE.add_log("error", "TG 推送失败：%s" % json.dumps(j, ensure_ascii=False)[:160])
    except Exception as e:
        STORE.add_log("error", "TG 推送异常：%s: %s" % (type(e).__name__, e))
    return False

_orig_notify_round = None
def notify_round(results):
    """按时段策略推送本轮签到结果。
    0:00-1:00  当天只推一次（首个触发点推，后续静默，防 0 点附近多次触发刷屏）
    1:00-8:00  完全静默，只写日志，结果缓存至 8 点后补报
    8:00-24:00 正常推送
    """
    if not results:
        return
    now = datetime.now()
    hour = now.hour
    today = now.strftime("%Y-%m-%d")
    state = STORE.get_notify_state()

    if hour == 0:
        if state.get("midnight_sent") == today:
            STORE.add_log("info", "0点-1点时段：今日已推送过，本轮静默")
            return
        state["midnight_sent"] = today
        STORE.save()
    elif 1 <= hour < 8:
        if state.get("silent_results") != today:
            state["silent_results"] = today
            state["silent_lines"] = []
        state["silent_lines"].extend(
            "%s：%s %s" % (r["account"], r["code"], r["msg"]) for r in results)
        STORE.save()
        STORE.add_log("info", "1点-8点静默时段，%d 条结果已缓存，8点后补报" % len(results))
        return
    # 8 点后正常推送
    ok_cnt = sum(1 for r in results if r["code"] == "OK")
    title = "TeleAgent 签到 %s（成功%d/共%d）" % (now.strftime("%m-%d %H:%M"), ok_cnt, len(results))
    lines = ["%s：%s %s" % (r["account"], r["code"], r["msg"]) for r in results]
    tg_send(title + "\n" + "\n".join(lines))

def flush_pending_at_8am():
    """8 点后把夜里静默缓存的补签结果补发一条摘要（0 点已推过的用户不再重复刷屏）。"""
    state = STORE.get_notify_state()
    today = datetime.now().strftime("%Y-%m-%d")
    if state.get("silent_results") == today and state.get("midnight_sent") != today:
        lines = state.get("silent_lines") or []
        if lines:
            tg_send("TeleAgent 签到补报 %s\n" % today + "\n".join(lines))
        state["silent_results"] = ""
        state["silent_lines"] = []
        STORE.save()

# ------------------------------------------------------------------ 定时器
def scheduler_loop():
    last_date = ""
    while True:
        try:
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            anchor = now.replace(hour=SIGN_HOUR, minute=SIGN_MINUTE, second=0)
            if now >= anchor and last_date != today:
                last_date = today
                reason = ("定时 %02d:%02d" % (SIGN_HOUR, SIGN_MINUTE)
                          if now.hour == SIGN_HOUR else "补签")
                run_all(reason)
            if now.hour == 8 and now.minute < 2:
                flush_pending_at_8am()
        except Exception as e:
            try:
                STORE.add_log("error", "定时器异常：%s: %s" % (type(e).__name__, e))
            except Exception:
                pass
        time.sleep(30)

# ------------------------------------------------------------------ WebUI
SESSIONS = set()  # 内存会话 token
BUSY = threading.Semaphore(1)

class Handler(BaseHTTPRequestHandler):
    server_version = "TeleAgentSignin/1.0"

    def log_message(self, fmt, *args):
        pass

    # ---------- 基础 ----------
    def _authed(self):
        tok = self._cookie_field("session")
        return tok and tok in SESSIONS

    def _cookie_field(self, name):
        raw = self.headers.get("Cookie", "")
        m = re.search(r"(?:^|;\s*)%s=([^;]+)" % re.escape(name), raw)
        return m.group(1) if m else None

    def _send(self, code=200, ctype="text/html; charset=utf-8", body=b"", set_cookie=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()
        self.wfile.write(body)

    def _html(self, text, code=200):
        self._send(code, "text/html; charset=utf-8", text.encode("utf-8"))

    def _json(self, obj, code=200):
        self._send(code, "application/json; charset=utf-8",
                   json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        if "application/json" in self.headers.get("Content-Type", ""):
            return _loads(raw.encode()) if raw else {}
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}

    def _redirect(self, path="/", set_cookie=None):
        self.send_response(302)
        self.send_header("Location", path)
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _busy(self):
        return BUSY.locked()

    # ---------- 路由 ----------
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/login":
            return self._html(PAGE_LOGIN)
        if not self._authed():
            return self._redirect("/login")
        if path in ("/", "/index"):
            return self._html(PAGE_MAIN)
        if path == "/logout":
            SESSIONS.discard(self._cookie_field("session"))
            return self._redirect("/login")
        if path == "/api/state":
            return self._json(self._state())
        if path == "/api/logs":
            n = int(urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("n", ["200"])[0])
            with _LOCK:
                return self._json({"logs": list(STORE.logs)[-n:]})
        if path == "/api/run":
            if not BUSY.acquire(blocking=False):
                return self._json({"ok": False, "err": "已有签到任务在跑"})
            threading.Thread(target=run_all, args=("手动",), daemon=True).start()
            STORE.add_log("info", "手动触发签到")
            return self._json({"ok": True})
        if path == "/api/pause_all":
            STORE.set_all_enabled(False)
            STORE.add_log("info", "已暂停全部账号")
            return self._json({"ok": True})
        if path == "/api/resume_all":
            STORE.set_all_enabled(True)
            STORE.add_log("info", "已恢复全部账号")
            return self._json({"ok": True})
        if path == "/api/account":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            acc_id = (qs.get("id") or [""])[0]
            action = (qs.get("action") or [""])[0]
            acc = STORE.find(acc_id)
            if not acc:
                return self._json({"ok": False, "err": "账号不存在"})
            if action == "pause":
                STORE.update_account(acc_id, enabled=False)
                STORE.add_log("info", "已暂停账号：%s" % acc["remark"])
            elif action == "resume":
                STORE.update_account(acc_id, enabled=True)
                STORE.add_log("info", "已恢复账号：%s" % acc["remark"])
            elif action == "delete":
                STORE.delete_account(acc_id)
                STORE.add_log("info", "已删除账号：%s" % acc["remark"])
            elif action == "run":
                if not BUSY.acquire(blocking=False):
                    return self._json({"ok": False, "err": "已有签到任务在跑"})
                threading.Thread(target=run_single, args=(acc_id,), daemon=True).start()
            else:
                return self._json({"ok": False, "err": "未知操作"})
            return self._json({"ok": True})
        if path == "/api/test_tg":
            ok = tg_send("TeleAgent 签到系统测试消息 %s" % now_s())
            return self._json({"ok": ok})
        return self._html("404 Not Found", 404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/login":
            body = self._body()
            if secrets.compare_digest((body.get("password") or "").encode(),
                                      WEBUI_PASSWORD.encode()):
                tok = secrets.token_hex(16)
                SESSIONS.add(tok)
                self._redirect("/", set_cookie="session=%s; Path=/; HttpOnly" % tok)
            else:
                self._html(PAGE_LOGIN.replace("{{err}}",
                                              "<p style='color:#c0392b'>密码错误</p>"))
            return
        if not self._authed():
            return self._redirect("/login")
        if path == "/api/account/save":
            body = self._body()
            acc_id = body.get("id", "")
            remark = (body.get("remark") or "").strip()
            token = body.get("token") or ""
            if acc_id:
                kw = {}
                if remark:
                    kw["remark"] = remark
                if token.strip():
                    kw["token"] = token
                ok = STORE.update_account(acc_id, **kw)
                STORE.add_log("info", "更新账号 %s（%s）" % (
                    acc_id, "含 Token 替换" if token.strip() else "仅备注"))
                return self._json({"ok": ok})
            if not token.strip():
                return self._json({"ok": False, "err": "Token 不能为空"})
            acc = STORE.add_account(remark, token)
            STORE.add_log("info", "新增账号：%s [%s]" % (acc["remark"], mask_token(acc["token"])))
            return self._json({"ok": True, "id": acc["id"]})
        return self._json({"ok": False, "err": "未知路径"}, 404)

    # ---------- 数据 ----------
    def _state(self):
        with _LOCK:
            accs = [{
                "id": a["id"], "remark": a["remark"], "enabled": a.get("enabled", True),
                "token_mask": mask_token(a.get("token", "")),
                "last_time": a.get("last_time", ""), "last_code": a.get("last_code", ""),
                "last_result": a.get("last_result", ""),
                "last_points": a.get("last_points", ""),
            } for a in STORE.accounts()]
        return {
            "accounts": accs,
            "notify": {"token_mask": TG_TOKEN[:6] + "***", "chat_id": TG_CHAT_ID,
                       "proxy": TG_PROXY or "(无)"},
            "sign_hour": SIGN_HOUR,
        }

# ------------------------------------------------------------------ 页面
PAGE_LOGIN = """<!doctype html><html lang=zh><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>登录 · TeleAgent 签到</title>
<style>body{font-family:-apple-system,"PingFang SC",sans-serif;background:#10141f;color:#dfe6f3;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.box{background:#1a2130;padding:32px;border-radius:14px;width:300px;box-shadow:0 8px 30px rgba(0,0,0,.4)}
h2{margin:0 0 18px;font-weight:600}input{width:100%;padding:10px;border-radius:8px;border:1px solid #2c3548;
background:#0d1117;color:#dfe6f3;box-sizing:border-box;margin-bottom:14px;font-size:15px}
button{width:100%;padding:10px;border:0;border-radius:8px;background:#3b82f6;color:#fff;font-size:15px;cursor:pointer}
button:hover{background:#2f6fe0}</style></head><body><div class=box>
<h2>⭐ TeleAgent 签到</h2>{{err}}
<form method=post action=/login><input type=password name=password placeholder=访问密码 autofocus>
<button>登录</button></form></div></body></html>"""

PAGE_MAIN = """<!doctype html><html lang=zh><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>TeleAgent 签到</title>
<style>
body{font-family:-apple-system,"PingFang SC",sans-serif;background:#10141f;color:#dfe6f3;margin:0;padding:20px}
.wrap{max-width:960px;margin:0 auto}h1{font-size:20px;font-weight:600}
.bar{display:flex;gap:10px;margin:14px 0;flex-wrap:wrap}
button{padding:8px 14px;border:0;border-radius:8px;background:#3b82f6;color:#fff;cursor:pointer;font-size:13px}
button.gray{background:#33405c}button.red{background:#c0392b}button.green{background:#27ae60}
table{width:100%;border-collapse:collapse;background:#1a2130;border-radius:10px;overflow:hidden;font-size:14px}
th,td{padding:9px 10px;text-align:left;border-bottom:1px solid #232c40}
th{color:#8fa3c8;font-weight:500;font-size:12px}
.tag{padding:2px 8px;border-radius:10px;font-size:12px}
.tag.OK{background:#14532d;color:#86efac}.tag.FAIL,.tag.INVALID,.tag.ERROR,.tag.EXC{background:#7f1d1d;color:#fca5a5}
.tag.EMPTY,.tag.NETWORK,.tag.UNVERIFIED{background:#78350f;color:#fcd34d}
.paused{opacity:.5}
.dlg{position:fixed;inset:0;background:rgba(0,0,0,.6);display:none;align-items:center;justify-content:center}
.dlg .c{background:#1a2130;padding:24px;border-radius:12px;width:460px;max-width:92vw}
textarea,input{width:100%;box-sizing:border-box;padding:9px;border-radius:8px;border:1px solid #2c3548;
background:#0d1117;color:#dfe6f3;font-size:13px;margin-bottom:12px}
textarea{height:90px;resize:vertical}
#logs{background:#0d1117;border-radius:10px;padding:12px;height:260px;overflow-y:auto;font:12px/1.7 ui-monospace,monospace}
.ok{color:#86efac}.err{color:#fca5a5}.info{color:#8fa3c8}
h3{font-size:14px;color:#8fa3c8;margin:20px 0 8px}
</style></head><body><div class=wrap>
<h1>⭐ TeleAgent 每日积分签到
<span style="font-size:12px;color:#8fa3c8">ta.teleai.com.cn · 自动签到 0:05 · TG 0-1点只推一次 / 1-8点静默补报</span></h1>
<div class=bar>
  <button onclick="api('/api/run')">▶ 立即签到全部</button>
  <button class=gray onclick="api('/api/pause_all')">⏸ 暂停全部</button>
  <button class=gray onclick="api('/api/resume_all')">▶ 恢复全部</button>
  <button class=green onclick="api('/api/test_tg')">✈ TG 测试</button>
  <button class=red onclick="location='/logout'">退出</button>
</div>
<h3>账号</h3>
<table id=accs><thead><tr><th>备注</th><th>Token</th><th>状态</th><th>最近结果</th><th>时间</th><th>操作</th></tr></thead>
<tbody></tbody></table>
<div class=bar style=margin-top:10px>
  <button onclick="dlgAdd()">＋ 添加账号</button>
</div>
<h3>签到日志</h3>
<div id=logs></div>
</div>

<div class=dlg id=dlg><div class=c>
<h3 id=dlgTitle style=margin-top:0>添加账号</h3>
<input id=fRemark placeholder="备注（如：主号）">
<textarea id=fToken placeholder="粘贴 X-Token（网页版 F12 → Application → Cookies → x-token；也支持直接粘整串 cookie 自动抽取）"></textarea>
<button onclick="save()">保存</button> <button class=gray onclick="hide()">取消</button>
</div></div>

<script>
let editing=null;
function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
async function refresh(){
  let s=await (await fetch('/api/state')).json();
  let tb=document.querySelector('#accs tbody');tb.innerHTML='';
  for(const a of s.accounts){
    let tr=document.createElement('tr');
    if(!a.enabled)tr.className='paused';
    tr.innerHTML=`<td>${esc(a.remark)}</td><td title="${esc(a.token_mask)}">${esc(a.token_mask)}</td>
    <td><span class="tag ${a.last_code}">${a.last_code||'—'}</span></td>
    <td>${esc(a.last_result)}</td><td>${esc(a.last_time)}</td>
    <td>
      ${a.enabled?`<button class=gray onclick="api('/api/account?id=${a.id}&action=pause')">暂停</button>`
                 :`<button onclick="api('/api/account?id=${a.id}&action=resume')">恢复</button>`}
      <button class=gray onclick="edit('${a.id}','${esc(a.remark)}')">换Token</button>
      <button onclick="api('/api/account?id=${a.id}&action=run')">签到</button>
      <button class=red onclick="del('${a.id}')">删除</button>
    </td>`;
    tb.appendChild(tr);
  }
  let lg=await (await fetch('/api/logs?n=200')).json();
  document.getElementById('logs').innerHTML=lg.logs.map(l=>
    `<div><span class=${l.level}>[${l.t}]</span> ${esc(l.msg)}</div>`).join('');
}
function api(u){fetch(u).then(r=>r.json()).then(j=>{if(j.err)alert(j.err);refresh()})}
function del(id){if(confirm('确认删除该账号？'))api('/api/account?id='+id+'&action=delete')}
function dlgAdd(){editing=null;fRemark.value='';fToken.value='';document.getElementById('dlg').style.display='flex'}
function edit(id,remark){editing=id;fRemark.value=remark;fToken.value='';document.getElementById('dlg').style.display='flex'}
function hide(){document.getElementById('dlg').style.display='none'}
async function save(){
  let body={remark:fRemark.value,token:fToken.value};
  if(editing)body.id=editing;
  let r=await fetch('/api/account/save',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json());
  if(r.err){alert(r.err);return}
  hide();refresh();
}
refresh();setInterval(refresh,15000);
</script></body></html>"""

# ------------------------------------------------------------------ 启动
def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    mode = (sys.argv[1] if len(sys.argv) > 1 else "web").lower()
    if mode == "run":
        run_all("命令行")
        return 0
    if mode == "status":
        for a in STORE.accounts():
            print("%s [%s] %s %s" % (a["remark"], "启用" if a.get("enabled") else "暂停",
                                     a.get("last_code") or "—", a.get("last_result") or ""))
        return 0
    threading.Thread(target=scheduler_loop, daemon=True).start()
    STORE.add_log("info", "服务启动，监听 %d，自动签到 %02d:%02d，TG 推送已配置" % (PORT, SIGN_HOUR, SIGN_MINUTE))
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("TeleAgent 签到 WebUI: http://127.0.0.1:%d  密码: %s" % (PORT, WEBUI_PASSWORD), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0

if __name__ == "__main__":
    sys.exit(main())
