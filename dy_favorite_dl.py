#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抖音「喜欢视频」批量下载 —— 独立小工具（从 dysync.net 抽离）

只做一件事：用你账号的 Cookie + sec_user_id，把「我喜欢（点赞）」列表里的视频
全量批量下载到本地，按  输出目录/作者昵称/视频标题/视频ID.mp4  存放。

零第三方依赖：仅用 Python 标准库，Python 3.8+ 即可运行。

------------------------------------------------------------------------------
用法示例
------------------------------------------------------------------------------
# 1) 最简单：把 sec_user_id 写进同目录 sec_user_id.txt，Cookie 写进 cookie.txt
#    （推荐；这样不用改脚本/命令行，也不会踩 .bat 编码坑）
python dy_favorite_dl.py --out "D:/抖音/喜欢" --cookie-file cookie.txt

# 2) sec_user_id 直接写在命令行
python dy_favorite_dl.py --sec-user-id "MS4wLjABAAAA..." --out "D:/抖音/喜欢" --cookie-file cookie.txt

# 3) 通过环境变量提供 Cookie（Windows: set DY_COOKIE=xxx  |  Linux/macOS: export DY_COOKIE=xxx）
python dy_favorite_dl.py --sec-user-id "MS4w..." --out "D:/抖音/喜欢"

# 4) 先看要下载哪些，不实际下载
python dy_favorite_dl.py --out "D:/抖音/喜欢" --cookie-file cookie.txt --dry-run

------------------------------------------------------------------------------
如何获取 sec_user_id 与 Cookie（见项目 README 1.1 节）
------------------------------------------------------------------------------
1. 浏览器登录 https://www.douyin.com/ ，F12 打开开发者工具 -> Network。
2. 筛选框输入 /follow ，点自己主页的「关注」按钮，任选一个请求。
3. Payload 里找到 sec_user_id 的值 -> 即 --sec-user-id。
4. Headers 里找到 Cookie 的值（完整，前后不要带换行）-> 即 --cookie。

------------------------------------------------------------------------------
说明 / 与原项目的差异
------------------------------------------------------------------------------
- 原项目默认 OnlySyncNew=true，只抓第一页(18条)；本工具默认【全量翻页】。
- 目录结构对齐原项目：喜欢 -> 作者/标题/，封面 {id}-poster.jpg，头像 author/{uid}.jpg。
- 相同标题的不同视频会自动放到 「标题_id」 目录，避免互相覆盖（幂等，可断点续跑）。
- 命名：无文字描述的作品，目录名/文件名用生成的文案（不再是纯作品 ID）；**作者目录用纯昵称**（不再带 uid 数字），只有同名不同作者才退回「昵称_uid」；旧版目录会在下次同步时按 .dyid / .author 自动改名（不会重复下载）。加 --no-pretty-names 可关闭。
- 图文(无 video.bit_rate)默认跳过；加 --images 可保存其图片。
- 【重复运行不会重复下载】去重以"文件是否已存在"为准：已下载的跳过；跑完后再次运行会
  从最新重新扫描，只补下【新增的点赞】；若你手动删了某个文件，下次会自动补下。
  （中断的任务会从断点续传，不会从头再来。）
- 【重要】抖音接口现在要求 cookie 里带 ttwid，否则返回 "blocked"。
  本工具若发现 cookie 缺 ttwid，会调用 ttwid.bytedance.com 自动注册一个并合并使用；
  若仍被 blocked，会自动刷新 ttwid 后重试。也可直接用浏览器里带 ttwid 的完整 cookie。
- 若视频直链卡住，会在 --download-timeout 秒后中断并自动切换到备用直链重试。
"""

import argparse
import hashlib
import json
import os
import random
import re
import socket
import sys
import time
import gzip
import zlib
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime

# ---------------------------------------------------------------------------
# 常量（对齐 dysync.net 的 utils/DouyinRequestParamManager.cs）
# ---------------------------------------------------------------------------
DOUYIN_HOST = "https://www.douyin.com"
FAVORITE_API = DOUYIN_HOST + "/aweme/v1/web/aweme/favorite"
REFERER = "https://www.douyin.com/user/self?showTab=like"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
)

# 抖音 web 端公共请求参数（对应 InitBaseParams + InitUserFavoriteParams）
BASE_PARAMS = {
    "device_platform": "webapp",
    "aid": "6383",
    "channel": "channel_pc_web",
    "pc_client_type": "1",
    "pc_libra_divert": "Windows",
    "cookie_enabled": "true",
    "browser_language": "zh-CN",
    "browser_platform": "Win32",
    "browser_name": "Chrome",
    "browser_online": "true",
    "engine_name": "Blink",
    "os_name": "Windows",
    "os_version": "10",
    "device_memory": "8",
    "platform": "PC",
    "downlink": "10",
    "effective_type": "4g",
    "round_trip_time": "0",
    "update_version_code": "170400",
    "whale_cut_token": "",
    # 喜欢列表特有
    "version_code": "170400",
    "version_name": "17.4.0",
    "screen_width": "1536",
    "screen_height": "960",
    "browser_version": "140.0.0.0",
    "engine_version": "140.0.0.0",
    "cpu_core_num": "20",
    "support_h265": "1",
    "support_dash": "1",
    "min_cursor": "0",
    "cut_version": "1",
}

INVALID_NAME_CHARS = ['/', '\0', '\n', '\r', '\v', '\f', ':', '*', '?', '"', '<', '>', '|', '\\']
# Windows 保留设备名：作为文件名/目录名会创建失败
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


# ---------------------------------------------------------------------------
# 文件名处理（对齐 utils/DouyinFileNameHelper.SanitizeLinuxFileName）
# ---------------------------------------------------------------------------
def sanitize_name(original, default_name, is_folder=False):
    """把标题转成安全的名字。isfolder=True 时只保留中文/字母/数字（更激进）。"""
    default_name = (default_name or "default").replace(" ", "")
    if original is None or not str(original).strip():
        result = default_name
    else:
        name = str(original)
        for c in INVALID_NAME_CHARS:
            name = name.replace(c, "_")

        raw = name.encode("utf-8")
        if len(raw) <= 100:
            result = name.replace(" ", "")
        else:
            truncated = raw[:100].decode("utf-8", errors="ignore").replace("\0", "").replace(" ", "")
            result = truncated if truncated.strip() else default_name

        if is_folder:
            # 只保留 中文 / 字母 / 数字
            result = re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9]", "", result)

    if not result or not result.strip():
        result = default_name
    # Windows 兼容：去掉结尾的点和空格；避开保留设备名
    result = result.strip().rstrip(". ")
    if result.split(".")[0].upper() in _WIN_RESERVED:
        result = "_" + result
    if not result:
        result = default_name
    return result


# ---------------------------------------------------------------------------
# 空标题文案生成（围绕「黑丝」，按 aweme_id 确定性生成，同一视频文案稳定不变）
# ---------------------------------------------------------------------------
CAPTION_PREFIX = [
    "", "", "", "日常", "今日穿搭", "氛围感", "法式", "御姐", "暗黑系", "优雅",
    "性感", "清纯", "甜酷", "通勤", "轻熟", "冷艳", "高级感", "复古", "港风",
]
CAPTION_CORE = ["黑丝", "黑色丝袜", "薄黑丝", "亮面黑丝", "蕾丝丝袜", "网纱黑丝", "丝袜", "黑丝长腿"]
CAPTION_SUFFIX = [
    "穿搭分享", "氛围感拉满", "气质绝了", "长腿即视感", "性感又高级", "美到犯规",
    "谁顶得住", "日常记录", "心动瞬间", "太绝了", "高级感十足", "一眼惊艳",
    "细节拉满", "又飒又美", "回头率爆表", "合集", "随拍", "氛围感大片",
]


def generate_caption(seed):
    """按 seed（如 aweme_id）稳定生成一句围绕「黑丝」的文案；同一个 seed 结果固定。"""
    h = int(hashlib.md5(str(seed or "").encode("utf-8")).hexdigest(), 16)
    p = CAPTION_PREFIX[h % len(CAPTION_PREFIX)]
    c = CAPTION_CORE[(h // 7) % len(CAPTION_CORE)]
    s = CAPTION_SUFFIX[(h // 13) % len(CAPTION_SUFFIX)]
    return f"{p}{c}{s}"


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
class Logger:
    def __init__(self, log_path=None, sink=None, quiet=False):
        self.fh = open(log_path, "a", encoding="utf-8") if log_path else None
        self.sink = sink       # 可选回调：sink(level, line)，供 Web UI 等捕获日志
        self.quiet = quiet     # True 时不打印到控制台（仍写文件/回调）

    def log(self, level, msg):
        line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [{level}] {msg}"
        if not self.quiet:
            try:
                print(line, flush=True)
            except UnicodeEncodeError:
                print(line.encode("utf-8", "replace").decode("utf-8", "replace"), flush=True)
        if self.fh:
            self.fh.write(line + "\n")
            self.fh.flush()
        if self.sink:
            try:
                self.sink(level, line)
            except Exception:
                pass

    def info(self, msg):
        self.log("INFO", msg)

    def warn(self, msg):
        self.log("WARN", msg)

    def error(self, msg):
        self.log("ERROR", msg)

    def close(self):
        if self.fh:
            self.fh.close()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _decode_body(resp, raw):
    enc = (resp.headers.get("Content-Encoding") or "").lower()
    try:
        if "gzip" in enc:
            return gzip.decompress(raw)
        if "deflate" in enc:
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception:
        pass
    return raw


def http_get_bytes(url, cookie, timeout=60, extra_headers=None):
    """发起 GET，返回 (status, headers, body_bytes)。"""
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": REFERER,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Cookie": cookie,
    }
    if extra_headers:
        headers.update(extra_headers)

    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, resp.headers, _decode_body(resp, raw)
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, e.headers, _decode_body(e, raw)


# ---------------------------------------------------------------------------
# ttwid（抖音反爬必需字段）：缺失时自动注册获取
# ---------------------------------------------------------------------------
TTWID_REGISTER_URL = "https://ttwid.bytedance.com/ttwid/union/register/"


class BlockedError(RuntimeError):
    """服务端返回 blocked（风控拦截）"""


def cookie_has(cookie, name):
    """判断 cookie 字符串里是否存在非空的指定字段。"""
    for part in (cookie or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        if k.strip() == name and v.strip():
            return True
    return False


def get_ttwid(logger=None, timeout=20):
    """向字节 ttwid 注册接口申请一个 ttwid。"""
    payload = json.dumps({
        "region": "cn", "aid": 1768, "needFid": False,
        "service": "www.ixigua.com",
        "migrate_info": {"ticket": "", "source": "node"},
        "cbUrlProtocol": "https", "union": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        TTWID_REGISTER_URL, data=payload,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for sc in (resp.headers.get_all("Set-Cookie") or []):
                if sc.startswith("ttwid="):
                    return sc.split(";", 1)[0].split("=", 1)[1]
    except Exception as e:  # noqa
        if logger:
            logger.warn(f"获取 ttwid 失败：{e}")
    return None


def ensure_ttwid(cookie, logger, force=False):
    """确保 cookie 含 ttwid；缺失或 force 时自动注册获取并合并，返回新 cookie。"""
    if not force and cookie_has(cookie, "ttwid"):
        return cookie
    ttwid = get_ttwid(logger)
    if not ttwid:
        return cookie
    parts = [p.strip() for p in cookie.split(";")
             if p.strip() and not p.strip().startswith("ttwid=")]
    parts.append("ttwid=" + ttwid)
    if logger:
        logger.info(("已刷新" if force else "已自动补齐") + " ttwid（抖音反爬必需字段）")
    return "; ".join(parts)


def build_favorite_url(sec_user_id, cursor, count):
    params = dict(BASE_PARAMS)
    params["sec_user_id"] = sec_user_id
    params["max_cursor"] = str(cursor)
    params["count"] = str(count)
    return FAVORITE_API + "?" + urllib.parse.urlencode(params)


def describe_network_error(e):
    """把底层 socket/urlopen 异常翻译成人能看懂的话 + 可操作的修复建议。

    踩坑记录：NAS（飞牛/群晖等）的宿主机 /etc/resolv.conf 常指向 127.0.0.53
    （systemd-resolved 桩服务），容器访问不到它，所有域名解析都会超时，
    表现为 `<urlopen error [Errno -3] Try again>`（-3 == socket.EAI_AGAIN）。
    """
    msg = str(e)
    err = getattr(e, "errno", None)
    if isinstance(e, socket.gaierror) or err in (socket.EAI_AGAIN, socket.EAI_NONAME, -3, -2):
        if err == socket.EAI_AGAIN or err == -3:
            return ("域名解析超时（DNS 不可用）—— 这是 NAS 上跑容器最常见的问题："
                    "宿主机 resolv.conf 指向 127.0.0.53，容器访问不到。"
                    "修复：在 docker-compose.yml 的服务里加 "
                    "`dns: [223.5.5.5, 119.29.29.29, 114.114.114.114]` 后 "
                    "`docker compose up -d --build` 重建容器。"
                    f"（原始错误：{msg}）")
        return ("域名解析失败（DNS 查不到该域名）—— 请检查容器网络是否连通、"
                f"是否存在 DNS 污染；必要时在 compose 里显式配置 `dns:`。原始错误：{msg}")
    if isinstance(e, socket.timeout) or "timed out" in msg.lower():
        return f"网络请求超时（{msg}）—— 可能是网速慢或抖音侧限流，稍后重试即可。"
    if isinstance(e, urllib.error.URLError) and isinstance(getattr(e, "reason", None), OSError):
        return describe_network_error(e.reason)
    return msg


def fetch_favorite_page(sec_user_id, cursor, count, cookie, logger, retries=3):
    """请求一页喜欢列表，返回解析好的 dict；失败抛异常。"""
    url = build_favorite_url(sec_user_id, cursor, count)
    last_err = None
    for attempt in range(1, retries + 1):
        body = None
        try:
            status, headers, body = http_get_bytes(url, cookie)
            if status != 200:
                raise RuntimeError(f"HTTP {status}")
            if body and body.strip().lower() == b"blocked":
                raise BlockedError("服务端返回 blocked（风控拦截，通常是 Cookie 缺少 ttwid）")
            if not body or not body.strip():
                raise RuntimeError(
                    "接口返回空内容（HTTP 200 但 body 为空）——通常是 Cookie 已失效/未登录，"
                    "或该 sec_user_id 无效，或被抖音风控拦截"
                )
            data = json.loads(body.decode("utf-8", errors="replace"))
            return data
        except BlockedError:
            raise
        except json.JSONDecodeError as e:
            snippet = (body[:200].decode("utf-8", errors="replace") if isinstance(body, (bytes, bytearray)) else "")
            last_err = RuntimeError(f"响应不是 JSON（{e}）。返回片段：{snippet!r}")
            if attempt < retries:
                delay = min(2 ** attempt, 10)
                logger.warn(f"获取列表失败（{attempt}/{retries}）：{last_err}，{delay}s 后重试")
                time.sleep(delay)
        except Exception as e:  # noqa
            # 网络类错误翻译成人话；DNS 故障值得等更久，退避放大到 5/15 秒
            described = describe_network_error(e)
            last_err = RuntimeError(described)
            if attempt < retries:
                delay = (5, 15)[min(attempt - 1, 1)] if _is_dns_error(e) else min(2 ** attempt, 10)
                logger.warn(f"获取列表失败（{attempt}/{retries}）：{described}，{delay}s 后重试")
                time.sleep(delay)
    raise RuntimeError(f"获取列表最终失败：{last_err}")


def _is_dns_error(e):
    err = getattr(e, "errno", None)
    return isinstance(e, socket.gaierror) or err in (socket.EAI_AGAIN, socket.EAI_NONAME, -3, -2)


# ---------------------------------------------------------------------------
# 直链解析（对齐 GetBestMatchedVideoUrl）
# ---------------------------------------------------------------------------
def pick_url_list(addr):
    """取出直链列表。把【不同主机】的地址排在前面（重试时更容易换到可用 CDN 节点）。"""
    if not isinstance(addr, dict):
        return []
    urls = [u for u in (addr.get("url_list") or []) if isinstance(u, str) and u.strip()]
    seen, first, rest = set(), [], []
    for u in urls:
        try:
            h = urllib.parse.urlparse(u).netloc.lower()
        except Exception:
            h = ""
        if h and h not in seen:
            seen.add(h)
            first.append(u)
        else:
            rest.append(u)
    return first + rest


def host_of(url):
    try:
        return urllib.parse.urlparse(url).netloc
    except Exception:  # noqa
        return ""


def host_port_of(url):
    try:
        p = urllib.parse.urlparse(url)
        return (p.hostname or ""), (p.port or 443)
    except Exception:  # noqa
        return "", 443


def preflight_host(host, port=443, timeout=6):
    """快速探测主机能否连通。
    抖音偶尔会分发连不上的节点（如 v26-webf.douyinvod.com），
    先探一下能避免 urlopen 长时间挂起、界面看起来像卡死。"""
    if not host:
        return True
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return True
    except Exception:  # noqa
        return False


def choose_bitrate(bit_rates, prefer_h265):
    def find(use_h265):
        want = 1 if use_h265 else 0
        cands = [
            b for b in (bit_rates or [])
            if b.get("is_h265", 0) == want and pick_url_list(b.get("play_addr"))
        ]
        if not cands:
            return None
        return max(cands, key=lambda b: b.get("bit_rate", 0))

    return find(prefer_h265) or find(not prefer_h265)


# ---------------------------------------------------------------------------
# 下载（对齐 DownloadAsync：多地址轮换 + 重试 + 半成品清理 + 实时速度）
# ---------------------------------------------------------------------------
def human_size(n):
    """把字节数格式化为人类可读（用于进度/速度日志）。"""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "0B"
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}TB"


def download_file(urls, save_path, cookie, logger, retries=3, timeout=30,
                  progress_cb=None, log_progress=False, connect_timeout=6):
    """依次尝试 urls，每个最多重试；成功返回 True。timeout 为单次读/连接超时(秒)。

    progress_cb: 可选，回调 progress_cb(dict)，含 file/got/total/speed/elapsed/pct
                 以及 phase(connecting/downloading)、host、attempt
    log_progress: 为 True 时输出「连接中 / 进度 / 速度」日志（主视频下载用）
    """
    if not urls:
        return False
    os.makedirs(os.path.dirname(save_path), exist_ok=True)

    tmp_path = save_path + ".part"
    name = os.path.basename(save_path)

    def emit(got, total, t0, phase="downloading", host="", attempt=0):
        if not progress_cb:
            return
        elapsed = max(1e-6, time.time() - t0)
        try:
            progress_cb({
                "stage": "download", "file": name, "got": got, "total": total,
                "speed": got / elapsed, "elapsed": elapsed,
                "pct": (got * 100.0 / total) if total else 0.0,
                "phase": phase, "host": host, "attempt": attempt, "retries": retries,
            })
        except Exception:  # noqa
            pass

    attempts = 0
    idx = 0
    while attempts < retries:
        url = urls[idx % len(urls)]
        idx += 1
        attempts += 1
        got, total, t0 = 0, 0, time.time()
        host = host_of(url)
        _ph, _pp = host_port_of(url)
        # 先探测主机：不可达就快速换下一个地址，而不是干等 urlopen 超时
        if log_progress:
            logger.info(f"[下载中] {name} 正在连接 {host or '?'}（第 {attempts}/{retries} 次尝试）")
        emit(0, 0, t0, phase="connecting", host=host, attempt=attempts)
        if not preflight_host(_ph, _pp, connect_timeout):
            logger.warn(f"主机不可达（{_ph}:{_pp} 连接超时 {connect_timeout}s），换下一个地址")
            emit(0, 0, t0, phase="connecting", host=host, attempt=attempts)
            continue
        try:
            headers = {
                "User-Agent": USER_AGENT,
                "Referer": "https://www.douyin.com/",
                "Accept": "*/*",
                "Accept-Encoding": "identity",
                "Cookie": cookie,
            }
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                try:
                    total = int(resp.headers.get("Content-Length") or 0)
                except (TypeError, ValueError):
                    total = 0
                t0 = time.time()
                last_emit = last_log = t0
                emit(0, total, t0, host=host, attempt=attempts)
                with open(tmp_path, "wb") as f:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                        now = time.time()
                        if now - last_emit >= 0.4:          # 网页进度条：约 2.5 次/秒
                            emit(got, total, t0, host=host, attempt=attempts)
                            last_emit = now
                        if log_progress and now - last_log >= 4.0:   # 日志：每 4 秒一条
                            el = max(1e-6, now - t0)
                            spd = human_size(got / el) + "/s"
                            if total:
                                logger.info(f"[下载中] {name}  {human_size(got)}/{human_size(total)} "
                                            f"({got * 100 // max(total, 1)}%)  {spd}")
                            else:
                                logger.info(f"[下载中] {name}  {human_size(got)}  {spd}")
                            last_log = now
                if got <= 0:
                    raise RuntimeError("下载内容为空")
            os.replace(tmp_path, save_path)
            el = max(1e-6, time.time() - t0)
            emit(got, total, t0, host=host, attempt=attempts)
            if log_progress:
                logger.info(f"[完成] {name}  {human_size(got)}  {human_size(got / el)}/s  用时 {el:.1f}s")
            return True
        except Exception as e:  # noqa
            logger.warn(f"下载失败（{attempts}/{retries}）{host} -> {e}")
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            if attempts < retries:
                time.sleep(min(2 ** attempts, 10))
    return False


def download_simple(url, save_path, cookie, logger, retries=2, timeout=20):
    return download_file([url], save_path, cookie, logger, retries=retries, timeout=timeout)


# ---------------------------------------------------------------------------
# 状态（断点续跑）
# ---------------------------------------------------------------------------
class State:
    def __init__(self, out_dir, resume=True):
        self.dir = os.path.join(out_dir, ".dystate")
        os.makedirs(self.dir, exist_ok=True)
        self.state_path = os.path.join(self.dir, "state.json")
        self.done_path = os.path.join(self.dir, "downloaded_ids.txt")
        self.excluded_path = os.path.join(self.dir, "excluded_ids.txt")  # 删除黑名单：不再下载
        self.resume = resume
        self.done_ids = set()
        self.excluded_ids = set()
        self.cursor = "0"
        self.finished = False
        self._load()

    def _load(self):
        if os.path.isfile(self.done_path):
            with open(self.done_path, "r", encoding="utf-8") as f:
                self.done_ids = {ln.strip() for ln in f if ln.strip()}
        if os.path.isfile(self.excluded_path):
            with open(self.excluded_path, "r", encoding="utf-8") as f:
                self.excluded_ids = {ln.strip() for ln in f if ln.strip()}
        if not self.resume:
            return
        if os.path.isfile(self.state_path):
            try:
                with open(self.state_path, "r", encoding="utf-8") as f:
                    st = json.load(f)
                self.cursor = str(st.get("cursor", "0"))
                self.finished = bool(st.get("finished", False))
            except Exception:
                pass

    def add_excluded(self, aweme_id):
        """加入「不再下载」黑名单。"""
        if not aweme_id or aweme_id in self.excluded_ids:
            return
        self.excluded_ids.add(aweme_id)
        with open(self.excluded_path, "a", encoding="utf-8") as f:
            f.write(aweme_id + "\n")

    def start_cursor(self):
        """起始游标：上次已完整跑完 → 从 0 重新扫描（抓取新点赞，已下载的靠去重跳过）；
        上次未跑完（中断）→ 从断点继续。"""
        return "0" if self.finished else self.cursor

    def mark_done(self, aweme_id):
        if aweme_id in self.done_ids:
            return
        self.done_ids.add(aweme_id)
        with open(self.done_path, "a", encoding="utf-8") as f:
            f.write(aweme_id + "\n")

    def save_progress(self, cursor, finished=False):
        self.cursor = str(cursor)
        self.finished = finished
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump({"cursor": self.cursor, "finished": finished,
                       "updated": datetime.now().isoformat()}, f, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 目录解析（幂等，避免同标题视频互相覆盖）
# ---------------------------------------------------------------------------
def resolve_video_dir(base_dir, title, aweme_id):
    """返回该视频应存放的目录；相同标题的不同视频追加 _id。"""
    d = os.path.join(base_dir, title)
    marker = os.path.join(d, ".dyid")
    if os.path.isdir(d):
        if os.path.isfile(marker):
            try:
                with open(marker, "r", encoding="utf-8") as f:
                    if f.read().strip() == aweme_id:
                        return d
            except OSError:
                pass
        # 目录已被别的视频占用 -> 追加 _id
        d2 = os.path.join(base_dir, f"{title}_{aweme_id}")
        if os.path.isdir(d2):
            return d2
        os.makedirs(d2, exist_ok=True)
        _write_marker(d2, aweme_id)
        return d2

    os.makedirs(d, exist_ok=True)
    _write_marker(d, aweme_id)
    return d


def _write_marker(d, aweme_id):
    try:
        with open(os.path.join(d, ".dyid"), "w", encoding="utf-8") as f:
            f.write(aweme_id)
    except OSError:
        pass


def pretty_title(desc, aweme_id, use_caption=True):
    """作品标题（用于目录名/文件名）。
    有描述用描述；无描述（退化成纯作品 ID）时改用生成的文案，避免磁盘上出现纯数字目录。"""
    t = sanitize_name(desc or "", aweme_id, is_folder=True)
    if use_caption and re.fullmatch(r"\d+", t or ""):
        return sanitize_name(generate_caption(aweme_id), aweme_id, is_folder=True)
    return t


def find_dir_by_marker(base_dir, aweme_id):
    """在作者目录下按 .dyid 找到该作品已存在的目录（改名迁移用）。"""
    try:
        entries = os.listdir(base_dir)
    except OSError:
        return None
    for name in entries:
        d = os.path.join(base_dir, name)
        if not os.path.isdir(d):
            continue
        try:
            with open(os.path.join(d, ".dyid"), "r", encoding="utf-8") as f:
                if f.read().strip() == aweme_id:
                    return d
        except OSError:
            continue
    return None


def read_marker(d):
    try:
        with open(os.path.join(d, ".dyid"), "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _dir_contains_aweme(d, aweme_id):
    try:
        for name in os.listdir(d):
            sub = os.path.join(d, name)
            if os.path.isdir(sub) and read_marker(sub) == aweme_id:
                return True
    except OSError:
        pass
    return False


def _write_author_marker(d, uid):
    try:
        with open(os.path.join(d, ".author"), "w", encoding="utf-8") as f:
            f.write(uid or "")
    except OSError:
        pass


def _read_author_marker(d):
    try:
        with open(os.path.join(d, ".author"), "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def resolve_author_dir(out_root, author_name, author_uid, pretty=True, logger=None):
    """作者目录：优先用「纯昵称」（磁盘上更美观）；只有同名不同作者才退回「昵称_uid」。
    旧版「昵称uid」目录会自动改名为纯昵称（判据：.author 归属标记为空或属于同一 uid）。"""
    plain = sanitize_name(author_name or "", author_uid or "未知博主", is_folder=True) \
        if pretty else sanitize_name(f"{author_name}_{author_uid}", author_uid or "未知博主", is_folder=True)
    if not plain:
        plain = author_uid or "未知博主"
    plain_dir = os.path.join(out_root, plain)
    # 注意：历史目录名是 sanitize_name("昵称_uid") 的结果（下划线会被去掉），
    # 这里必须用同样的方式拼，才能匹配上磁盘上已有的旧目录。
    legacy_name = sanitize_name(f"{plain}_{author_uid}", author_uid or plain, is_folder=True) \
        if author_uid else None
    legacy = os.path.join(out_root, legacy_name) if legacy_name else None

    if pretty and legacy and os.path.isdir(legacy) and not os.path.isdir(plain_dir):
        if _read_author_marker(legacy) in ("", author_uid):     # 无归属标记或本作者的 -> 可改名
            try:
                os.rename(legacy, plain_dir)
                _write_author_marker(plain_dir, author_uid)
                if logger:
                    logger.info(f"[整理命名] 作者目录「{os.path.basename(legacy)}」→「{plain}」")
            except OSError:
                pass

    if not os.path.isdir(plain_dir):
        os.makedirs(plain_dir, exist_ok=True)
    cur = _read_author_marker(plain_dir)
    if not cur or cur == author_uid:
        _write_author_marker(plain_dir, author_uid)
        return plain_dir
    # 同名不同作者：退回「昵称_uid」，避免两个作者的作品混在一起
    tgt = legacy if (legacy and os.path.isdir(legacy)) else \
        os.path.join(out_root, sanitize_name(f"{plain}_{author_uid}", author_uid or plain, is_folder=True))
    os.makedirs(tgt, exist_ok=True)
    _write_author_marker(tgt, author_uid)
    return tgt


def rename_artifacts(d, aweme_id, new_title):
    """把目录内以 aweme_id 命名的文件（含 -poster 等）改为 new_title 开头。"""
    renamed = []
    try:
        names = os.listdir(d)
    except OSError:
        return renamed
    for fn in names:
        stem, ext = os.path.splitext(fn)
        if stem != aweme_id and not stem.startswith(aweme_id + "-"):
            continue
        if stem == new_title or stem.startswith(new_title + "-"):
            continue
        new_stem = new_title + stem[len(aweme_id):]
        src, dst = os.path.join(d, fn), os.path.join(d, new_stem + ext)
        if os.path.exists(dst):
            continue
        try:
            os.replace(src, dst)
            renamed.append(f"{fn} → {new_stem}{ext}")
        except OSError:
            pass
    return renamed


def migrate_legacy_dir(base_dir, expect_dir, aweme_id, new_title, logger=None):
    """把旧版「纯 ID 命名」的目录迁移到期望目录名，并重命名内部文件。
    锚点是 .dyid，因此不会误动别人的目录；成功返回新目录，失败返回 None。"""
    old = find_dir_by_marker(base_dir, aweme_id)
    if not old or os.path.abspath(old) == os.path.abspath(expect_dir):
        return None
    if os.path.exists(expect_dir):
        return None
    notes = []
    try:
        os.rename(old, expect_dir)
    except OSError:
        return None
    notes.append(f"目录「{os.path.basename(old)}」→「{os.path.basename(expect_dir)}」")
    ren = rename_artifacts(expect_dir, aweme_id, new_title)
    if ren:
        notes.append("文件 " + "、".join(ren))
    if logger:
        logger.info(f"[整理命名] {aweme_id}：{'；'.join(notes)}")
    return expect_dir


# ---------------------------------------------------------------------------
# 处理单条作品
# ---------------------------------------------------------------------------
def process_aweme(item, args, cookie, logger, state, file_progress=None):
    aweme_id = str(item.get("aweme_id") or "").strip()
    if not aweme_id:
        return "skip"

    # 删除黑名单：在网页里删过的视频不再重复下载
    if aweme_id in state.excluded_ids:
        if getattr(args, "verbose", False):
            logger.info(f"[已忽略] {aweme_id}（在「不再下载」黑名单中）")
        return "skip"

    # 去重以「文件是否已存在」为准（见下方 save_path 判断）：
    # 已下载的绝不重复下；若你手动删了文件，下次会重新补下。
    # state.done_ids 仅用于统计/断点，不再作为跳过依据。

    desc = item.get("desc") or ""
    author = item.get("author") or {}
    author_name = author.get("nickname") or "未知博主"
    author_uid = str(author.get("uid") or "")

    video = item.get("video") or {}
    bit_rates = video.get("bit_rate")

    # 目录/文件名：无描述时用生成的文案（可用 --no-pretty-names 关掉，退回纯 ID）
    pretty = bool(getattr(args, "pretty_names", True))

    # 作者目录：默认纯昵称（不含 uid 数字）；同名不同作者才加 uid
    base_dir = resolve_author_dir(args.out, author_name, author_uid, pretty, logger)

    title = pretty_title(desc, aweme_id, use_caption=pretty)
    expect_dir = os.path.join(base_dir, title)
    video_dir = None
    if pretty and not os.path.isdir(expect_dir):
        # 旧版用「纯 ID」命名的目录 -> 先迁移改名，避免同一作品被重复下载
        video_dir = migrate_legacy_dir(base_dir, expect_dir, aweme_id, title, logger)
    if not video_dir:
        video_dir = resolve_video_dir(base_dir, title, aweme_id)

    # ---- 图文（无视频码率）----
    if not bit_rates:
        images = item.get("images") or []
        if not images:
            logger.warn(f"[{aweme_id}] 无视频也无图片，跳过")
            state.mark_done(aweme_id)
            return "skip"
        if not args.images:
            logger.info(f"[图文] {aweme_id} [{desc[:30]}] 跳过（加 --images 可保存图片）")
            state.mark_done(aweme_id)
            return "image"
        saved = 0
        for i, img in enumerate(images, 1):
            urls = pick_url_list({"url_list": img.get("url_list")})
            if not urls:
                continue
            p = os.path.join(video_dir, f"{aweme_id}_{i:03d}.jpg")
            if os.path.exists(p):
                saved += 1
                continue
            if download_simple(urls[0], p, cookie, logger):
                saved += 1
        logger.info(f"[图文] {aweme_id} [{desc[:30]}] 保存图片 {saved}/{len(images)}")
        state.mark_done(aweme_id)
        return "image"

    # ---- 视频 ----
    bit = choose_bitrate(bit_rates, prefer_h265=(args.codec == 265))

    # 先推导文件名并判断是否已下载（即使暂时拿不到直链，也不影响"已存在"判定）
    fmt = (bit or {}).get("format") or "mp4"
    if bit is None:
        for _b in (bit_rates or []):
            if _b.get("format"):
                fmt = _b["format"]
                break
    file_name = f"{title}.{fmt}" if pretty else f"{aweme_id}.{fmt}"
    save_path = os.path.join(video_dir, file_name)

    if os.path.exists(save_path) and not args.overwrite:
        if getattr(args, "verbose", False):
            logger.info(f"[已存在] {author_name} / {desc[:30]} / {file_name}")
        state.mark_done(aweme_id)
        maybe_sidecars(item, save_path, author_uid, cookie, logger, args)
        return "dup"

    if bit is None:
        logger.error(f"[{aweme_id}] 未找到可用下载地址，跳过")
        return "fail"

    urls = pick_url_list(bit.get("play_addr") or {})
    if not urls:
        logger.error(f"[{aweme_id}] url_list 为空，跳过")
        return "fail"

    if args.dry_run:
        logger.info(f"[DRY] 将下载 {author_name} / {desc[:40]} -> {save_path}")
        return "dry"

    logger.info(f"[下载中] {author_name} / {desc[:40]} / {file_name}  ({bit.get('bit_rate', 0)//1000}kbps, h265={bit.get('is_h265')})")
    time.sleep(random.uniform(args.min_interval, args.max_interval))

    ok = download_file(urls, save_path, cookie, logger,
                       timeout=getattr(args, "download_timeout", 30),
                       progress_cb=file_progress, log_progress=True)
    if not ok:
        logger.error(f"[{aweme_id}] 下载失败：{desc[:40]}")
        return "fail"

    maybe_sidecars(item, save_path, author_uid, cookie, logger, args)
    state.mark_done(aweme_id)
    return "ok"


def maybe_sidecars(item, video_save_path, author_uid, cookie, logger, args):
    """下载封面 + 作者头像（对齐原项目）。"""
    video = item.get("video") or {}
    video_dir = os.path.dirname(video_save_path)
    stem = os.path.splitext(os.path.basename(video_save_path))[0]

    if not args.no_cover:
        cover = (video.get("cover") or {})
        urls = pick_url_list(cover)
        if urls:
            cover_path = os.path.join(video_dir, f"{stem}-poster.jpg")
            if not os.path.exists(cover_path):
                download_simple(urls[0], cover_path, cookie, logger)

    if not args.no_avatar and author_uid:
        author = item.get("author") or {}
        avatar = (author.get("avatar_larger") or {})
        urls = pick_url_list(avatar) or pick_url_list(author.get("avatar_thumb") or {})
        if urls:
            avatar_dir = os.path.join(args.out, "author")
            avatar_path = os.path.join(avatar_dir, f"{author_uid}.jpg")
            if not os.path.exists(avatar_path):
                os.makedirs(avatar_dir, exist_ok=True)
                download_simple(urls[0], avatar_path, cookie, logger)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run(args, stop_event=None, progress_cb=None, log_sink=None):
    """执行一次同步。
    stop_event : threading.Event，置位后尽快停止（已下载内容保留，可续传）
    progress_cb: 进度回调 progress_cb(dict)
    log_sink   : 日志回调 log_sink(level, line)
    """
    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    logger = Logger(os.path.join(out_dir, "dy_favorite_dl.log"), sink=log_sink)
    state = State(out_dir, resume=not args.no_resume)

    def stopped():
        return stop_event is not None and stop_event.is_set()

    logger.info("=" * 70)
    logger.info(f"抖音喜欢视频批量下载 | 输出: {out_dir}")
    logger.info(f"sec_user_id={args.sec_user_id[:20]}...  codec={'H265' if args.codec == 265 else 'H264'}  "
                f"页码量={args.count}  dry_run={args.dry_run}  resume={not args.no_resume}")
    if not args.no_resume and state.finished:
        logger.info(f"上次已完整同步过；本次从最新重新扫描以抓取新点赞，已下载的会自动跳过（已记录 {len(state.done_ids)} 条）。")
    elif not args.no_resume and state.cursor not in ("0", ""):
        logger.info(f"检测到未完成的断点，从 cursor={state.cursor} 继续；已记录完成 {len(state.done_ids)} 条")
    logger.info("=" * 70)

    # 抖音反爬：Cookie 必须含 ttwid，缺失则自动注册获取（否则接口返回 blocked）
    cookie = args.cookie
    if not cookie_has(cookie, "ttwid"):
        logger.info("Cookie 中未发现 ttwid，正在自动获取...")
        cookie = ensure_ttwid(cookie, logger)
        if not cookie_has(cookie, "ttwid"):
            logger.warn("未能自动获取 ttwid，接口可能返回 blocked。建议改用浏览器里完整 Cookie（含 ttwid）。")

    cursor = state.start_cursor() if not args.no_resume else "0"
    page = 0
    stats = {"ok": 0, "dup": 0, "fail": 0, "skip": 0, "image": 0, "dry": 0}
    total_seen = 0
    video_seen = 0

    def report(stage=""):
        if not progress_cb:
            return
        try:
            progress_cb({"stage": stage, "page": page, "cursor": cursor,
                         "stats": dict(stats), "total_seen": total_seen,
                         "video_seen": video_seen, "out_dir": out_dir})
        except Exception:
            pass

    def file_progress(p):
        """下载中的逐文件进度（字节/速度），直接透传给上层 UI。"""
        if not progress_cb:
            return
        try:
            progress_cb(p)
        except Exception:
            pass

    report("start")

    while True:
        page += 1
        if stopped():
            logger.warn("收到停止信号，本次结束（已下载内容保留，下次自动续传）。")
            break
        if args.max_pages and page > args.max_pages:
            logger.info(f"已达 --max-pages {args.max_pages}，停止")
            break

        logger.info(f"----- 第 {page} 页 (cursor={cursor}) -----")
        try:
            data = fetch_favorite_page(args.sec_user_id, cursor, args.count, cookie, logger)
        except BlockedError as e:
            logger.warn(f"{e}，尝试刷新 ttwid 后重试本页...")
            cookie = ensure_ttwid(cookie, logger, force=True)
            try:
                data = fetch_favorite_page(args.sec_user_id, cursor, args.count, cookie, logger)
            except Exception as e2:  # noqa
                logger.error(f"获取第 {page} 页失败：{e2}")
                break
        except Exception as e:  # noqa
            logger.error(f"获取第 {page} 页失败：{e}")
            break

        status_code = data.get("status_code", -1)
        if status_code != 0:
            logger.error(f"接口返回 status_code={status_code}，可能是 Cookie 失效或触发风控，停止。")
            break

        aweme_list = data.get("aweme_list") or []
        if not aweme_list:
            logger.info("本页无数据，结束。")
            break

        for item in aweme_list:
            if stopped():
                break
            if args.max_videos and video_seen >= args.max_videos:
                break
            total_seen += 1
            if (item.get("video") or {}).get("bit_rate"):
                video_seen += 1
            try:
                r = process_aweme(item, args, cookie, logger, state, file_progress)
                stats[r] = stats.get(r, 0) + 1
            except Exception as e:  # noqa
                stats["fail"] += 1
                logger.error(f"处理作品异常：{e}")
            report("item")

        if stopped():
            logger.warn("收到停止信号，本次结束（已下载内容保留，下次自动续传）。")
            break

        if args.max_videos and video_seen >= args.max_videos:
            logger.info(f"已达 --max-videos {args.max_videos}，停止")
            break

        if data.get("has_more") != 1:
            logger.info("has_more != 1，已到最后一页。")
            # 完整跑完：下次从最新重新扫描（抓新点赞），已下载的靠去重跳过
            state.save_progress("0", finished=True)
            break

        next_cursor = str(data.get("max_cursor") or data.get("cursor") or cursor)
        # 未跑完：记住断点，下次可续传
        state.save_progress(next_cursor, finished=False)
        cursor = next_cursor
        report("page")

        # 可被停止打断的翻页延迟
        delay = random.uniform(args.page_delay_min, args.page_delay_max)
        waited = 0.0
        while waited < delay and not stopped():
            time.sleep(0.2)
            waited += 0.2

    logger.info("-" * 70)
    logger.info(f"结束。成功 {stats['ok']}，已存在 {stats['dup']}，失败 {stats['fail']}，"
                f"图文 {stats['image']}，跳过 {stats['skip']}，预演 {stats['dry']}")
    logger.info("-" * 70)
    report("done")
    logger.close()


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        description="抖音「喜欢视频」批量下载（独立小工具，仅标准库）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  python dy_favorite_dl.py --sec-user-id MS4w... --out D:/抖音/喜欢 --cookie-file cookie.txt\n"
               "  python dy_favorite_dl.py --out D:/抖音/喜欢 --cookie-file cookie.txt   # 自动读同目录 sec_user_id.txt\n",
    )
    p.add_argument("--sec-user-id", default=None,
                   help="抖音 sec_user_id（也可用 --sec-user-id-file，或同目录 sec_user_id.txt）")
    p.add_argument("--sec-user-id-file", default=None, help="从文件读取 sec_user_id")
    p.add_argument("--out", required=True, help="输出目录")
    p.add_argument("--cookie", default=None, help="完整 Cookie 字符串（或用 --cookie-file / 环境变量 DY_COOKIE）")
    p.add_argument("--cookie-file", default=None, help="从文件读取 Cookie（推荐，避免命令行泄露）")
    p.add_argument("--count", type=int, default=18, help="每页条数，默认 18（与原项目一致）")
    p.add_argument("--max-pages", type=int, default=0, help="最多抓取页数，0=不限")
    p.add_argument("--max-videos", type=int, default=0, help="最多处理视频数，0=不限")
    p.add_argument("--codec", type=int, choices=[264, 265], default=264, help="优先编码，默认 264；无匹配自动回退")
    p.add_argument("--min-interval", type=float, default=1.0, help="每个视频下载前最小延迟秒，默认1")
    p.add_argument("--max-interval", type=float, default=4.0, help="每个视频下载前最大延迟秒，默认4")
    p.add_argument("--page-delay-min", type=float, default=2.0, help="翻页最小延迟秒，默认2")
    p.add_argument("--page-delay-max", type=float, default=9.0, help="翻页最大延迟秒，默认9")
    p.add_argument("--download-timeout", type=float, default=30.0,
                   help="单个文件下载的单次读/连接超时秒，默认30（卡住会在此时间内中断并换地址重试）")
    p.add_argument("--images", action="store_true", help="图文作品也保存图片（默认跳过）")
    p.add_argument("--no-cover", action="store_true", help="不下载封面 poster.jpg")
    p.add_argument("--no-avatar", action="store_true", help="不下载作者头像")
    p.add_argument("--no-resume", action="store_true", help="忽略断点状态，从头开始")
    p.add_argument("--overwrite", action="store_true", help="已存在的文件也重新下载")
    p.add_argument("--verbose", action="store_true", help="打印每一条已存在的记录（默认只在结尾汇总）")
    p.add_argument("--no-pretty-names", dest="pretty_names", action="store_false",
                   help="目录/文件名不使用生成的文案（作品无描述时退回纯 ID）")
    p.add_argument("--dry-run", action="store_true", help="只列出将要下载的内容，不实际下载")
    return p


def normalize_cookie(s):
    """清理 Cookie：去掉首尾空白/引号、去掉可能粘贴进来的 'Cookie:' 前缀。"""
    s = (s or "").strip()
    if s.lower().startswith("cookie:"):
        s = s[len("cookie:"):].strip()
    if len(s) >= 2 and ((s[0] == '"' and s[-1] == '"') or (s[0] == "'" and s[-1] == "'")):
        s = s[1:-1].strip()
    return s


def resolve_cookie(args):
    if args.cookie:
        return normalize_cookie(args.cookie)
    if args.cookie_file:
        path = os.path.abspath(args.cookie_file)
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        with open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
            return normalize_cookie(f.read())
    env = os.environ.get("DY_COOKIE")
    if env:
        return normalize_cookie(env)
    return None


PLACEHOLDER_HINTS = ("请替换", "你的sec", "your_sec", "替换成", "填这里", "xxxxxxxx", "replace")


def normalize_scalar(s):
    """清理 sec_user_id：去引号/空白；若粘贴的是主页链接则从中提取。"""
    s = (s or "").strip()
    if len(s) >= 2 and ((s[0] == '"' and s[-1] == '"') or (s[0] == "'" and s[-1] == "'")):
        s = s[1:-1].strip()
    m = re.search(r"[?&]sec_user_id=([^&\s]+)", s) or re.search(r"/user/([A-Za-z0-9_\-]+)", s)
    if m:
        s = m.group(1)
    return re.sub(r"\s+", "", s)


def resolve_sec_user_id(args):
    """优先级：命令行 -> --sec-user-id-file -> 脚本目录/当前目录的 sec_user_id.txt"""
    if args.sec_user_id:
        return normalize_scalar(args.sec_user_id)

    candidates = []
    if args.sec_user_id_file:
        candidates.append(os.path.abspath(args.sec_user_id_file))
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(script_dir, "sec_user_id.txt"))
    candidates.append(os.path.join(os.getcwd(), "sec_user_id.txt"))

    for path in candidates:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
                value = normalize_scalar(f.read())
            if value:
                return value
            if args.sec_user_id_file and os.path.abspath(args.sec_user_id_file) == path:
                raise FileNotFoundError(path)
    if args.sec_user_id_file and not any(os.path.isfile(p) for p in candidates):
        raise FileNotFoundError(os.path.abspath(args.sec_user_id_file))
    return None


def main():
    parser = build_parser()
    args = parser.parse_args()

    # 防呆 1：sec_user_id（只拦截明确的占位符；长度异常只警告，避免误伤真实值）
    try:
        sec_user_id = resolve_sec_user_id(args)
    except FileNotFoundError as e:
        print(f"× 找不到 sec_user_id 文件：{e.args[0]}", file=sys.stderr)
        sys.exit(2)

    if not sec_user_id:
        print("× 未提供 sec_user_id。以下任选其一：", file=sys.stderr)
        print('  1) 命令行：--sec-user-id "MS4wLjABAAAA..."', file=sys.stderr)
        print("  2) 文件：  在同目录新建 sec_user_id.txt，把 sec_user_id 写进去（推荐，无需改脚本）", file=sys.stderr)
        print("  3) 文件：  --sec-user-id-file 你的文件路径", file=sys.stderr)
        sys.exit(2)

    if any(h in sec_user_id.lower() for h in PLACEHOLDER_HINTS):
        shown = sec_user_id[:24]
        print(f"× sec_user_id 似乎还是占位符：{shown}...", file=sys.stderr)
        print("  请填真实值：编辑 run.example.bat 里的 SEC_USER_ID，或写进同目录 sec_user_id.txt。", file=sys.stderr)
        sys.exit(2)

    if len(sec_user_id) < 10:
        print(f"[警告] sec_user_id 只有 {len(sec_user_id)} 个字符，看起来偏短，可能不对。", file=sys.stderr)

    args.sec_user_id = sec_user_id

    # 防呆 2：Cookie 文件不存在 / 为空
    try:
        cookie = resolve_cookie(args)
    except FileNotFoundError as e:
        missing = e.args[0] if e.args else args.cookie_file
        print(f"× 找不到 Cookie 文件：{missing}", file=sys.stderr)
        print("  请新建该文件并粘贴你的完整 Cookie（可复制同目录 cookie.txt.example 改名）。", file=sys.stderr)
        sys.exit(2)

    if not cookie:
        print("× Cookie 为空：请在 cookie.txt 里粘贴完整的登录 Cookie。", file=sys.stderr)
        print("  获取方法：浏览器登录 douyin.com -> F12 -> Network -> 任一请求 -> Headers -> Cookie。", file=sys.stderr)
        sys.exit(2)

    if "sessionid" not in cookie and "odin_tt" not in cookie:
        print("[警告] Cookie 中未发现 sessionid/odin_tt，可能不是登录态，接口多半会失败。", file=sys.stderr)
        print("       请确认复制的是【已登录】状态下的完整 Cookie。", file=sys.stderr)

    args.cookie = cookie
    run(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已手动中断（下次运行会自动断点续跑）。")
        sys.exit(130)
