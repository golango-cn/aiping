#!/usr/bin/env python3
"""
aiping — HTTP → 可点击桌面通知网关

收到 POST /notify 弹 GNOME 系统通知（进通知中心），
通知可点击 / 带「查看详情」按钮，点击后用 xdg-open 打开本地详情页
  http://127.0.0.1:8787/alert/<id>
详情页由本网关自己提供，展示该次告警的完整结构化信息。

依赖：PyGObject + libnotify（Ubuntu 自带 python3-gi / libnotify-bin）

配置（环境变量）：
  AIPING_HOST   监听地址，默认 127.0.0.1
  AIPING_PORT   端口，默认 8787
  AIPING_TOKEN  鉴权 token，留空不校验
  AIPING_APP    通知应用名，默认 "Aiping"

调用：
  curl -s -X POST http://127.0.0.1:8787/notify \\
    -H 'Content-Type: application/json' \\
    -d '{
      "id":"cpu-17723",            # 可选，幂等 id；不传自动生成
      "title":"巡检告警",
      "body":"177.23 CPU 95%",
      "urgency":"加急",            # 一般/普通/重要/加急
      "detail":"多行详细说明\\nTOP: java pid=1234",   # 可选，详情页正文
      "fields":{"主机":"177.23","负载":9.8}            # 可选，键值表格
    }'
"""

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import gi
gi.require_version("Notify", "0.7")
from gi.repository import GLib, Notify  # noqa: E402

HOST = os.environ.get("AIPING_HOST", "127.0.0.1")
PORT = int(os.environ.get("AIPING_PORT", "8787"))
TOKEN = os.environ.get("AIPING_TOKEN", "")
APP = os.environ.get("AIPING_APP", "Aiping")
MAX_ALERTS = 500

# SQLite 持久化：告警存数据库，重启不丢
DB_PATH = os.path.expanduser("~/.local/share/aiping/alerts.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
DB_LOCK = threading.Lock()


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_db():
    conn = _get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id TEXT PRIMARY KEY,
            title TEXT, body TEXT, urgency INTEGER, urgency_label TEXT,
            icon TEXT, detail TEXT, fields TEXT, extra TEXT, raw TEXT,
            ts TEXT, ts_sort TEXT, read INTEGER DEFAULT 0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ts_sort ON alerts(ts_sort DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_urgency ON alerts(urgency)")
    conn.commit()
    conn.close()


def _db_save(record: dict):
    """插入或覆盖一条告警。"""
    with DB_LOCK:
        conn = _get_db()
        conn.execute("""
            INSERT OR REPLACE INTO alerts
            (id,title,body,urgency,urgency_label,icon,detail,fields,extra,raw,ts,ts_sort,read)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            record["id"], record["title"], record["body"],
            record["urgency"], record["urgency_label"],
            record.get("icon", ""), record.get("detail", ""),
            json.dumps(record.get("fields", {}), ensure_ascii=False),
            json.dumps(record.get("extra", {}), ensure_ascii=False),
            json.dumps(record.get("raw", {}), ensure_ascii=False),
            record["ts"], record["ts_sort"],
            record.get("read", 0),
        ))
        conn.commit()
        conn.close()


def _db_load_all() -> list:
    """加载全部告警，按 ts_sort 倒序。"""
    with DB_LOCK:
        conn = _get_db()
        rows = conn.execute(
            "SELECT * FROM alerts ORDER BY ts_sort DESC"
        ).fetchall()
        conn.close()
    result = []
    for r in rows:
        result.append({
            "id": r["id"], "title": r["title"], "body": r["body"],
            "urgency": r["urgency"], "urgency_label": r["urgency_label"],
            "icon": r["icon"], "detail": r["detail"],
            "fields": json.loads(r["fields"] or "{}"),
            "extra": json.loads(r["extra"] or "{}"),
            "raw": json.loads(r["raw"] or "{}"),
            "ts": r["ts"], "ts_sort": r["ts_sort"],
            "read": r["read"],
        })
    return result


def _db_load_one(alert_id: str) -> dict | None:
    with DB_LOCK:
        conn = _get_db()
        r = conn.execute(
            "SELECT * FROM alerts WHERE id=?", (alert_id,)
        ).fetchone()
        conn.close()
    if not r:
        return None
    return {
        "id": r["id"], "title": r["title"], "body": r["body"],
        "urgency": r["urgency"], "urgency_label": r["urgency_label"],
        "icon": r["icon"], "detail": r["detail"],
        "fields": json.loads(r["fields"] or "{}"),
        "extra": json.loads(r["extra"] or "{}"),
        "raw": json.loads(r["raw"] or "{}"),
        "ts": r["ts"], "ts_sort": r["ts_sort"],
        "read": r["read"],
    }


def _db_mark_read(alert_id: str):
    with DB_LOCK:
        conn = _get_db()
        conn.execute("UPDATE alerts SET read=1 WHERE id=?", (alert_id,))
        conn.commit()
        conn.close()


def _db_count() -> int:
    with DB_LOCK:
        conn = _get_db()
        n = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
        conn.close()
    return n


def _db_trim():
    """超过 MAX_ALERTS 时删除最早的。"""
    with DB_LOCK:
        conn = _get_db()
        n = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
        if n > MAX_ALERTS:
            conn.execute("""
                DELETE FROM alerts WHERE id IN (
                    SELECT id FROM alerts ORDER BY ts_sort ASC LIMIT ?
                )
            """, (n - MAX_ALERTS,))
            conn.commit()
        conn.close()


# 内存缓存（从 DB 加载，读操作走缓存避免每次查库）
ALERTS_CACHE: list = []
CACHE_LOCK = threading.Lock()


def _refresh_cache():
    global ALERTS_CACHE
    with CACHE_LOCK:
        ALERTS_CACHE = _db_load_all()


_init_db()
_refresh_cache()

def _detect_xauthority() -> str:
    """GNOME Wayland 把 X 授权放 /run/user/$UID/.mutter-Xwaylandauth.*，
    每次登录后缀变，必须动态探测。fallback 到 ~/.Xauthority。"""
    import glob
    for p in glob.glob(f"/run/user/{os.getuid()}/.mutter-Xwaylandauth.*"):
        if os.path.exists(p):
            return p
    home_xauth = os.path.expanduser("~/.Xauthority")
    return home_xauth if os.path.exists(home_xauth) else ""


AIPING_ENV = {
    "DISPLAY": os.environ.get("DISPLAY", ":0"),
    "DBUS_SESSION_BUS_ADDRESS": os.environ.get(
        "DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/%d/bus" % os.getuid()
    ),
    # systemd user service 常常漏这几个，xdg-open 启浏览器必需
    "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"),
    "XAUTHORITY": _detect_xauthority(),
    "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
    "HOME": os.environ.get("HOME", os.path.expanduser("~")),
}
URGENCY_LEVELS = {"一般": 0, "普通": 1, "重要": 2, "加急": 3}
URGENCY_LABEL = {0: "一般", 1: "普通", 2: "重要", 3: "加急"}
# 通知阈值：重要(2) 及以上才弹桌面通知
NOTIFY_THRESHOLD = 2

_glib_loop: GLib.MainLoop | None = None

# 关键：保活已 show 的 Notification 对象。
# PyGObject 里 Notification 是局部变量时，show() 后会被 Python GC，
# 通知虽仍显示在桌面，但 action 回调随之失效 → 点击无反应。
LIVE_NOTIFS: set = set()


# ---------- 通知 ----------

# 按级别选系统图标
URGENCY_ICON = {
    0: "dialog-information",   # 一般
    1: "dialog-information",   # 普通
    2: "dialog-warning",       # 重要
    3: "dialog-error",         # 加急
}


def _show_notification(alert_id: str, title: str, body: str,
                       urgency: int, icon: str) -> None:
    """弹 GNOME 系统通知（进通知中心、响铃）。

    GNOME 50 通知预览 body 强制单行渲染，任何换行 markup（\\n / <br> / &#10;）
    都无效。所以采用：title 放告警描述，body 只放详情页 URL。
    预览里 title/body 天然两行 → URL 单独成行且可点击打开详情页。
    级别用系统图标体现（dialog-warning / dialog-information）。
    """
    detail_url = f"http://127.0.0.1:{PORT}/alert/{alert_id}"

    # title 装"告警摘要"，把 body 关键信息并进 title（预览第一行）
    if body:
        notify_title = f"{title} — {body}"
    else:
        notify_title = title
    # body 只放 URL，预览里单独第二行、可点击
    notify_body = detail_url

    final_icon = URGENCY_ICON.get(urgency, "dialog-information")

    n = Notify.Notification.new(notify_title, notify_body, final_icon)
    n.set_urgency(urgency)
    n.connect("closed", lambda notif: LIVE_NOTIFS.discard(notif))
    try:
        n.show()
        LIVE_NOTIFS.add(n)
        sys.stderr.write(f"[notify] shown: {alert_id} urgency={urgency} "
                         f"(live={len(LIVE_NOTIFS)})\n")
        sys.stderr.flush()
    except Exception as e:
        sys.stderr.write(f"notify show failed: {e}\n")
        sys.stderr.flush()


# ---------- 详情页 HTML ----------

_DETAIL_CSS = """
*{box-sizing:border-box}
body{font:14px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",sans-serif;
  max-width:1100px;margin:0 auto;padding:32px 24px;color:var(--fg);background:var(--bg);min-height:100vh}
.back-btn{display:inline-block;margin-bottom:16px;padding:7px 16px;border:1px solid var(--border);
  border-radius:8px;background:var(--card);color:var(--link);text-decoration:none;font-size:13px;
  font-weight:500;transition:all .15s}
.back-btn:hover{background:var(--hover);border-color:var(--link);text-decoration:none}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:28px 32px;
  box-shadow:0 1px 3px var(--shadow)}
h1{font-size:22px;margin:0 0 6px;font-weight:600;line-height:1.3}
.meta{color:var(--muted);font-size:13px;margin-bottom:20px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.badge{display:inline-block;padding:3px 10px;border-radius:12px;font-size:12px;
  font-weight:600;color:#fff;letter-spacing:.3px}
.badge.加急{background:#dc2626}
.badge.重要{background:#ea580c}
.badge.普通{background:#2563eb}
.badge.一般{background:#9ca3af}
.sep{color:var(--border)}
.mono{font-family:ui-monospace,SFMono-Regular,"Cascadia Code",monospace;font-size:12px;color:var(--muted)}
.section{margin-top:22px}
.section h3{font-size:13px;font-weight:600;color:var(--muted);text-transform:uppercase;
  letter-spacing:.5px;margin:0 0 10px;padding-bottom:6px;border-bottom:2px solid var(--row-border)}
.body-text{font-size:15px;color:var(--fg);padding:10px 0}
pre{background:var(--bg);padding:14px 16px;border-radius:8px;white-space:pre-wrap;
  word-break:break-word;border:1px solid var(--border);font-size:13px;line-height:1.6;
  font-family:ui-monospace,SFMono-Regular,monospace;color:var(--fg);overflow-x:auto}
table.kv{border-collapse:collapse;width:100%}
table.kv td{padding:9px 12px;border-top:1px solid var(--row-border);vertical-align:top;font-size:14px}
table.kv td:first-child{color:var(--muted);width:32%;white-space:nowrap;font-weight:500}
table.kv td:last-child{color:var(--fg);word-break:break-word}
.url-box{background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:12px 16px;
  margin-top:8px;font-family:ui-monospace,monospace;font-size:13px;word-break:break-all}
.url-box a{color:var(--link);text-decoration:none}
.url-box a:hover{text-decoration:underline}
.raw-toggle{margin-top:8px;font-size:13px}
.raw-toggle summary{cursor:pointer;color:var(--muted);font-weight:500;padding:6px 0}
.raw-toggle summary:hover{color:var(--link)}
.footer{margin-top:28px;padding-top:16px;border-top:1px solid var(--border);
  font-size:12px;color:var(--muted);display:flex;gap:16px}
.footer a{color:var(--link);text-decoration:none}
.footer a:hover{text-decoration:underline}
.theme-bar{position:fixed;top:12px;right:12px;z-index:100}
.theme-bar select{padding:4px 8px;border:1px solid var(--border);border-radius:6px;
  background:var(--card);color:var(--fg);font-size:12px;cursor:pointer}
@media(max-width:600px){body{padding:12px}.card{padding:18px}table.kv td:first-child{width:40%}}
"""

# 主题变量定义（详情页和列表页共用）
_THEME_VARS = """
:root,[data-theme=light]{--bg:#f6f8fa;--fg:#1f2328;--card:#fff;--muted:#6b7280;--border:#e1e4e8;
  --row-border:#f0f0f0;--th-bg:#fafbfc;--hover:#f9fafb;--shadow:rgba(0,0,0,.05);--link:#0969da;--unread-bg:#eef4ff}
[data-theme=dark]{--bg:#0d1117;--fg:#e6edf3;--card:#161b22;--muted:#8b949e;--border:#30363d;
  --row-border:#21262d;--th-bg:#161b22;--hover:#1c2128;--shadow:rgba(0,0,0,.3);--link:#58a6ff;--unread-bg:#1a2332}
[data-theme=green]{--bg:#e8f0e5;--fg:#2d3a2e;--card:#f7faf5;--muted:#5a7260;--border:#c5d6c0;
  --row-border:#dce8d8;--th-bg:#f0f5ec;--hover:#eef4ea;--shadow:rgba(45,58,46,.08);--link:#2d8c5a;--unread-bg:#eaf4e8}
[data-theme=warm]{--bg:#fdf6ec;--fg:#5c4a2e;--card:#fffaf2;--muted:#9a8260;--border:#e8d5b8;
  --row-border:#f0e2cc;--th-bg:#faf3e8;--hover:#fcf5ea;--shadow:rgba(92,74,46,.06);--link:#c47b3a;--unread-bg:#fbf2e4}
[data-theme=nightblue]{--bg:#0a1628;--fg:#c8d6e8;--card:#14213d;--muted:#6b7fa0;--border:#2a3f5f;
  --row-border:#1e3050;--th-bg:#162447;--hover:#1a2d52;--shadow:rgba(0,0,0,.3);--link:#4a9eff;--unread-bg:#11204a}
"""

_THEME_SCRIPT = """
<script>(function(){var t=localStorage.getItem('ng-theme')||'light';
document.documentElement.setAttribute('data-theme',t);
window.addEventListener('DOMContentLoaded',function(){
var s=document.getElementById('theme-sel');if(s)s.value=t;
if(s)s.onchange=function(){localStorage.setItem('ng-theme',s.value);
document.documentElement.setAttribute('data-theme',s.value)};});})();</script>
"""

_LIST_CSS = """
*{box-sizing:border-box}
body{font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  margin:0;padding:24px;background:var(--bg);color:var(--fg);min-height:100vh}
.wrap{max-width:1400px;margin:0 auto}
.header{display:flex;align-items:center;justify-content:space-between;margin-bottom:18px;flex-wrap:wrap;gap:12px}
.header-left{display:flex;align-items:center;gap:14px}
h1{font-size:20px;margin:0;font-weight:600}
.count{font-size:13px;color:var(--muted)}
.theme-select{padding:5px 10px;border:1px solid var(--border);border-radius:6px;
  background:var(--card);color:var(--fg);font-size:13px;cursor:pointer}
table{border-collapse:collapse;width:100%;background:var(--card);border:1px solid var(--border);
  border-radius:10px;overflow:hidden;box-shadow:0 1px 3px var(--shadow)}
th,td{padding:11px 14px;border-bottom:1px solid var(--row-border);text-align:left;vertical-align:middle}
th{background:var(--th-bg);color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;
  letter-spacing:.4px;border-bottom:2px solid var(--border)}
tr:last-child td{border-bottom:none}
tr:hover{background:var(--hover)}
.badge{display:inline-block;padding:2px 9px;border-radius:10px;font-size:11px;font-weight:600;color:#fff}
.badge.加急{background:#dc2626}
.badge.重要{background:#ea580c}
.badge.普通{background:#2563eb}
.badge.一般{background:#9ca3af}
tr.viewed td a{color:var(--muted)}
tr.viewed td a:hover{color:var(--muted);text-decoration:none}
.empty{color:var(--muted);padding:40px;text-align:center;font-size:14px}
.mono{font-family:ui-monospace,SFMono-Regular,monospace;font-size:12px;color:var(--muted)}
a{color:var(--link);text-decoration:none}
a:hover{text-decoration:underline}
.pager{display:flex;align-items:center;justify-content:space-between;margin-top:16px;flex-wrap:wrap;gap:10px}
.pager-info{font-size:13px;color:var(--muted)}
.pager-btns{display:flex;gap:6px;align-items:center}
.pager-btns a,.pager-btns span{display:inline-block;padding:6px 13px;border:1px solid var(--border);
  border-radius:6px;font-size:13px;color:var(--fg);background:var(--card);text-decoration:none;min-width:36px;text-align:center}
.pager-btns a:hover{background:var(--hover);text-decoration:none}
.pager-btns .current{background:var(--link);color:#fff;border-color:var(--link)}
.pager-btns .disabled{color:var(--muted);opacity:.5;cursor:default}
/* 已读/未读状态 */
tr.unread{background:var(--unread-bg)}
tr.unread td:first-child{border-left:3px solid var(--link)}
tr.read{opacity:.7}
.title-unread{color:var(--link);font-weight:600}
.title-read{color:var(--muted);font-weight:400}
.toolbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.theme-select,.filter-select{padding:5px 10px;border:1px solid var(--border);border-radius:6px;
  background:var(--card);color:var(--fg);font-size:13px;cursor:pointer}
.auto-refresh{display:flex;align-items:center;gap:4px;font-size:13px;color:var(--muted);cursor:pointer;user-select:none}
.auto-refresh input{cursor:pointer}
#ar-interval{padding:4px 8px;border:1px solid var(--border);border-radius:6px;
  background:var(--card);color:var(--fg);font-size:12px}
#ar-interval:disabled{opacity:.5}
"""

PAGE_SIZE = 100


def _render_list(page: int = 1, urgency_filter: str = "") -> bytes:
    # 从缓存读取（缓存由 DB 刷新）
    with CACHE_LOCK:
        all_items = list(ALERTS_CACHE)
    # 级别筛选
    if urgency_filter in ("一般", "普通", "重要", "加急"):
        target_val = URGENCY_LEVELS[urgency_filter]
        all_items = [a for a in all_items if a.get("urgency", 1) == target_val]
    total = len(all_items)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, total_pages))
    start = (page - 1) * PAGE_SIZE
    items = all_items[start:start + PAGE_SIZE]

    # 翻页/筛选的 query 前缀
    qparts = []
    if urgency_filter:
        qparts.append(f"urgency={urgency_filter}")
    def q(page_num=None):
        qs = []
        if page_num is not None:
            qs.append(f"page={page_num}")
        if urgency_filter:
            qs.append(f"urgency={urgency_filter}")
        return "?" + "&".join(qs) if qs else ""

    parts = ["<!doctype html><meta charset=utf8><meta name=viewport content='width=device-width,initial-scale=1'>",
             "<title>告警列表</title>",
             f"<style>{_THEME_VARS}{_LIST_CSS}</style>",
             _THEME_SCRIPT,
             "<div class=wrap>",
             "<div class=header><div class=header-left><h1>告警列表</h1>",
             f'<span class=count>共 {total} 条</span></div>',
             '<div class=toolbar>',
             '<select class=filter-select id=urgency-filter onchange="applyFilter()">',
             f'<option value=""{" selected" if not urgency_filter else ""}>全部级别</option>',
             f'<option value=加急{" selected" if urgency_filter=="加急" else ""}>加急</option>',
             f'<option value=重要{" selected" if urgency_filter=="重要" else ""}>重要</option>',
             f'<option value=普通{" selected" if urgency_filter=="普通" else ""}>普通</option>',
             f'<option value=一般{" selected" if urgency_filter=="一般" else ""}>一般</option>',
             '</select>',
             '<select class=theme-select id=theme-sel>',
             '<option value=light>浅色</option>',
             '<option value=dark>深色</option>',
             '<option value=green>护眼绿</option>',
             '<option value=warm>暖色</option>',
             '<option value=nightblue>夜蓝</option>',
             '</select>',
             '<label class=auto-refresh><input type=checkbox id=ar-toggle onchange="toggleAR()">自动刷新</label>',
             '<select id=ar-interval onchange="resetAR()" disabled>',
             '<option value=5>5秒</option><option value=10 selected>10秒</option>',
             '<option value=30>30秒</option><option value=60>60秒</option>',
             '</select>',
             '</div></div>']

    # 列顺序：ID → 时间 → 级别 → 标题 → 摘要
    parts.append("<table><thead><tr>"
                 "<th>ID</th><th>时间</th><th>级别</th><th>标题</th><th>摘要</th>"
                 "</tr></thead><tbody>")
    if not items:
        parts.append('<tr><td colspan=5 class=empty>暂无告警</td></tr>')
    for a in items:
        u = a.get("urgency", 1)
        ulabel = URGENCY_LABEL.get(u, "普通")
        aid = escape(a.get("id", ""))
        is_read = a.get("read", 0)
        row_cls = "read" if is_read else "unread"
        title_cls = "title-read" if is_read else "title-unread"
        parts.append(
            f"<tr class={row_cls}><td class=mono>{aid}</td>"
            f"<td class=mono>{escape(a.get('ts',''))}</td>"
            f"<td><span class='badge {ulabel}'>{ulabel}</span></td>"
            f"<td><a href='/alert/{aid}' class='{title_cls}'>{escape(a.get('title',''))}</a></td>"
            f"<td>{escape(a.get('body','')[:80])}</td></tr>"
        )
    parts.append("</tbody></table>")

    # 分页
    end = min(start + PAGE_SIZE, total)
    parts.append('<div class=pager>'
                 f'<span class=pager-info>第 {start+1}-{end} 条 / 共 {total} 条 · 第 {page}/{total_pages} 页</span>'
                 '<span class=pager-btns>')
    if page > 1:
        parts.append(f'<a href="/alerts{q(1)}">«</a>')
        parts.append(f'<a href="/alerts{q(page-1)}">‹</a>')
    else:
        parts.append('<span class=disabled>«</span><span class=disabled>‹</span>')
    parts.append(f'<span class=current>{page}</span>')
    if page < total_pages:
        parts.append(f'<a href="/alerts{q(page+1)}">›</a>')
        parts.append(f'<a href="/alerts{q(total_pages)}">»</a>')
    else:
        parts.append('<span class=disabled>›</span><span class=disabled>»</span>')
    parts.append('</span></div>')

    # 自动刷新 + 筛选 JS
    parts.append("""
<script>
function applyFilter(){
  var v=document.getElementById('urgency-filter').value;
  location.href='/alerts'+(v?'?urgency='+v:'');
}
var arTimer=null;
function toggleAR(){
  var cb=document.getElementById('ar-toggle');
  var iv=document.getElementById('ar-interval');
  iv.disabled=!cb.checked;
  if(cb.checked){startAR()}else{stopAR()}
}
function startAR(){
  stopAR();
  var sec=parseInt(document.getElementById('ar-interval').value)||10;
  arTimer=setTimeout(function(){location.reload()},sec*1000);
}
function stopAR(){if(arTimer){clearTimeout(arTimer);arTimer=null}}
function resetAR(){
  var cb=document.getElementById('ar-toggle');
  if(cb.checked){startAR()}
}
</script>
""")
    parts.append("</div>")
    return "".join(parts).encode()


def _render_detail(a: dict) -> bytes:
    urgency = a.get("urgency", 1)
    ulabel = URGENCY_LABEL.get(urgency, "普通")
    ts = a.get("ts", "")
    body = a.get("body", "")
    detail = a.get("detail", "")
    fields = a.get("fields", {}) or {}
    extra = a.get("extra", {}) or {}
    alert_id = a.get("id", "")
    title = escape(a.get("title", "通知"))
    detail_url = f"http://127.0.0.1:{PORT}/alert/{alert_id}"

    parts = [f"<!doctype html><meta charset=utf8><meta name=viewport content='width=device-width,initial-scale=1'>",
             f"<title>{title}</title>",
             f"<style>{_THEME_VARS}{_DETAIL_CSS}</style>",
             _THEME_SCRIPT,
             '<div class=theme-bar><select id=theme-sel>'
             '<option value=light>浅色</option><option value=dark>深色</option>'
             '<option value=green>护眼绿</option><option value=warm>暖色</option>'
             '<option value=nightblue>夜蓝</option></select></div>',
             f"<div class=card>",
             '<a href="/alerts" class=back-btn>← 返回列表</a>',
             f"<h1>{title}</h1>",
             f"<div class=meta><span class='badge {ulabel}'>{ulabel}</span>"
             f"<span class=sep>·</span><span class=mono>ID: {escape(alert_id)}</span>"
             f"<span class=sep>·</span><span>{escape(ts)}</span></div>"]

    # 正文
    if body:
        parts.append(f'<div class=section><h3>摘要</h3><div class=body-text>{escape(body)}</div></div>')

    # 详情页 URL（可复制/可点）
    parts.append(f'<div class=section><h3>详情链接</h3>'
                 f'<div class=url-box><a href="{detail_url}">{escape(detail_url)}</a></div></div>')

    # detail 多行
    if detail:
        parts.append(f'<div class=section><h3>详细信息</h3><pre>{escape(detail)}</pre></div>')

    # fields 键值表
    if fields:
        parts.append('<div class=section><h3>字段</h3><table class=kv>')
        for k, v in fields.items():
            parts.append(f"<tr><td>{escape(str(k))}</td><td>{escape(str(v))}</td></tr>")
        parts.append("</table></div>")

    # 额外字段（调用方传的、不在标准字段里的）
    if extra:
        parts.append('<div class=section><h3>附加数据</h3><table class=kv>')
        for k, v in extra.items():
            parts.append(f"<tr><td>{escape(str(k))}</td><td>{escape(str(v))}</td></tr>")
        parts.append("</table></div>")

    # 原始 payload（可折叠）
    raw = a.get("raw", {})
    if raw:
        raw_json = escape(json.dumps(raw, ensure_ascii=False, indent=2))
        parts.append(f'<details class="raw-toggle section"><summary>原始请求数据 (JSON)</summary>'
                     f'<pre>{raw_json}</pre></details>')

    parts.append(f'<div class=footer><a href="/alerts">← 返回列表</a>'
                 f'<a href="/health">health</a>'
                 f'<span>由 aiping 生成</span></div>')
    parts.append("</div>")
    return "".join(parts).encode()


# ---------- HTTP
# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "aiping/2.0"

    def _send(self, code, content_type, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, "application/json", json.dumps(obj, ensure_ascii=False).encode())

    def _check_token(self):
        if not TOKEN:
            return True
        auth = self.headers.get("Authorization", "")
        key = self.headers.get("X-Api-Key", "")
        return auth == f"Bearer {TOKEN}" or key == TOKEN

    def do_GET(self):
        # 去掉 query string 用于路径判断
        path = self.path.split("?")[0]
        query = self.path.split("?")[1] if "?" in self.path else ""

        if path in ("/", "/health"):
            self._json(200, {"ok": True, "service": "aiping", "app": APP,
                             "auth": bool(TOKEN), "alerts": _db_count()})
            return
        if path == "/alerts":
            # 解析 page 和 urgency 参数（中文需 URL 解码）
            from urllib.parse import unquote
            page = 1
            urgency_f = ""
            for kv in query.split("&"):
                if kv.startswith("page="):
                    try:
                        page = int(kv[5:])
                    except ValueError:
                        page = 1
                elif kv.startswith("urgency="):
                    urgency_f = unquote(kv[8:])
            self._send(200, "text/html; charset=utf-8",
                       _render_list(page, urgency_f))
            return
        if path.startswith("/alert/"):
            aid = path[len("/alert/"):]
            a = _db_load_one(aid)
            if not a:
                self._send(404, "text/html; charset=utf-8",
                           "<h1>404</h1><p>告警不存在或已被清理</p>".encode())
                return
            # 标记已读
            _db_mark_read(aid)
            a["read"] = 1
            _refresh_cache()
            self._send(200, "text/html; charset=utf-8", _render_detail(a))
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._check_token():
            self._json(401, {"error": "unauthorized"})
            return
        if self.path != "/notify":
            self._json(404, {"error": "not found"})
            return
        try:
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            payload = json.loads(raw or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"error": "invalid json"})
            return

        alert_id = str(payload.get("id") or "").strip() or uuid.uuid4().hex[:10]
        title = str(payload.get("title", "通知")).strip() or "通知"
        body = str(payload.get("body", "")).strip()
        urgency = str(payload.get("urgency", "普通"))
        urgency_val = URGENCY_LEVELS.get(urgency, 1)
        icon = str(payload.get("icon", "")).strip()
        detail = str(payload.get("detail", ""))
        fields = payload.get("fields", {}) or {}
        if not isinstance(fields, dict):
            fields = {}

        # 已知字段单独提取便于渲染；raw 保留完整原始 payload（含任意额外字段）
        known_keys = {"id", "title", "body", "urgency", "icon", "detail", "fields"}
        extra = {k: v for k, v in payload.items() if k not in known_keys}

        record = {
            "id": alert_id, "title": title, "body": body,
            "urgency": urgency_val, "urgency_label": urgency,
            "icon": icon, "detail": detail,
            "fields": fields, "extra": extra,
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "ts_sort": datetime.now().strftime("%Y%m%d%H%M%S"),
            "raw": payload,
            "read": 0,
        }
        # 持久化到 SQLite
        _db_save(record)
        _db_trim()
        _refresh_cache()

        # 只有重要(2)和加急(3)级别弹桌面通知，一般/普通只记录不通知
        notified = False
        if urgency_val >= NOTIFY_THRESHOLD:
            GLib.idle_add(_show_notification, alert_id, title, body, urgency_val, icon)
            notified = True

        self._json(200, {"ok": True, "id": alert_id, "title": title,
                         "urgency": urgency,
                         "notified": notified,
                         "detail_url": f"http://127.0.0.1:{PORT}/alert/{alert_id}"})

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))


# ---------- 启动 ----------

def main():
    global _glib_loop
    Notify.init(APP)

    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True,
                     name="http").start()
    sys.stderr.write(f"aiping listening on {HOST}:{PORT} "
                     f"(auth={'on' if TOKEN else 'off'}, app='{APP}')\n")

    _glib_loop = GLib.MainLoop()
    try:
        _glib_loop.run()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        Notify.uninit()


if __name__ == "__main__":
    main()
