#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抖音「喜欢视频」批量下载 —— 可视化 Web 面板（仅标准库，无外部依赖）

从 dysync.net 抽离的「喜欢视频批量下载」+ 一个仿原版的轻量 Web UI。
适合部署在 飞牛OS / 群晖 / 任何支持 Docker 的 NAS 上，视频可直接下载到 NAS 磁盘。

功能：
  - 仪表盘：运行状态、进度、本次统计
  - 视频列表：按作者/标题浏览已下载视频，支持搜索、播放（Range 流式）
  - 配置：sec_user_id / Cookie / 输出目录 / 编码 / 限速 / 限量 等，保存到 config.json
  - 日志：实时查看运行日志
  - 开始 / 停止

运行：
  python webui.py --host 0.0.0.0 --port 8090 --out "D:/抖音/喜欢"
  然后浏览器打开 http://<机器IP>:8090

Docker（飞牛OS）：见同目录 Dockerfile / docker-compose.yml
"""

import argparse
import hashlib
import hmac
import json
import mimetypes
import os
import re
import sys
import threading
import time
import urllib.parse
from collections import deque
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 复用下载引擎（同目录）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dy_favorite_dl as core  # noqa: E402

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGIN_SALT = "dysync-liked-dl"

DEFAULT_CONFIG = {
    "sec_user_id": "",
    "cookie": "",
    "out": os.environ.get("OUT_DIR", os.path.join(BASE_DIR, "data")),
    "count": 18,
    "codec": 264,
    "min_interval": 1.0,
    "max_interval": 4.0,
    "page_delay_min": 2.0,
    "page_delay_max": 9.0,
    "download_timeout": 30.0,
    "max_videos": 0,
    "max_pages": 0,
    "images": False,
    "no_cover": False,
    "no_avatar": False,
    "dry_run": False,
    "caption_untitled": True,
    "pretty_names": True,
    "password": "",
    # 定时任务：schedule_enabled 总开关；schedule 为任务列表
    # 每条: {"id","start":"HH:MM","stop":"HH:MM","days":[0..6=周一..周日],"enabled":bool}
    "schedule_enabled": False,
    "schedule": [],
}

# ---------------------------------------------------------------------------
# 运行态 / 配置
# ---------------------------------------------------------------------------
RUN_LOCK = threading.Lock()
RUN = {
    "running": False,
    "stage": "idle",
    "page": 0,
    "cursor": "0",
    "stats": {"ok": 0, "dup": 0, "fail": 0, "skip": 0, "image": 0, "dry": 0},
    "started_at": None,
    "finished_at": None,
    "last_error": "",
    # 当前文件的下载进度（用于网页实时速度展示）
    "file": "",
    "got": 0,
    "total": 0,
    "speed": 0.0,
    "pct": 0.0,
    "phase": "",
    "host": "",
    "attempt": 0,
    "retries": 0,
    "updated_at": 0.0,
}
import itertools

LOGS = deque(maxlen=5000)          # 元素为 (seq, line)
LOG_SEQ = itertools.count(1)       # 全局单调递增序号（永不复位）
LOG_LOCK = threading.Lock()
LOG_FILE = None                    # 日志持久化文件路径（main 中设置）
THREAD = None
STOP_EVENT = threading.Event()


def add_log(line):
    """写入日志缓冲并持久化到文件，返回该行的全局序号。"""
    with LOG_LOCK:
        seq = next(LOG_SEQ)
        LOGS.append((seq, line))
        if LOG_FILE:
            try:
                with open(LOG_FILE, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                pass
    return seq


def load_log_tail(path, limit=1000):
    """启动时把上次的日志（尾部 limit 行）读回内存，保证重启后历史不丢。"""
    if not path or not os.path.isfile(path):
        return 0
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.read().splitlines()[-limit:]
    except Exception:
        return 0
    with LOG_LOCK:
        for ln in lines:
            LOGS.append((next(LOG_SEQ), ln))
    return len(lines)


CFG_PATH = None
CONFIG = dict(DEFAULT_CONFIG)
WARNINGS = []            # 启动期/运行期的环境问题（目录不可写等），展示在界面顶部


def add_warning(msg):
    """记录一条环境告警（去重）。"""
    if msg and msg not in WARNINGS:
        WARNINGS.append(msg)
    return msg


def mount_out():
    """容器映射目录（Docker 里为 OUT_DIR，即 volumes 左边那个宿主机目录）。"""
    return (os.environ.get("OUT_DIR") or "").strip()


def _norm_path(p):
    try:
        return os.path.normcase(os.path.abspath((p or "").strip().rstrip("/\\")))
    except Exception:  # noqa
        return (p or "").strip()


def dyn_warnings():
    """每次刷新都重算的告警（例如输出目录与容器挂载点不一致）。"""
    out = []
    mo = mount_out()
    cur = (CONFIG.get("out") or "").strip()
    if mo and cur and _norm_path(cur) != _norm_path(mo):
        out.append(f"输出目录当前填的是「{cur}」，但在容器里必须填「{mo}」——只有 {mo} 才是映射到"
                   f"飞牛磁盘的目录。填宿主机路径的话，文件会存在容器内部，飞牛文件管理器里看不到。"
                   f"请把「输出目录」改回 {mo}")
    return out


def ensure_writable(path, what="目录"):
    """确保目录存在且可写；失败时返回错误信息而不是抛异常（避免容器重启循环）。"""
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".wtest")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return None
    except Exception as e:  # noqa
        return f"{what}不可写：{path}（{e.__class__.__name__}: {e}）"


def log_sink(level, line):
    add_log(line)


def progress_cb(p):
    with RUN_LOCK:
        stage = p.get("stage")
        if stage:
            RUN["stage"] = stage
        for key in ("page", "cursor", "stats"):
            if key in p:
                RUN[key] = p[key]
        if stage == "download" or "got" in p:
            # 逐文件下载进度：字节数 / 总大小 / 实时速度 / 当前阶段与主机
            RUN["file"] = p.get("file", RUN["file"])
            RUN["got"] = int(p.get("got") or 0)
            RUN["total"] = int(p.get("total") or 0)
            RUN["speed"] = float(p.get("speed") or 0.0)
            RUN["pct"] = float(p.get("pct") or 0.0)
            RUN["phase"] = p.get("phase", "") or ""
            RUN["host"] = p.get("host", "") or ""
            RUN["attempt"] = int(p.get("attempt") or 0)
            RUN["retries"] = int(p.get("retries") or 0)
            RUN["updated_at"] = time.time()
        elif stage and stage != "download":
            # 进入/离开下载阶段时清空，避免进度条残留
            RUN["file"] = ""
            RUN["got"] = RUN["total"] = 0
            RUN["speed"] = RUN["pct"] = 0.0
            RUN["phase"] = ""
            RUN["host"] = ""
            RUN["attempt"] = RUN["retries"] = 0
            RUN["updated_at"] = time.time()


def load_config(path):
    global CONFIG, CFG_PATH
    CFG_PATH = path
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            merged = dict(DEFAULT_CONFIG)
            merged.update({k: v for k, v in data.items() if k in DEFAULT_CONFIG})
            CONFIG = merged
        except Exception as e:  # noqa
            add_log(f"[WARN] 读取配置失败：{e}")
    # 输出目录为空时，回填默认路径（<脚本目录>/data），让界面能看到具体位置
    if not (CONFIG.get("out") or "").strip():
        CONFIG["out"] = os.path.join(BASE_DIR, "data")
    return CONFIG


def save_config(cfg):
    global CONFIG
    merged = dict(DEFAULT_CONFIG)
    merged.update({k: v for k, v in cfg.items() if k in DEFAULT_CONFIG})
    # 定时设置不在「配置」表单里：若本次未提交，则沿用内存中的旧值，避免被表单保存冲掉
    if "schedule" not in cfg:
        merged["schedule"] = CONFIG.get("schedule", [])
    if "schedule_enabled" not in cfg:
        merged["schedule_enabled"] = CONFIG.get("schedule_enabled", False)
    merged["schedule"] = normalize_schedule(merged.get("schedule"))
    CONFIG = merged
    os.makedirs(os.path.dirname(os.path.abspath(CFG_PATH)) or ".", exist_ok=True)
    with open(CFG_PATH, "w", encoding="utf-8") as f:
        json.dump(CONFIG, f, ensure_ascii=False, indent=2)
    _SCAN_CACHE["data"] = None      # 配置变更后，下次列表重新扫描（如"空标题文案"开关立即生效）
    return CONFIG


def out_dir():
    return os.path.abspath(CONFIG.get("out") or os.path.join(BASE_DIR, "data"))


def excluded_count():
    """「不再下载」名单里的条目数（历史遗留，当前删除已不再写入）。"""
    p = os.path.join(out_dir(), ".dystate", "excluded_ids.txt")
    if not os.path.isfile(p):
        return 0
    try:
        with open(p, "r", encoding="utf-8") as f:
            return len([x for x in f.read().split() if x])
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# 视频扫描（带 5 秒缓存）
# ---------------------------------------------------------------------------
_SCAN_CACHE = {"t": 0, "data": None}
VIDEO_EXTS = (".mp4", ".m4a", ".mp3", ".mov", ".flv")
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")


def _rel_meta(rel, base):
    """从相对路径取出 (作者原始名, 作者显示名, 标题目录名)。路径形如 作者/标题/文件。"""
    parts = rel.split(os.sep)
    author_raw = parts[0] if len(parts) >= 1 else ""
    author_disp = re.sub(r"\d{8,}$", "", author_raw) or author_raw
    title = parts[1] if len(parts) >= 3 else ""
    return author_raw, author_disp, title


def _title_display(title, seed):
    untitled = (not title) or re.fullmatch(r"\d+", title) is not None
    if untitled and CONFIG.get("caption_untitled", True):
        return core.generate_caption(seed), untitled
    return (title or seed), untitled


def scan_videos(force=False):
    now = time.time()
    if not force and _SCAN_CACHE["data"] is not None and now - _SCAN_CACHE["t"] < 5:
        return _SCAN_CACHE["data"]

    base = out_dir()
    items = []
    image_groups = {}   # 目录 -> [图片文件路径]（一个目录=一条图文）
    if os.path.isdir(base):
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d != ".dystate"]
            is_author_dir = os.path.basename(root) == "author"
            for fn in files:
                ext = os.path.splitext(fn.lower())[1]
                fp = os.path.join(root, fn)
                if ext in VIDEO_EXTS:
                    rel = os.path.relpath(fp, base)
                    a_raw, a_disp, title = _rel_meta(rel, base)
                    stem_name = core.read_marker(root) or os.path.splitext(fn)[0]
                    title_disp, untitled = _title_display(title, stem_name)
                    try:
                        st = os.stat(fp)
                    except OSError:
                        continue
                    relu = rel.replace(os.sep, "/")
                    items.append({
                        "type": "video", "rel": relu, "files": [relu], "count": 1,
                        "author": a_disp, "author_raw": a_raw,
                        "title": title, "title_disp": title_disp, "untitled": untitled,
                        "name": fn, "size": st.st_size, "mtime": int(st.st_mtime),
                        "has_poster": os.path.isfile(os.path.splitext(fp)[0] + "-poster.jpg"),
                    })
                elif ext in IMAGE_EXTS and not is_author_dir and not fn.endswith("-poster.jpg"):
                    image_groups.setdefault(root, []).append(fp)

    # 图文：一个目录聚合为一条（相册）
    for root, fps in image_groups.items():
        fps.sort()
        first = fps[0]
        rel = os.path.relpath(first, base)
        a_raw, a_disp, title = _rel_meta(rel, base)
        aid = core.read_marker(root) or re.sub(r"_\d+$", "", os.path.splitext(os.path.basename(first))[0])
        title_disp, untitled = _title_display(title, aid)
        try:
            total = sum(os.path.getsize(p) for p in fps)
            mtime = max(int(os.path.getmtime(p)) for p in fps)
        except OSError:
            continue
        items.append({
            "type": "image",
            "rel": rel.replace(os.sep, "/"),
            "files": [os.path.relpath(p, base).replace(os.sep, "/") for p in fps],
            "count": len(fps),
            "author": a_disp, "author_raw": a_raw,
            "title": title, "title_disp": title_disp, "untitled": untitled,
            "name": os.path.basename(first), "size": total, "mtime": mtime,
            "has_poster": False,
        })

    items.sort(key=lambda x: x["mtime"], reverse=True)
    _SCAN_CACHE["data"] = items
    _SCAN_CACHE["t"] = now
    return items


def compute_stats():
    items = scan_videos()
    total_size = sum(i["size"] for i in items)
    by_author = {}
    for i in items:
        by_author[i["author"]] = by_author.get(i["author"], 0) + 1
    authors = sorted(by_author.items(), key=lambda kv: kv[1], reverse=True)[:30]
    today = datetime.now().strftime("%Y-%m-%d")
    today_count = sum(1 for i in items
                      if datetime.fromtimestamp(i["mtime"]).strftime("%Y-%m-%d") == today)
    return {
        "total": len(items),
        "videos": sum(1 for i in items if i.get("type") == "video"),
        "images": sum(1 for i in items if i.get("type") == "image"),
        "total_size": total_size,
        "today": today_count,
        "authors": [{"name": n, "count": c} for n, c in authors],
        "out_dir": out_dir(),
    }


# ---------------------------------------------------------------------------
# 下载控制
# ---------------------------------------------------------------------------
def build_args(cfg):
    class A:
        pass
    a = A()
    a.sec_user_id = (cfg.get("sec_user_id") or "").strip()
    a.cookie = (cfg.get("cookie") or "").strip()
    a.out = out_dir()
    a.count = int(cfg.get("count") or 18)
    a.codec = int(cfg.get("codec") or 264)
    a.min_interval = float(cfg.get("min_interval", 1))
    a.max_interval = float(cfg.get("max_interval", 4))
    a.page_delay_min = float(cfg.get("page_delay_min", 2))
    a.page_delay_max = float(cfg.get("page_delay_max", 9))
    a.download_timeout = float(cfg.get("download_timeout", 30))
    a.max_videos = int(cfg.get("max_videos") or 0)
    a.max_pages = int(cfg.get("max_pages") or 0)
    a.images = bool(cfg.get("images"))
    a.no_cover = bool(cfg.get("no_cover"))
    a.no_avatar = bool(cfg.get("no_avatar"))
    a.dry_run = bool(cfg.get("dry_run"))
    a.no_resume = False
    a.overwrite = False
    a.verbose = False
    a.pretty_names = bool(cfg.get("pretty_names", True))
    return a


def _worker(cfg):
    args = build_args(cfg)
    try:
        core.run(args, stop_event=STOP_EVENT, progress_cb=progress_cb, log_sink=log_sink)
    except Exception as e:  # noqa
        add_log(f"[ERROR] 运行异常：{e}")
        with RUN_LOCK:
            RUN["last_error"] = str(e)
    finally:
        with RUN_LOCK:
            RUN["running"] = False
            RUN["stage"] = "done" if not RUN["last_error"] else "error"
            RUN["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            RUN["file"] = ""
            RUN["got"] = RUN["total"] = 0
            RUN["speed"] = RUN["pct"] = 0.0
        scan_videos(force=True)


def start_run(cfg):
    global THREAD
    if RUN["running"]:
        return False, "任务正在运行中"
    if not (cfg.get("sec_user_id") or "").strip():
        return False, "请先填写 sec_user_id（必填）"
    if not (cfg.get("cookie") or "").strip():
        return False, "请先填写 Cookie（必填）"
    if not (cfg.get("out") or "").strip():
        return False, "请先填写输出目录（必填）"
    save_config(cfg)
    STOP_EVENT.clear()
    # 保留历史日志：不清空，只追加分隔（重启后也会从文件读回）
    add_log("")
    add_log(f"[INFO] ===== 开始同步 {datetime.now():%Y-%m-%d %H:%M:%S} =====")
    with RUN_LOCK:
        RUN.update({"running": True, "stage": "starting", "page": 0, "cursor": "0",
                    "stats": {"ok": 0, "dup": 0, "fail": 0, "skip": 0, "image": 0, "dry": 0},
                    "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "finished_at": None, "last_error": "",
                    "file": "", "got": 0, "total": 0, "speed": 0.0, "pct": 0.0,
                    "phase": "", "host": "", "attempt": 0, "retries": 0,
                    "updated_at": time.time()})
    THREAD = threading.Thread(target=_worker, args=(dict(cfg),), daemon=True)
    THREAD.start()
    return True, "已开始"


def stop_run():
    if not RUN["running"]:
        return False, "当前没有运行中的任务"
    STOP_EVENT.set()
    return True, "已发送停止信号"


# ---------------------------------------------------------------------------
# 定时任务（到点自动开始 / 到点自动停止）
# ---------------------------------------------------------------------------
WEEK_ZH = ["一", "二", "三", "四", "五", "六", "日"]
SCHED_STATE = {}          # id -> {"date": "YYYY-MM-DD", "started": bool, "stopped": bool}
SCHED_LOCK = threading.Lock()


def hhmm_to_min(s):
    try:
        h, m = str(s).split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return 0


def _norm_hhmm(v, default="00:00"):
    s = str(v or "").strip()
    if re.fullmatch(r"\d{1,2}:\d{1,2}", s):
        h, m = (int(x) for x in s.split(":"))
        if 0 <= h <= 23 and 0 <= m <= 59:
            return f"{h:02d}:{m:02d}"
    return default


def normalize_schedule(raw):
    """规范化定时任务列表（容错：脏数据不会让服务崩溃）。"""
    out = []
    for t in (raw or []):
        if not isinstance(t, dict):
            continue
        start = _norm_hhmm(t.get("start"), "02:00")
        stop = _norm_hhmm(t.get("stop"), "06:00")
        if start == stop:
            continue                                  # 起止相同视为无效，忽略
        days = t.get("days")
        if not isinstance(days, list) or not days:
            days = list(range(7))
        clean = []
        for d in days:
            try:
                d = int(d)
            except (TypeError, ValueError):
                continue
            if 0 <= d <= 6 and d not in clean:
                clean.append(d)
        if not clean:
            clean = list(range(7))
        out.append({
            "id": str(t.get("id") or f"{start}-{stop}-{''.join(map(str, sorted(clean)))}"),
            "start": start, "stop": stop,
            "days": sorted(clean),
            "enabled": bool(t.get("enabled", True)),
        })
    return out


def sched_window(start, stop, cur_min):
    """返回 (是否处于开始窗口, 是否已越过结束点)。支持跨天（如 23:00 -> 02:00）。"""
    s, e = hhmm_to_min(start), hhmm_to_min(stop)
    if e > s:
        return (s <= cur_min < e, cur_min >= e)
    return (cur_min >= s or cur_min < e, e <= cur_min < s)


def sched_next_run(now=None):
    """计算下一次「自动开始」时间，供界面展示。"""
    cfg = CONFIG
    if not cfg.get("schedule_enabled"):
        return None
    tasks = [t for t in (cfg.get("schedule") or []) if t.get("enabled", True)]
    if not tasks:
        return None
    now = now or datetime.now()
    best = None
    for t in tasks:
        s = hhmm_to_min(t["start"])
        for add in range(0, 8):
            d = now + timedelta(days=add)
            if d.weekday() not in (t.get("days") or list(range(7))):
                continue
            cand = d.replace(hour=s // 60, minute=s % 60, second=0, microsecond=0)
            if cand > now:                      # 今天该时间已过就顺延到下一天
                if best is None or cand < best[0]:
                    best = (cand, t)
                break
    if not best:
        return None
    return {"at": best[0].strftime("%Y-%m-%d %H:%M"), "start": best[1]["start"], "stop": best[1]["stop"]}


def sched_tick(now=None):
    """执行一次定时检查：到开始时间自动开始，到结束时间自动停止。"""
    cfg = CONFIG
    if not cfg.get("schedule_enabled"):
        return
    tasks = cfg.get("schedule") or []
    if not tasks:
        return
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    cur = now.hour * 60 + now.minute
    wd = now.weekday()                                  # 0=周一
    for t in tasks:
        if not t.get("enabled", True):
            continue
        if wd not in (t.get("days") or list(range(7))):
            continue
        key = str(t.get("id"))
        with SCHED_LOCK:
            st = SCHED_STATE.setdefault(key, {})
            if st.get("date") != today:
                st.clear()
                st["date"] = today
            in_start, past_stop = sched_window(t["start"], t["stop"], cur)
            if in_start and not st.get("started"):
                with RUN_LOCK:
                    running = RUN["running"]
                if running:
                    st["started"] = True                # 已在跑，本次只标记，避免重复触发
                else:
                    ok, msg = start_run(dict(cfg))
                    st["started"] = True
                    add_log(f"[INFO] ⏰ 定时任务 {t['start']} —— {msg}" if ok
                            else f"[WARN] ⏰ 定时任务 {t['start']} 启动失败：{msg}")
            if past_stop and st.get("started") and not st.get("stopped"):
                st["stopped"] = True
                with RUN_LOCK:
                    running = RUN["running"]
                if running:
                    stop_run()
                    add_log(f"[INFO] ⏰ 定时任务到达结束时间 {t['stop']}，已发送停止信号")


def scheduler_loop():
    while True:
        try:
            sched_tick()
        except Exception as e:  # noqa
            add_log(f"[WARN] 定时任务检查异常：{e}")
        time.sleep(15)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _parse_range(header, size):
    """解析单段 Range。返回 (start,end)；None=无法满足(应回 416)；False=应忽略(按整文件返回)。"""
    if not header or not header.startswith("bytes="):
        return False
    spec = header.split("=", 1)[1].split(",")[0].strip()   # 多段只取第一段
    s_str, _, e_str = spec.partition("-")
    try:
        if s_str == "":                     # bytes=-N 末尾 N 字节
            n = int(e_str)
            if n <= 0:
                return None
            start, end = max(0, size - n), size - 1
        else:
            start = int(s_str)
            end = int(e_str) if e_str else size - 1
    except ValueError:
        return False                        # 非法 Range：忽略，按整体返回
    if start < 0 or start >= size:
        return None
    end = min(end, size - 1)
    if start > end:
        return None
    return (start, end)


def _auth_token(pw):
    return hashlib.sha256((LOGIN_SALT + (pw or "")).encode("utf-8")).hexdigest()


def need_auth():
    return bool((CONFIG.get("password") or "").strip())


class Handler(BaseHTTPRequestHandler):
    server_version = "dyLikedDL/1.0"

    def log_message(self, *a):
        pass

    # ---- helpers ----
    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, obj, code=200, headers=None):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8", headers)

    def _body_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n < 0 or n > 1_000_000:      # 防止超大请求体
                return {}
            raw = self.rfile.read(n) if n else b"{}"
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return {}

    def _authed(self):
        if not need_auth():
            return True
        ck = self.headers.get("Cookie") or ""
        for part in ck.split(";"):
            if part.strip().startswith("dyauth="):
                got = part.strip().split("=", 1)[1]
                return hmac.compare_digest(got, _auth_token(CONFIG.get("password")))
        return False

    def _guard(self):
        if self._authed():
            return True
        self._json({"ok": False, "need_login": True, "error": "需要登录"}, 401)
        return False

    def _safe_path(self, rel):
        """把相对路径解析到 out_dir 内，防止路径穿越（含符号链接逃逸）。"""
        base = os.path.realpath(out_dir())
        rel = urllib.parse.unquote(rel or "")
        target = os.path.realpath(os.path.join(base, rel))
        if target != base and not target.startswith(base + os.sep):
            return None
        if not os.path.isfile(target):
            return None
        return target

    # ---- routes ----
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/":
            if not self._authed():
                return self._send(200, LOGIN_HTML, "text/html; charset=utf-8")
            return self._send(200, INDEX_HTML, "text/html; charset=utf-8")

        if path == "/api/state":
            if not self._guard():
                return
            with RUN_LOCK:
                st = dict(RUN)
            st["stats_directory"] = compute_stats()
            st["need_login"] = need_auth()
            st["version"] = "1.0"
            st["schedule_enabled"] = bool(CONFIG.get("schedule_enabled"))
            st["schedule_count"] = len(CONFIG.get("schedule") or [])
            st["schedule_next"] = sched_next_run()
            st["excluded_count"] = excluded_count()
            st["warnings"] = list(WARNINGS) + dyn_warnings()
            st["mount_out"] = mount_out()
            return self._json({"ok": True, "state": st})

        if path == "/api/config":
            if not self._guard():
                return
            return self._json({"ok": True, "config": CONFIG,
                               "config_path": os.path.abspath(CFG_PATH) if CFG_PATH else "",
                               "out_resolved": out_dir(),
                               "mount_out": mount_out()})

        if path == "/api/schedule":
            if not self._guard():
                return
            return self._json({"ok": True,
                               "schedule": CONFIG.get("schedule") or [],
                               "schedule_enabled": bool(CONFIG.get("schedule_enabled")),
                               "next": sched_next_run(),
                               "week": WEEK_ZH})

        if path == "/api/logs":
            if not self._guard():
                return
            try:
                since = int(qs.get("since", ["0"])[0])
            except ValueError:
                since = 0
            with LOG_LOCK:
                items = list(LOGS)
            lines = [ln for (sq, ln) in items if sq > since]
            last_seq = items[-1][0] if items else since
            return self._json({"ok": True, "last_seq": last_seq, "lines": lines, "total": len(items)})

        if path == "/api/videos":
            if not self._guard():
                return
            q = (qs.get("q", [""])[0] or "").lower()
            typ = (qs.get("type", ["all"])[0] or "all").lower()
            sort = (qs.get("sort", ["time_desc"])[0] or "time_desc").lower()
            try:
                page = max(1, int(qs.get("page", ["1"])[0]))
                size = min(200, max(1, int(qs.get("size", ["60"])[0])))
            except ValueError:
                page, size = 1, 60
            items = scan_videos()
            if typ in ("video", "image"):
                items = [i for i in items if i.get("type") == typ]
            if q:
                items = [i for i in items
                         if q in i["author"].lower() or q in i.get("author_raw", "").lower()
                         or q in i["title"].lower() or q in i.get("title_disp", "").lower()
                         or q in i["name"].lower()]
            sorters = {
                "time_desc": (lambda x: x["mtime"], True),
                "time_asc": (lambda x: x["mtime"], False),
                "size_desc": (lambda x: x["size"], True),
                "size_asc": (lambda x: x["size"], False),
                "name": (lambda x: (x.get("title_disp") or "").lower(), False),
            }
            keyf, rev = sorters.get(sort, sorters["time_desc"])
            items = sorted(items, key=keyf, reverse=rev)
            total = len(items)
            start = (page - 1) * size
            return self._json({"ok": True, "total": total, "page": page, "size": size,
                               "type": typ, "sort": sort,
                               "items": items[start:start + size]})

        if path == "/api/stats":
            if not self._guard():
                return
            return self._json({"ok": True, "stats": compute_stats()})

        if path == "/api/rescan":
            if not self._guard():
                return
            scan_videos(force=True)
            return self._json({"ok": True, "stats": compute_stats()})

        if path in ("/play", "/file"):
            if not self._authed():
                return self._send(403, "forbidden", "text/plain; charset=utf-8")
            return self._serve_file(qs.get("p", [""])[0])

        self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        data = self._body_json()

        if path == "/api/login":
            pw = str(data.get("password") or "")
            if hmac.compare_digest(_auth_token(pw), _auth_token(CONFIG.get("password"))):
                token = _auth_token(CONFIG.get("password"))
                return self._json({"ok": True}, headers={
                    "Set-Cookie": f"dyauth={token}; Path=/; Max-Age=2592000; HttpOnly; SameSite=Lax"})
            return self._json({"ok": False, "error": "密码错误"}, 401)

        if path == "/api/logout":
            return self._json({"ok": True}, headers={
                "Set-Cookie": "dyauth=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"})

        if not self._guard():
            return

        if path == "/api/config":
            try:
                save_config(data.get("config") or {})
                return self._json({"ok": True, "config": CONFIG})
            except Exception as e:  # noqa
                return self._json({"ok": False, "error": str(e)}, 400)

        if path == "/api/schedule":
            cfg = dict(CONFIG)
            cfg["schedule"] = normalize_schedule(data.get("schedule"))
            cfg["schedule_enabled"] = bool(data.get("schedule_enabled"))
            save_config(cfg)
            add_log(f"[INFO] 定时任务已更新：{'启用' if cfg['schedule_enabled'] else '停用'}，"
                    f"共 {len(cfg['schedule'])} 条")
            return self._json({"ok": True, "schedule": CONFIG["schedule"],
                               "schedule_enabled": CONFIG["schedule_enabled"],
                               "next": sched_next_run()})

        if path == "/api/start":
            cfg = data.get("config") or dict(CONFIG)
            # 注意：保存发生在 start_run() 内部、且只在校验通过之后，避免把非法配置落盘
            ok, msg = start_run(cfg)
            return self._json({"ok": ok, "message": msg}, 200 if ok else 400)

        if path == "/api/stop":
            ok, msg = stop_run()
            return self._json({"ok": ok, "message": msg}, 200 if ok else 400)

        if path == "/api/rescan":
            scan_videos(force=True)
            return self._json({"ok": True, "stats": compute_stats()})

        if path == "/api/delete":
            files = data.get("files") or ([data.get("path")] if data.get("path") else [])
            return self._delete_video(files)

        if path == "/api/clear-exclusions":
            exc = os.path.join(out_dir(), ".dystate", "excluded_ids.txt")
            n = 0
            if os.path.isfile(exc):
                try:
                    with open(exc, "r", encoding="utf-8") as f:
                        n = len([x for x in f.read().split() if x])
                    with open(exc, "w", encoding="utf-8") as f:
                        f.write("")
                except OSError:
                    n = 0
            add_log(f"[INFO] 已清空「不再下载」名单（{n} 条），这些作品下次同步会重新下载")
            return self._json({"ok": True, "cleared": n})

        self._send(404, "not found", "text/plain; charset=utf-8")

    # ---- 删除视频 / 图文（只删本地文件，删完可重新下载） ----
    def _delete_video(self, rels):
        """删除本地文件（视频/图片 + 封面）并清理空目录；
        同时移除该作品的「不再下载」记录，使其之后能重新下载。"""
        base = out_dir()
        exc_path = os.path.join(base, ".dystate", "excluded_ids.txt")
        removed, folders, aid = [], set(), None
        for rel in (rels or []):
            fp = self._safe_path(rel)            # 只允许 out_dir 内的文件
            if not fp:
                continue
            folder = os.path.dirname(fp)
            folders.add(folder)
            if aid is None:
                # 以 .dyid 为准（文件名现在是标题，不再是作品 ID）
                aid = core.read_marker(folder) or re.sub(
                    r"_\d+$", "", os.path.splitext(os.path.basename(fp))[0])
            try:
                os.remove(fp)
                removed.append(os.path.basename(fp))
            except OSError:
                continue
            poster = os.path.splitext(fp)[0] + "-poster.jpg"
            if os.path.isfile(poster):
                try:
                    os.remove(poster)
                    removed.append(os.path.basename(poster))
                except OSError:
                    pass
        if not removed:
            return self._json({"ok": False, "error": "文件不存在或路径非法"}, 404)
        # 清掉「不再下载」记录 -> 之后同步会重新下载这个作品
        if aid and os.path.isfile(exc_path):
            try:
                with open(exc_path, "r", encoding="utf-8") as f:
                    keep = [ln.strip() for ln in f if ln.strip() and ln.strip() != aid]
                with open(exc_path, "w", encoding="utf-8") as f:
                    if keep:
                        f.write("\n".join(keep) + "\n")
            except OSError:
                pass
        # 清理空目录（含只剩 .dyid 标记的情况）
        for folder in folders:
            try:
                leftovers = os.listdir(folder)
                if leftovers and set(leftovers) <= {".dyid"}:
                    os.remove(os.path.join(folder, ".dyid"))
                    leftovers = []
                if not leftovers:
                    os.rmdir(folder)
            except OSError:
                pass
        # 作者目录若只剩 .author 归属标记（或完全空）也一并清理
        for parent in {os.path.dirname(f) for f in folders}:
            try:
                if not [x for x in os.listdir(parent) if x != ".author"]:
                    am = os.path.join(parent, ".author")
                    if os.path.isfile(am):
                        os.remove(am)
                    os.rmdir(parent)
            except OSError:
                pass
        scan_videos(force=True)
        return self._json({"ok": True, "removed": removed})

    # ---- 静态文件 / 视频流 ----
    def _serve_file(self, rel):
        fp = self._safe_path(rel)
        if not fp:
            return self._send(404, "not found", "text/plain; charset=utf-8")
        ctype = mimetypes.guess_type(fp)[0] or "application/octet-stream"
        try:
            size = os.path.getsize(fp)
        except OSError:
            return self._send(404, "not found", "text/plain; charset=utf-8")
        rng = _parse_range(self.headers.get("Range"), size)
        try:
            with open(fp, "rb") as f:
                if rng is None:                      # 合法 Range 但无法满足
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if rng is False:                     # 无 Range 或非法 Range -> 整体返回
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Length", str(size))
                    self.end_headers()
                    while True:
                        chunk = f.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                    return
                start, end = rng
                length = end - start + 1
                self.send_response(206)
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Content-Length", str(length))
                self.end_headers()
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa
            add_log(f"[WARN] 读取文件失败：{os.path.basename(fp)}：{e}")


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------
LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>登录 · 抖音喜欢下载</title>
<style>
:root{--bg:#0b0f14;--panel:#131c26;--line:#23303f;--txt:#e6edf3;--dim:#8b9bb0;--acc:#3b82f6;--input:#0d131a;--ring:rgba(59,130,246,.20)}
body.light{--bg:#eef2f7;--panel:#ffffff;--line:#e3e9f0;--txt:#111827;--dim:#5b6b7f;--acc:#2563eb;--input:#ffffff;--ring:rgba(37,99,235,.18)}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
  background:radial-gradient(900px 520px at 50% -12%,rgba(59,130,246,.16),transparent 60%),var(--bg);
  color:var(--txt);font:14px/1.55 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.box{width:352px;max-width:92vw;background:var(--panel);border:1px solid var(--line);border-radius:18px;
  padding:34px 30px;box-shadow:0 24px 60px rgba(0,0,0,.42)}
.logo{font-size:32px;line-height:1}
h1{margin:12px 0 4px;font-size:18px;font-weight:600;letter-spacing:.2px}
p{margin:0 0 22px;color:var(--dim);font-size:12.5px}
input{width:100%;padding:12px 14px;border-radius:11px;border:1px solid var(--line);background:var(--input);
  color:var(--txt);font-size:14px;outline:none;transition:.15s}
input:focus{border-color:var(--acc);box-shadow:0 0 0 3px var(--ring)}
button{width:100%;margin-top:14px;padding:12px;border:0;border-radius:11px;background:var(--acc);color:#fff;
  font-size:14px;font-weight:500;cursor:pointer;transition:.15s}
button:hover{filter:brightness(1.08)}
button:disabled{opacity:.6;cursor:default}
.msg{color:#ef4444;font-size:12px;margin-top:10px;min-height:16px}
</style></head><body>
<div class="box">
  <div class="logo">🎬</div>
  <h1>抖音喜欢下载</h1>
  <p>请输入访问密码以继续</p>
  <input id="pw" type="password" placeholder="访问密码" autofocus onkeydown="if(event.key==='Enter')login()">
  <button onclick="login()">登 录</button>
  <div class="msg" id="msg"></div>
</div>
<script>
if(localStorage.getItem('dytheme')==='light')document.body.classList.add('light');
async function login(){
  var btn=document.querySelector('button'); btn.disabled=true;
  try{
    var r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({password:document.getElementById('pw').value})});
    var j=await r.json();
    if(j.ok){location.href='/';return;}
    document.getElementById('msg').textContent=j.error||'登录失败';
  }catch(e){document.getElementById('msg').textContent='网络错误，请重试';}
  btn.disabled=false;
}
</script></body></html>"""


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>抖音喜欢 · 下载面板</title>
<style>
:root{
  --bg:#0b0f14;--panel:#121b25;--panel2:#18232f;--line:#23303f;--line2:#2e3f52;
  --txt:#e6edf3;--dim:#8b9bb0;--dim2:#6c7d92;--input:#0d131a;--code:#0a0f15;
  --acc:#3b82f6;--acc2:#2f6fe0;--ok:#22c55e;--warn:#f59e0b;--err:#ef4444;--img:#a855f7;
  --radius:14px;--radius-sm:10px;--shadow:0 14px 36px rgba(0,0,0,.40);--ring:rgba(59,130,246,.22)
}
body.light{
  --bg:#eef2f7;--panel:#ffffff;--panel2:#f3f6fa;--line:#e3e9f0;--line2:#d3dce7;
  --txt:#101827;--dim:#5b6b7f;--dim2:#8895a6;--input:#ffffff;--code:#f4f7fb;
  --acc:#2563eb;--acc2:#1d4ed8;--ok:#16a34a;--warn:#d97706;--err:#dc2626;--img:#9333ea;
  --shadow:0 14px 36px rgba(16,24,40,.12);--ring:rgba(37,99,235,.18)
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;color:var(--txt);font:14px/1.55 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
  background:radial-gradient(1100px 520px at 100% -10%,rgba(59,130,246,.10),transparent 62%),var(--bg)}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:var(--line2);border-radius:8px;border:3px solid transparent;background-clip:content-box}
::-webkit-scrollbar-thumb:hover{background:var(--dim2);background-clip:content-box;border:3px solid transparent}
::-webkit-scrollbar-track{background:transparent}
.mut{color:var(--dim)}

/* ---------- 顶栏 ---------- */
.top{position:sticky;top:0;z-index:30;display:flex;align-items:center;gap:12px;flex-wrap:wrap;
  padding:12px 20px;background:var(--panel);border-bottom:1px solid var(--line);
  box-shadow:0 6px 18px rgba(0,0,0,.12)}
.brand{display:flex;align-items:center;gap:9px;font-size:15.5px;font-weight:600;letter-spacing:.2px}
.brand .logo{font-size:19px}
.pill{display:inline-flex;align-items:center;gap:7px;padding:5px 12px;border-radius:999px;
  background:var(--panel2);border:1px solid var(--line);color:var(--dim);font-size:12px;white-space:nowrap}
.dot{width:8px;height:8px;border-radius:50%;background:var(--dim2);display:inline-block}
.dot.run{background:var(--ok);box-shadow:0 0 0 3px rgba(34,197,94,.22);animation:pulse 1.5s infinite}
.dot.err{background:var(--err);box-shadow:0 0 0 3px rgba(239,68,68,.22)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.45}}
.grow{flex:1}

/* ---------- 按钮 ---------- */
.btn{padding:8px 15px;border:1px solid var(--line);border-radius:10px;background:var(--panel2);color:var(--txt);
  font-size:13px;cursor:pointer;transition:.15s;white-space:nowrap;font-family:inherit}
.btn:hover{border-color:var(--line2);filter:brightness(1.12)}
.btn:active{transform:translateY(1px)}
.btn:disabled{opacity:.42;cursor:not-allowed;transform:none}
.btn.primary{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:500}
.btn.primary:hover{background:var(--acc2)}
.btn.danger{background:transparent;border-color:var(--err);color:var(--err)}
.btn.danger:hover{background:var(--err);color:#fff;filter:none}
.btn.icon{padding:8px 11px}
.acts{white-space:nowrap}
.selcol{display:none}
table.selmode .selcol{display:table-cell}
.selcol input{width:16px;height:16px;accent-color:var(--err);cursor:pointer;margin:0}
.selhint{display:none;margin:-4px 0 10px;padding:8px 12px;border-radius:9px;background:var(--panel2);
  border:1px solid var(--line);color:var(--dim);font-size:12.5px}
.selhint.on{display:block}
.btn.sel-on{background:var(--err);border-color:var(--err);color:#fff}

/* ---------- 布局 ---------- */
.wrap{max-width:1280px;margin:0 auto;padding:18px 20px 40px}

/* ---------- 统计卡 ---------- */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));gap:12px;margin-bottom:16px}
.stat{position:relative;background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  padding:15px 17px;box-shadow:var(--shadow);overflow:hidden}
.stat::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--acc);opacity:.55}
.stat .k{color:var(--dim);font-size:12px}
.stat .v{font-size:25px;font-weight:700;margin-top:5px;letter-spacing:.3px}
.stat .s{color:var(--dim2);font-size:11.5px;margin-top:3px}
.prog{margin-bottom:14px;padding:12px 16px;background:var(--panel);border:1px solid var(--line);
  border-radius:var(--radius);box-shadow:var(--shadow)}
.prog-top{display:flex;align-items:baseline;gap:12px;margin-bottom:8px}
.prog-name{font-size:13px;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:56%}
.prog-meta{margin-left:auto;color:var(--dim);font-size:12px;white-space:nowrap;font-variant-numeric:tabular-nums}
.track{height:8px;border-radius:999px;background:var(--code);overflow:hidden;border:1px solid var(--line)}
.track i{display:block;height:100%;width:0;border-radius:999px;
  background:linear-gradient(90deg,var(--acc),var(--img));transition:width .35s ease}
.track.stripe i{width:100%;background:repeating-linear-gradient(90deg,var(--acc) 0 14px,transparent 14px 24px);
  background-size:38px 100%;animation:slide 1s linear infinite}
@keyframes slide{from{background-position:0 0}to{background-position:38px 0}}

/* ---------- 标签页 ---------- */
.tabs{display:inline-flex;gap:4px;margin-bottom:14px;padding:4px;border-radius:999px;
  background:var(--panel);border:1px solid var(--line)}
.tab{padding:8px 18px;border:0;border-radius:999px;background:transparent;color:var(--dim);
  cursor:pointer;font-size:13px;font-family:inherit;transition:.15s;white-space:nowrap}
.tab:hover{color:var(--txt)}
.tab.on{background:var(--acc);color:#fff;box-shadow:0 4px 14px var(--ring)}

/* ---------- 卡片 / 工具条 ---------- */
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:16px;box-shadow:var(--shadow)}
.toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:14px}
.toolbar .grow{flex:1}
.seg{display:inline-flex;background:var(--panel2);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.seg button{border:0;background:transparent;color:var(--dim);padding:8px 15px;cursor:pointer;font-size:13px;font-family:inherit;transition:.15s}
.seg button:hover{color:var(--txt)}
.seg button.on{background:var(--acc);color:#fff}

input,textarea,select{width:100%;padding:9px 12px;border-radius:10px;border:1px solid var(--line);
  background:var(--input);color:var(--txt);font-size:13px;font-family:inherit;outline:none;transition:.15s}
input:focus,textarea:focus,select:focus{border-color:var(--acc);box-shadow:0 0 0 3px var(--ring)}
textarea{min-height:92px;resize:vertical;font-family:ui-monospace,Consolas,monospace}
.toolbar input,.toolbar select{width:auto}
.toolbar .search{flex:1;min-width:170px;max-width:340px}
label.f{display:block;margin:0 0 6px;color:var(--dim);font-size:12px}
.req{color:var(--err)}

/* ---------- 表格 ---------- */
.tablewrap{overflow:auto;max-height:62vh;border:1px solid var(--line);border-radius:12px}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line);vertical-align:middle}
th{position:sticky;top:0;z-index:2;background:var(--panel2);color:var(--dim);font-weight:500;font-size:12px}
tbody tr:last-child td{border-bottom:0}
tbody tr{transition:background .12s}
tbody tr:hover{background:var(--panel2)}
td.small,th.small{font-size:12px;color:var(--dim)}
.thumb{width:46px;height:62px;object-fit:cover;border-radius:8px;background:var(--code);display:block}
.tag{display:inline-block;padding:2px 8px;border-radius:6px;background:var(--acc);color:#fff;
  font-size:11px;margin-right:8px;vertical-align:1px;white-space:nowrap}
.tag.img{background:var(--img)}
.tname{font-weight:500}
.empty{padding:56px 16px;text-align:center;color:var(--dim);line-height:2}
.empty .big{font-size:30px;display:block;margin-bottom:8px;opacity:.7}

/* ---------- 分页 ---------- */
.pager{display:flex;gap:12px;align-items:center;justify-content:center;margin-top:14px}

/* ---------- 日志 ---------- */
#logs{height:58vh;overflow:auto;background:var(--code);border:1px solid var(--line);border-radius:12px;
  padding:13px 15px;font:12px/1.7 ui-monospace,Consolas,monospace;white-space:pre-wrap;word-break:break-all}
#logs .WARN{color:var(--warn)}
#logs .ERROR{color:var(--err)}
.chk{display:inline-flex;align-items:center;gap:7px;font-size:13px;color:var(--txt);cursor:pointer;margin:0}
.chk input{width:auto;accent-color:var(--acc)}
.chips{display:inline-flex;gap:6px;flex-wrap:wrap}
.chip{width:30px;height:30px;display:inline-flex;align-items:center;justify-content:center;border-radius:8px;
  border:1px solid var(--line);background:var(--panel2);color:var(--dim);font-size:12px;cursor:pointer;
  user-select:none;transition:.15s}
.chip:hover{color:var(--txt);border-color:var(--line2)}
.chip.on{background:var(--acc);border-color:var(--acc);color:#fff}
.toolbar label.f{display:inline-flex;align-items:center;gap:6px;margin:0}
.toolbar input[type="time"]{width:118px}

/* ---------- 配置分组 ---------- */
.fieldset{border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:12px;background:var(--panel2)}
.fieldset>.ft{color:var(--dim);font-size:12px;font-weight:600;letter-spacing:.4px;margin-bottom:10px;text-transform:uppercase}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
.chks{display:flex;flex-wrap:wrap;gap:8px 22px}
.hint{background:var(--panel2);border:1px solid var(--line);border-left:3px solid var(--acc);border-radius:10px;
  padding:10px 13px;color:var(--dim);font-size:12px;margin-bottom:14px;word-break:break-all;line-height:1.7}
.warnbar{background:var(--panel);border:1px solid var(--err);border-left:4px solid var(--err);
  border-radius:12px;padding:12px 15px;margin-bottom:14px;box-shadow:var(--shadow);font-size:13px;line-height:1.9}
.warnbar b{color:var(--err)}
.fhint{font-size:11.5px;color:var(--dim);margin-top:5px;line-height:1.6}
.fhint.warn{color:var(--err)}
.actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:4px}

/* ---------- 弹窗 ---------- */
.modal{position:fixed;top:0;right:0;bottom:0;left:0;background:rgba(4,7,11,.72);display:none;
  align-items:center;justify-content:center;z-index:60;padding:20px}
.modal.on{display:flex;animation:fade .18s ease}
@keyframes fade{from{opacity:0}to{opacity:1}}
.modal-box{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:14px;
  box-shadow:0 30px 70px rgba(0,0,0,.55);max-width:92vw;max-height:90vh;overflow:auto}
.modal-box.wide{width:min(1000px,92vw)}
.modal-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px}
.modal-head b{font-size:14px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.modal video{display:block;max-width:88vw;max-height:78vh;background:#000;border-radius:12px}
.gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px}
.gallery img{width:100%;border-radius:10px;cursor:zoom-in;background:var(--code);transition:.15s}
.gallery img:hover{transform:scale(1.02)}

.toast{position:fixed;left:50%;bottom:26px;transform:translate(-50%,16px);background:var(--panel);
  border:1px solid var(--line2);border-radius:11px;padding:11px 20px;box-shadow:var(--shadow);
  opacity:0;transition:.25s;pointer-events:none;z-index:99;font-size:13px;max-width:86vw}
.toast.on{opacity:1;transform:translate(-50%,0)}

@media(max-width:720px){
  .grid2,.grid3{grid-template-columns:1fr}
  .hide-sm{display:none}
  .wrap{padding:14px 12px 32px}
  table{min-width:520px}
}
</style></head><body>
<header class="top">
  <div class="brand"><span class="logo">🎬</span><span>抖音喜欢下载</span></div>
  <span class="pill"><i class="dot" id="dot"></i><span id="stext">空闲</span></span>
  <span class="grow"></span>
  <button class="btn icon" id="btnTheme" onclick="toggleTheme()" title="切换光/暗主题">🌙</button>
  <button class="btn primary" id="btnStart" onclick="startRun()">▶ 开始下载</button>
  <button class="btn danger" id="btnStop" onclick="stopRun()" disabled>■ 停止</button>
</header>

<div class="wrap">
  <div class="warnbar" id="warnBar" style="display:none"></div>
  <div class="prog" id="progWrap" style="display:none">
    <div class="prog-top">
      <span class="prog-name" id="pfile"></span>
      <span class="prog-meta" id="pmeta"></span>
    </div>
    <div class="track" id="ptrack"><i id="pbar"></i></div>
  </div>
  <section class="stats">
    <div class="stat"><div class="k">已下载内容</div><div class="v" id="cTotal">–</div><div class="s" id="cSplit">视频 0 · 图文 0</div></div>
    <div class="stat"><div class="k">占用空间</div><div class="v" id="cSize">–</div><div class="s">磁盘占用</div></div>
    <div class="stat"><div class="k">今日新增</div><div class="v" id="cToday">–</div><div class="s">今日入库</div></div>
    <div class="stat"><div class="k">本次运行</div><div class="v" id="cRun">0 / 0 / 0</div><div class="s" id="cRunS">成功 / 跳过 / 失败</div></div>
  </section>

  <nav class="tabs">
    <button class="tab on" data-t="videos" onclick="showTab('videos')">📼 内容列表</button>
    <button class="tab" data-t="logs" onclick="showTab('logs')">📜 运行日志</button>
    <button class="tab" data-t="config" onclick="showTab('config')">⚙️ 配置</button>
    <button class="tab" data-t="sched" onclick="showTab('sched')">⏰ 定时任务</button>
  </nav>

  <section id="page-videos" class="card">
    <div class="toolbar">
      <div class="seg" id="typeSeg">
        <button data-v="all" class="on" onclick="setFilter('all')">全部</button>
        <button data-v="video" onclick="setFilter('video')">视频</button>
        <button data-v="image" onclick="setFilter('image')">图文</button>
      </div>
      <input id="q" class="search" placeholder="搜索 作者 / 标题 / 文件名" oninput="onQuery()">
      <select id="fSort" onchange="loadVideos(1)">
        <option value="time_desc">最新在前</option>
        <option value="time_asc">最早在前</option>
        <option value="size_desc">最大在前</option>
        <option value="size_asc">最小在前</option>
        <option value="name">按标题</option>
      </select>
      <select id="fSize" onchange="loadVideos(1)">
        <option>30</option><option selected>60</option><option>120</option><option>200</option>
      </select>
      <span class="grow"></span>
      <button class="btn" id="btnExcl" onclick="clearExclusions()" style="display:none"
        title="清空「不再下载」名单，让之前被忽略的作品可以重新下载">↺ 清空忽略名单</button>
      <button class="btn" id="btnDelCancel" onclick="cancelDelMode()" style="display:none">✕ 取消</button>
      <button class="btn selbtn" id="btnDelSel" onclick="toggleDelMode()">🗑 删除</button>
    </div>
    <div class="selhint" id="selHint">勾选封面左侧的框可多选；点右上角「删除」批量删除，按 <b>Esc</b> 退出</div>
    <div class="tablewrap">
      <table id="vtable"><thead><tr>
        <th class="selcol" style="width:40px"><input type="checkbox" id="selAllBox" onchange="selAllBox(this.checked)" title="全选本页"></th>
        <th style="width:64px">封面</th><th>标题</th>
        <th class="hide-sm" style="width:168px">作者</th>
        <th style="width:92px">大小</th>
        <th class="hide-sm" style="width:150px">时间</th>
        <th style="width:112px"></th>
      </tr></thead><tbody id="vbody"></tbody></table>
    </div>
    <div class="pager">
      <button class="btn" id="pgPrev" onclick="gotoPage(vpage-1)">← 上一页</button>
      <span class="mut" id="vinfo"></span>
      <button class="btn" id="pgNext" onclick="gotoPage(vpage+1)">下一页 →</button>
    </div>
  </section>

  <section id="page-logs" class="card" style="display:none">
    <div class="toolbar">
      <label class="chk"><input type="checkbox" id="autoScroll" checked> 自动滚动</label>
      <span class="grow"></span>
      <button class="btn" onclick="clearLogs()">清屏</button>
    </div>
    <div id="logs"></div>
  </section>

  <section id="page-config" class="card" style="display:none">
    <div class="hint" id="cfginfo"></div>

    <div class="fieldset">
      <div class="ft">必填项</div>
      <div class="grid2">
        <div><label class="f">sec_user_id <span class="req">*</span></label><input id="f_sec" placeholder="抖音用户 sec_user_id"></div>
        <div><label class="f">输出目录<span class="req">*</span></label><input id="f_out" oninput="renderOutHint()" placeholder="容器里请填 /app/data">
          <div class="fhint" id="outHint"></div></div>
      </div>
      <label class="f" style="margin-top:12px">Cookie（完整登录 Cookie）<span class="req">*</span></label>
      <textarea id="f_cookie" placeholder="必填，需包含 ttwid / sessionid 等字段"></textarea>
    </div>

    <div class="fieldset">
      <div class="ft">下载参数</div>
      <div class="grid3">
        <div><label class="f">每页条数</label><input id="f_count" type="number"></div>
        <div><label class="f">优先编码</label><select id="f_codec"><option value="264">H.264</option><option value="265">H.265</option></select></div>
        <div><label class="f">单次下载超时(秒)</label><input id="f_dlto" type="number" step="1"></div>
      </div>
    </div>

    <div class="fieldset">
      <div class="ft">内容选项</div>
      <div class="chks">
        <label class="chk"><input type="checkbox" id="f_images"> 保存图文作品（列表显示为「图文」）</label>
        <label class="chk"><input type="checkbox" id="f_caption"> 空标题自动生成文案</label>
      </div>
    </div>

    <div class="actions">
      <button class="btn primary" onclick="saveConfig(true)">💾 保存并开始</button>
      <button class="btn" onclick="saveConfig(false)">仅保存</button>
    </div>
  </section>

  <section id="page-sched" class="card" style="display:none">
    <div class="hint">
      到达「开始时间」自动开启下载，到达「结束时间」自动停止（使用上方「配置」里已保存的参数）。
      可添加多条，按星期重复；结束时间为次日凌晨时（如 23:00 → 02:00）按跨天处理。
      <br><span id="schedNext" class="mut"></span>
    </div>
    <div class="toolbar">
      <label class="chk"><input type="checkbox" id="schedOn"> 启用定时任务</label>
      <span class="grow"></span>
      <button class="btn primary" onclick="saveSchedule()">💾 保存定时设置</button>
    </div>
    <div class="tablewrap" style="max-height:44vh">
      <table><thead><tr>
        <th style="width:140px">开始时间</th><th style="width:140px">结束时间</th>
        <th>重复（周）</th><th style="width:100px">状态</th><th style="width:100px"></th>
      </tr></thead><tbody id="schedBody"></tbody></table>
    </div>
    <div class="fieldset" style="margin-top:14px">
      <div class="ft">新增任务</div>
      <div class="toolbar" style="margin:0">
        <label class="f">开始 <input type="time" id="nsStart" value="02:00"></label>
        <label class="f">结束 <input type="time" id="nsStop" value="06:00"></label>
        <span class="chips" id="nsDays"></span>
        <span class="grow"></span>
        <button class="btn" onclick="addTask()">＋ 添加任务</button>
      </div>
    </div>
  </section>
</div>

<div class="modal" id="player">
  <div class="modal-box">
    <div class="modal-head"><b id="playerTitle"></b>
      <button class="btn icon" onclick="closePlayer()">✕</button></div>
    <video id="playerVideo" controls autoplay playsinline></video>
  </div>
</div>
<div class="modal" id="gallery">
  <div class="modal-box wide">
    <div class="modal-head"><b id="galleryTitle"></b>
      <button class="btn icon" onclick="closeGallery()">✕</button></div>
    <div id="galleryGrid" class="gallery"></div>
  </div>
</div>
<div class="modal" id="delmodal">
  <div class="modal-box" style="max-width:400px">
    <div class="modal-head"><b id="delTitle">删除</b>
      <button class="btn icon" onclick="closeDel()">✕</button></div>
    <div class="mut" id="delDesc" style="line-height:1.8;margin-bottom:16px;word-break:break-all"></div>
    <div class="actions">
      <button class="btn danger" onclick="doDel()">删除</button>
      <button class="btn" onclick="closeDel()">取消</button>
    </div>
  </div>
</div>
<div class="toast" id="toast"></div>
<script>
let vpage=1, logSince=0, pageItems=[], vType='all', qTimer=null, CURCFG={};
function $(id){return document.getElementById(id);}
function toast(m){var t=$('toast');t.textContent=m;t.classList.add('on');
  clearTimeout(t._t);t._t=setTimeout(function(){t.classList.remove('on');},2400);}
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
function fmtSize(n){n=n||0;var u=['B','KB','MB','GB','TB'],i=0;
  while(n>=1024&&i<u.length-1){n/=1024;i++;}return (i?n.toFixed(1):n)+' '+u[i];}
function fmtTime(ts){if(!ts)return '–';var d=new Date(ts*1000),p=function(x){return String(x).padStart(2,'0');};
  return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())+' '+p(d.getHours())+':'+p(d.getMinutes());}
function fmtDur(s){s=Math.max(0,Math.round(s||0));if(s<60)return s+' 秒';
  var m=Math.floor(s/60),r=s%60;return m+' 分'+(r?(' '+r+' 秒'):'');}
async function jget(u){var r=await fetch(u,{cache:'no-store'});
  if(r.status===401){location.href='/';return {ok:false};}return r.json();}
async function jpost(u,b){var r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify(b||{})});
  if(r.status===401){location.href='/';return {ok:false};}return r.json();}

function showTab(t){
  ['videos','logs','config','sched'].forEach(function(x){
    var pg=$('page-'+x); if(pg)pg.style.display=(x===t)?'block':'none';
    var tb=document.querySelector('.tab[data-t="'+x+'"]'); if(tb)tb.classList.toggle('on',x===t);
  });
  if(t==='videos')loadVideos(vpage);
  if(t==='config')loadConfig();
  if(t==='sched')loadSchedule();
}
function setFilter(v){
  vType=v;
  document.querySelectorAll('#typeSeg button').forEach(function(b){b.classList.toggle('on',b.dataset.v===v);});
  cancelSel(); loadVideos(1);
}
function gotoPage(p){ cancelSel(); loadVideos(p); }
function onQuery(){clearTimeout(qTimer);qTimer=setTimeout(function(){cancelSel();loadVideos(1);},280);}
function clearLogs(){$('logs').innerHTML='';}

async function refreshState(){
  var j=await jget('/api/state'); if(!j||!j.ok)return;
  var s=j.state||{}, st=s.stats_directory||{}, rs=s.stats||{};
  $('cTotal').textContent=(st.total==null)?'–':st.total;
  $('cSplit').textContent='视频 '+(st.videos||0)+' · 图文 '+(st.images||0);
  $('cSize').textContent=fmtSize(st.total_size);
  $('cToday').textContent=(st.today==null)?'–':st.today;
  <!-- 本次运行：成功 = 视频 + 图文（与「已下载内容」口径一致） -->
  $('cRun').textContent=((rs.ok||0)+(rs.image||0))+' / '+((rs.dup||0)+(rs.skip||0))+' / '+(rs.fail||0);
  $('cRunS').textContent='成功 / 跳过 / 失败（视频 '+(rs.ok||0)+' · 图文 '+(rs.image||0)+'）';
  $('dot').className='dot'+(s.running?' run':(s.last_error?' err':''));
  var txt='空闲';
  if(s.running){
    if(s.stage==='download'&&s.file){
      if(s.phase==='connecting'){
        txt='连接中 · '+(s.host||'…')+(s.attempt?(' · 第'+s.attempt+'次'):'');
      }else{
        var sp0=s.speed||0;
        txt='下载中 · '+s.file+(sp0>0?(' · '+fmtSize(sp0)+'/s'):'');
      }
    }else if(s.stage==='item'){txt='解析中 · 第 '+(s.page||1)+' 页';}
    else{txt='运行中 · '+(s.stage||'');}
  }
  $('stext').textContent=txt;
  updateProgress(s);
  $('btnStart').disabled=!!s.running;
  $('btnStop').disabled=!s.running;
  var eb=$('btnExcl');
  if(eb){var nx=s.excluded_count||0;
    eb.style.display=nx?'':'none';
    eb.textContent='↺ 清空忽略名单（'+nx+'）';
    eb.title='清空「不再下载」名单（'+nx+' 条），让这些作品可以重新下载';}
  var ws=s.warnings||[], wb=$('warnBar');
  if(wb){
    wb.style.display=ws.length?'block':'none';
    if(ws.length)wb.innerHTML='<b>⚠ 环境问题</b><br>'+ws.map(esc).join('<br>');
  }
}
function updateProgress(s){
  var pw=$('progWrap'); if(!pw)return;
  if(!(s.running&&s.stage==='download'&&s.file)){pw.style.display='none';return;}
  pw.style.display='block';
  var got=s.got||0,total=s.total||0,sp=s.speed||0,tr=$('ptrack');
  var conn=(s.phase==='connecting');
  if(conn){
    // 正在探测/连接 CDN 主机：用不确定态条纹，并显示在连哪个主机、第几次尝试
    $('pfile').textContent=s.file;
    tr.classList.add('stripe'); $('pbar').style.width='';
    var at=s.attempt?('（第 '+s.attempt+'/'+(s.retries||s.attempt)+' 次尝试）'):'';
    $('pmeta').textContent='正在连接 '+(s.host||'…')+' '+at;
    return;
  }
  $('pfile').textContent=s.file;
  if(total>0){
    tr.classList.remove('stripe');
    $('pbar').style.width=Math.min(100,got*100/total).toFixed(1)+'%';
  }else{
    tr.classList.add('stripe');
    $('pbar').style.width='';
  }
  var meta=fmtSize(got);
  if(total>0)meta+=' / '+fmtSize(total)+'（'+(got*100/total).toFixed(0)+'%）';
  meta+=' · '+(sp>0?(fmtSize(sp)+'/s'):'速度 —');
  if(total>0&&sp>0)meta+=' · 剩余 '+fmtDur((total-got)/sp);
  if(s.host)meta+=' · '+s.host;
  $('pmeta').textContent=meta;
}

async function loadVideos(p){
  if(p<1)p=1; vpage=p;
  var q=encodeURIComponent($('q').value||'');
  var size=$('fSize').value, sort=$('fSort').value;
  var j=await jget('/api/videos?page='+p+'&size='+size+'&q='+q+'&type='+vType+'&sort='+sort);
  if(!j||!j.ok)return;
  pageItems=j.items||[];
  var tb=$('vbody'); tb.innerHTML='';
  if(!pageItems.length){
    tb.innerHTML='<tr><td colspan="7"><div class="empty"><span class="big">📭</span>还没有内容<br>'+
      '<span class="small">点右上角「开始下载」抓取喜欢的视频；图文需在「配置」里勾选「保存图文作品」</span></div></td></tr>';
  }
  pageItems.forEach(function(it,idx){
    var thumbRel=it.has_poster?it.rel.replace(/\.(mp4|mov|flv|m4a|mp3)$/i,'-poster.jpg')
        :(it.type==='image'&&it.files&&it.files[0]?it.files[0]:'');
    var poster=thumbRel?'<img class="thumb" src="/file?p='+encodeURIComponent(thumbRel)+'" onerror="this.style.visibility=\'hidden\'">':'';
    var tag=it.type==='image'?('<span class="tag img">图文'+(it.count>1?'·'+it.count:'')+'</span>'):'<span class="tag">视频</span>';
    var act=it.type==='image'
        ?'<button class="btn" onclick="galleryAt('+idx+')">🖼 查看</button>'
        :'<button class="btn" onclick="playAt('+idx+')">▶ 播放</button>';
    var tr=document.createElement('tr');
    tr.innerHTML='<td class="selcol"><input type="checkbox" '+(selSet[it.rel]?'checked':'')
      +' onchange="toggleSelAt('+idx+',this.checked)"></td>'
      +'<td>'+poster+'</td>'
      +'<td>'+tag+'<span class="tname">'+esc(it.title_disp||it.title||it.name)+'</span></td>'
      +'<td class="small hide-sm">'+esc(it.author)+'</td>'
      +'<td class="small">'+fmtSize(it.size)+'</td>'
      +'<td class="small hide-sm">'+fmtTime(it.mtime)+'</td>'
      +'<td class="acts">'+act+'</td>';
    tb.appendChild(tr);
  });
  var ab=$('selAllBox');
  if(ab){var nSel=selCount();
    ab.checked=pageItems.length>0&&nSel>=pageItems.length;
    ab.indeterminate=nSel>0&&nSel<pageItems.length;}
  renderSelBar();
  var pages=Math.max(1,Math.ceil(j.total/j.size));
  $('vinfo').textContent='第 '+j.page+' / '+pages+' 页 · 共 '+j.total+' 条';
  $('pgPrev').disabled=j.page<=1;
  $('pgNext').disabled=j.page>=pages;
}

function playAt(i){var it=pageItems[i]; if(!it)return;
  $('playerTitle').textContent=it.title_disp||it.name||'';
  $('playerVideo').src='/play?p='+encodeURIComponent(it.rel);
  $('player').classList.add('on');}
function closePlayer(){var v=$('playerVideo');v.pause();v.removeAttribute('src');v.load();
  $('player').classList.remove('on');}

function galleryAt(i){var it=pageItems[i]; if(!it)return;
  $('galleryTitle').textContent=(it.title_disp||it.name||'')+'（'+(it.files||[]).length+' 张）';
  var g=$('galleryGrid'); g.innerHTML='';
  (it.files||[]).forEach(function(f){
    var im=document.createElement('img');
    im.src='/file?p='+encodeURIComponent(f);
    im.loading='lazy';
    im.onclick=function(){window.open(im.src,'_blank');};
    g.appendChild(im);
  });
  $('gallery').classList.add('on');}
function closeGallery(){$('gallery').classList.remove('on');$('galleryGrid').innerHTML='';}

/* ---------- 多选批量删除 ---------- */
var delMode=false, selSet={}, delList=[];
function selCount(){return Object.keys(selSet).length;}
function renderSelBar(){
  var n=selCount(), b=$('btnDelSel');
  if(b){b.textContent=n?('🗑 删除（'+n+'）'):'🗑 删除';b.classList.toggle('sel-on',n>0);}
  var h=$('selHint'); if(h)h.classList.toggle('on',delMode);
  var c=$('btnDelCancel'); if(c)c.style.display=delMode?'':'none';
  var tv=$('vtable'); if(tv)tv.classList.toggle('selmode',delMode);
}
function toggleSelAt(i,on){
  var it=pageItems[i]; if(!it)return;
  if(on)selSet[it.rel]=1; else delete selSet[it.rel];
  var cb=document.querySelector('#vbody tr:nth-child('+(i+1)+') .selcol input');
  if(cb)cb.checked=!!on;                      // 程序化勾选时也同步视觉
  renderSelBar();
}
function selAllBox(on){
  pageItems.forEach(function(it){ if(on)selSet[it.rel]=1; else delete selSet[it.rel]; });
  renderSelBar(); loadVideos(vpage);
}
function toggleDelMode(){
  if(!delMode){ delMode=true; selSet={}; renderSelBar(); loadVideos(vpage); return; }
  var n=selCount();
  if(!n){ toast('请先勾选要删除的项目'); return; }
  delList=pageItems.filter(function(it){return selSet[it.rel];});
  $('delTitle').textContent='删除选中的 '+n+' 项？';
  $('delDesc').textContent=delList.slice(0,3).map(function(it){return it.title_disp||it.name;}).join('、')
    +(n>3?(' 等 '+n+' 项'):'')+'　删除后本地文件不再保留，下次同步会重新下载。';
  $('delmodal').classList.add('on');
}
function cancelSel(){ if(delMode){ delMode=false; selSet={}; renderSelBar(); } }
function cancelDelMode(){ delMode=false; selSet={}; renderSelBar(); loadVideos(vpage); }
function closeDel(){$('delmodal').classList.remove('on');delList=[];}
async function doDel(){
  if(!delList.length){ closeDel(); return; }
  var files=[], n=delList.length, path=delList[0].rel;
  delList.forEach(function(it){ (it.files||[it.rel]).forEach(function(f){files.push(f);}); });
  var j=await jpost('/api/delete',{files:files,path:path});
  closeDel();
  if(j.ok){
    toast('已删除 '+n+' 项（之后同步可重新下载）');
    delMode=false; selSet={}; renderSelBar(); loadVideos(vpage); refreshState();
  }else{ toast('删除失败：'+(j.error||'')); }
}
async function clearExclusions(){
  var j=await jpost('/api/clear-exclusions');
  if(j.ok){toast(j.cleared?('已清空 '+j.cleared+' 条忽略记录，可重新下载'):'当前没有忽略记录');refreshState();}
}

async function pollLogs(){
  var j=await jget('/api/logs?since='+logSince); if(!j||!j.ok)return;
  if(j.lines&&j.lines.length){
    var box=$('logs'), frag=document.createDocumentFragment();
    j.lines.forEach(function(ln){
      var d=document.createElement('div');
      if(ln.indexOf('] [WARN]')>=0)d.className='WARN';
      else if(ln.indexOf('] [ERROR]')>=0)d.className='ERROR';
      d.textContent=ln; frag.appendChild(d);
    });
    box.appendChild(frag);
    while(box.childElementCount>2000)box.removeChild(box.firstChild);
    if($('autoScroll').checked)box.scrollTop=box.scrollHeight;
  }
  if(typeof j.last_seq==='number')logSince=j.last_seq;
}

async function loadConfig(){
  var j=await jget('/api/config'); if(!j||!j.ok)return; var c=j.config||{};
  CURCFG=Object.assign({},c);            // 缓存整份配置：表单只暴露少数字段，其余沿用这份
  var g=function(id,v){var e=$(id); if(e)e.value=(v==null?'':v);};
  g('f_sec',c.sec_user_id); g('f_cookie',c.cookie); g('f_out',c.out||j.out_resolved||'');
  g('f_count',c.count); g('f_dlto',c.download_timeout);
  $('f_codec').value=String(c.codec||264);
  $('f_images').checked=!!c.images; $('f_caption').checked=c.caption_untitled!==false;
  $('cfginfo').innerHTML='配置文件：'+esc(j.config_path||'')+'<br>实际输出目录：'+esc(j.out_resolved||'');
  MOUNT_OUT=j.mount_out||'';
  renderOutHint();
}
var MOUNT_OUT='';
function renderOutHint(){
  var oh=$('outHint'); if(!oh)return;
  if(!MOUNT_OUT){ oh.textContent=''; oh.className='fhint'; return; }
  var v=($('f_out').value||'').trim().replace(/[\\/]+$/,'');
  if(v===MOUNT_OUT){
    oh.className='fhint';
    oh.innerHTML='✓ 容器内填 <b>'+esc(MOUNT_OUT)+'</b>，它已映射到你的飞牛目录';
  }else{
    oh.className='fhint warn';
    oh.innerHTML='⚠ 容器里必须填 <b>'+esc(MOUNT_OUT)+'</b>（只有它映射到飞牛磁盘，'+
      '填宿主机路径会存在容器内部看不到）　<a href="javascript:void(0)" onclick="fixOutDir()">一键改回</a>';
  }
}
function fixOutDir(){ if(!MOUNT_OUT)return; $('f_out').value=MOUNT_OUT; renderOutHint(); }
function collectConfig(){
  // 以服务器上的配置为底，只覆盖表单里出现的字段（隐藏项因此不会被重置）
  var c=Object.assign({},CURCFG), n=function(id){return $(id);};
  c.sec_user_id=n('f_sec').value.trim();
  c.cookie=n('f_cookie').value.trim();
  c.out=n('f_out').value.trim();
  c.count=+n('f_count').value||18;
  c.codec=+n('f_codec').value||264;
  c.download_timeout=+n('f_dlto').value||30;
  c.images=n('f_images').checked;
  c.caption_untitled=n('f_caption').checked;
  return c;
}
function requiredMissing(c){
  if(!c.sec_user_id)return 'sec_user_id';
  if(!c.cookie)return 'Cookie';
  if(!c.out)return '输出目录';
  return '';
}
async function saveConfig(andStart){
  var c=collectConfig();
  if(andStart){var miss=requiredMissing(c); if(miss){toast('请先填写：'+miss);showTab('config');return;}}
  var j=await jpost('/api/config',{config:c});
  if(!j.ok){toast('保存失败：'+(j.error||''));return;}
  toast('已保存');
  if(andStart)await startRun();
}
async function startRun(){
  var c=collectConfig(), miss=requiredMissing(c);
  if(miss){toast('请先填写必填项：'+miss);showTab('config');return;}
  var j=await jpost('/api/start',{config:c});
  toast(j.message||(j.ok?'已开始':'失败'));
  refreshState();
}
async function stopRun(){var j=await jpost('/api/stop');toast(j.message||'');refreshState();}

/* ---------- 定时任务 ---------- */
var WEEK=['一','二','三','四','五','六','日'];
var SCHED=[], NEWDAYS=[0,1,2,3,4,5,6];
async function loadSchedule(){
  var j=await jget('/api/schedule'); if(!j||!j.ok)return;
  SCHED=j.schedule||[];
  $('schedOn').checked=!!j.schedule_enabled;
  renderSchedule(); showNext(j.next);
}
function showNext(nx){
  $('schedNext').textContent=nx
    ? ('下次自动开始：'+nx.at+'　（'+nx.start+' → '+nx.stop+'）')
    : '当前没有已启用的定时任务';
}
function renderSchedule(){
  var tb=$('schedBody'); if(!tb)return; tb.innerHTML='';
  if(!SCHED.length){
    tb.innerHTML='<tr><td colspan="5"><div class="empty"><span class="big">⏰</span>还没有定时任务'+
      '<br><span class="small">在下面设置「开始时间 / 结束时间」，到点会自动开始下载，到点自动停止</span></div></td></tr>';
    return;
  }
  SCHED.forEach(function(t,i){
    var chips=WEEK.map(function(w,di){
      return '<span class="chip'+(t.days.indexOf(di)>=0?' on':'')+'" onclick="toggleDay('+i+','+di+')">'+w+'</span>';
    }).join('');
    var tr=document.createElement('tr');
    tr.innerHTML='<td><input type="time" value="'+esc(t.start)+'" onchange="syncRow(this,'+i+',\'start\')"></td>'
      +'<td><input type="time" value="'+esc(t.stop)+'" onchange="syncRow(this,'+i+',\'stop\')"></td>'
      +'<td><span class="chips">'+chips+'</span></td>'
      +'<td><label class="chk"><input type="checkbox" '+(t.enabled?'checked':'')
      +' onchange="syncRow(this,'+i+',\'enabled\')"></label></td>'
      +'<td><button class="btn danger" onclick="delTask('+i+')">🗑 删除</button></td>';
    tb.appendChild(tr);
  });
}
function syncRow(el,i,f){SCHED[i][f]=(el.type==='checkbox')?el.checked:el.value;}
function toggleDay(i,d){
  var k=SCHED[i].days.indexOf(d);
  if(k>=0)SCHED[i].days.splice(k,1); else SCHED[i].days.push(d);
  if(!SCHED[i].days.length)SCHED[i].days=[d];
  SCHED[i].days.sort(function(a,b){return a-b;});
  renderSchedule();
}
function delTask(i){SCHED.splice(i,1);renderSchedule();}
function renderNewDays(){
  var el=$('nsDays'); if(!el)return;
  el.innerHTML=WEEK.map(function(w,di){
    return '<span class="chip'+(NEWDAYS.indexOf(di)>=0?' on':'')+'" onclick="toggleNewDay('+di+')">'+w+'</span>';
  }).join('');
}
function toggleNewDay(d){
  var k=NEWDAYS.indexOf(d);
  if(k>=0)NEWDAYS.splice(k,1); else NEWDAYS.push(d);
  if(!NEWDAYS.length)NEWDAYS=[d];
  NEWDAYS.sort(function(a,b){return a-b;});
  renderNewDays();
}
function addTask(){
  var s=$('nsStart').value||'02:00', e=$('nsStop').value||'06:00';
  if(s===e){toast('开始时间与结束时间不能相同');return;}
  SCHED.push({start:s,stop:e,days:NEWDAYS.slice(),enabled:true});
  renderSchedule();
}
async function saveSchedule(){
  var j=await jpost('/api/schedule',{schedule:SCHED,schedule_enabled:$('schedOn').checked});
  if(!j.ok){toast('保存失败');return;}
  SCHED=j.schedule||[];
  renderSchedule(); showNext(j.next);
  toast('定时设置已保存');
}

function applyTheme(t){
  document.body.classList.toggle('light',t==='light');
  var b=$('btnTheme'); if(b)b.textContent=(t==='light')?'☀️':'🌙';
}
function toggleTheme(){
  var t=document.body.classList.contains('light')?'dark':'light';
  localStorage.setItem('dytheme',t); applyTheme(t);
}

// 弹窗：点击遮罩关闭 / Esc 关闭
$('player').addEventListener('click',function(e){if(e.target===this)closePlayer();});
$('gallery').addEventListener('click',function(e){if(e.target===this)closeGallery();});
$('delmodal').addEventListener('click',function(e){if(e.target===this)closeDel();});
document.addEventListener('keydown',function(e){
  if(e.key!=='Escape')return;
  if($('delmodal').classList.contains('on')){closeDel();return;}
  if(delMode){cancelDelMode();return;}
  if($('player').classList.contains('on'))closePlayer();
  if($('gallery').classList.contains('on'))closeGallery();
});

var _tm=/[?&]theme=(light|dark)\b/.exec(location.search);
applyTheme(_tm?_tm[1]:(localStorage.getItem('dytheme')||'dark'));
refreshState();
loadVideos(1);
loadConfig();          // 提前拉取整份配置，表单未暴露的项才不会在保存时被重置
renderNewDays();
var _tb=/[?&]tab=(videos|logs|config|sched)\b/.exec(location.search);
if(_tb)showTab(_tb[1]);
setInterval(refreshState,2000);
setInterval(pollLogs,1500);
setInterval(function(){
  if($('page-videos').style.display!=='none'&&$('btnStart').disabled)loadVideos(1);
},15000);
</script></body></html>"""


def main():
    global LOG_FILE
    ap = argparse.ArgumentParser(description="抖音喜欢视频下载 · Web 面板")
    ap.add_argument("--host", default="0.0.0.0", help="监听地址，默认 0.0.0.0")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8090")), help="端口，默认 8090")
    ap.add_argument("--out", default=None, help="输出目录（覆盖配置）")
    ap.add_argument("--config", default=None, help="配置文件路径，默认同目录 config.json")
    ap.add_argument("--password", default=None, help="访问密码（设置后需要登录）")
    args = ap.parse_args()

    cfg_path = args.config or os.path.join(BASE_DIR, "config.json")
    load_config(cfg_path)
    # 日志持久化文件：与 config.json 同目录
    LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(cfg_path)) or ".", "webui.log")
    restored = load_log_tail(LOG_FILE)
    if args.out:
        CONFIG["out"] = args.out
    if args.password is not None:
        CONFIG["password"] = args.password

    # ↓ 环境自检：目录/配置不可写时**不崩溃**，只记录告警（否则容器会无限重启）
    _e = ensure_writable(out_dir(), "输出目录")
    if _e:
        add_warning(_e + "　→　请在飞牛上把该目录属主改成容器运行用户（chown -R 1000:1000 目录），"
                          "或在 docker-compose.yml 里注释掉 user: 行")
    _e = ensure_writable(os.path.dirname(os.path.abspath(cfg_path)) or ".", "配置目录")
    if _e:
        add_warning(_e + "　→　配置将无法保存，请检查挂载目录权限")
    try:
        save_config(CONFIG)       # 每次启动都落盘，保证默认值(如输出目录)被记住
    except Exception as e:  # noqa
        add_warning(f"配置文件写入失败：{cfg_path}（{e.__class__.__name__}: {e}）　→　设置不会被保存")

    add_log(f"[INFO] Web 面板启动：http://{args.host}:{args.port}  输出目录：{out_dir()}"
            + (f"  (已恢复历史日志 {restored} 行)" if restored else ""))
    for _w in WARNINGS:
        add_log("[WARN] " + _w)
    if CONFIG.get("schedule_enabled") and CONFIG.get("schedule"):
        _nxt = sched_next_run()
        add_log("[INFO] 定时任务已启用，共 %d 条%s"
                % (len(CONFIG["schedule"]), ("，下次开始 " + _nxt["at"]) if _nxt else ""))
    threading.Thread(target=scheduler_loop, daemon=True).start()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f" * 抖音喜欢视频下载 Web 面板")
    print(f" * 访问地址: http://<本机IP>:{args.port}")
    print(f" * 输出目录: {out_dir()}")
    print(f" * 配置文件: {cfg_path}")
    for _w in WARNINGS:
        print(f" ! {_w}")
    if need_auth():
        print(" * 已启用访问密码")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n退出。")


if __name__ == "__main__":
    main()
