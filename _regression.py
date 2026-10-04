# -*- coding: utf-8 -*-
"""回归用例：覆盖核心下载引擎 + Web API + 安全/边界。
用本地 mock 模拟抖音接口与直链，不访问真实网络。"""
import importlib.util, json, os, re, subprocess, tempfile, threading, time, urllib.request, urllib.parse, urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("webui", os.path.join(ROOT, "webui.py"))
webui = importlib.util.module_from_spec(spec); spec.loader.exec_module(webui)
core = webui.core

PASS = []
def ok(name, cond, extra=""):
    assert cond, f"FAIL: {name} {extra}"
    PASS.append(name)

VIDEO = b"\x00\x00\x00\x18ftypmp42" + b"V" * 5000
IMG = b"\xff\xd8\xff" + b"I" * 800
PAGES = {}   # cursor -> aweme_list

def aweme_video(vid, desc, authors=("甲作者", "11111111111")):
    return {"aweme_id": vid, "desc": desc,
            "author": {"nickname": authors[0], "uid": authors[1],
                       "avatar_larger": {"url_list": [f"http://127.0.0.1:{PORT}/a/{authors[1]}.jpg"]}},
            "video": {"bit_rate": [{"is_h265": 0, "bit_rate": 3000, "format": "mp4",
                     "play_addr": {"url_list": [f"http://127.0.0.1:{PORT}/v/{vid}.mp4"]}}],
                     "cover": {"url_list": [f"http://127.0.0.1:{PORT}/c/{vid}.jpg"]}}}

def aweme_image(vid, desc, authors=("乙作者", "22222222222"), n=3):
    return {"aweme_id": vid, "desc": desc,
            "author": {"nickname": authors[0], "uid": authors[1]},
            "video": {},
            "images": [{"url_list": [f"http://127.0.0.1:{PORT}/i/{vid}_{i}.jpg"]} for i in range(1, n + 1)]}

class Mock(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        if p.path.endswith("/aweme/favorite"):
            cur = urllib.parse.parse_qs(p.query).get("max_cursor", ["0"])[0]
            body = json.dumps(PAGES.get(cur, {"status_code": 0, "has_more": 0, "max_cursor": "0", "aweme_list": []})).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        elif p.path.startswith("/v/"):
            self.send_response(200); self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(VIDEO))); self.end_headers(); self.wfile.write(VIDEO)
        elif p.path.startswith("/i/") or p.path.startswith("/c/") or p.path.startswith("/a/"):
            self.send_response(200); self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(IMG))); self.end_headers(); self.wfile.write(IMG)
        else:
            self.send_response(404); self.end_headers()

mock = HTTPServer(("127.0.0.1", 0), Mock); PORT = mock.server_address[1]
threading.Thread(target=mock.serve_forever, daemon=True).start()
core.FAVORITE_API = f"http://127.0.0.1:{PORT}/aweme/v1/web/aweme/favorite"

td = tempfile.mkdtemp(); out = os.path.join(td, "out")
webui.load_config(os.path.join(td, "config.json"))
webui.LOG_FILE = os.path.join(td, "webui.log")
webui.CONFIG.update({"sec_user_id": "MS4wTEST", "cookie": "sessionid=x; ttwid=z", "out": out,
                     "count": 18, "codec": 264, "min_interval": 0, "max_interval": 0,
                     "page_delay_min": 0, "page_delay_max": 0, "images": True, "no_cover": False,
                     "no_avatar": False, "max_videos": 0, "max_pages": 0, "dry_run": False,
                     "caption_untitled": True, "password": ""})

srv = ThreadingHTTPServer(("127.0.0.1", 0), webui.Handler); HP = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
B = f"http://127.0.0.1:{HP}"
def gj(u, hdrs=None):
    r = urllib.request.Request(B + u, headers=hdrs or {})
    try:
        with urllib.request.urlopen(r) as resp: return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e: return e.code, e.read(), dict(e.headers or {})
def pj(u, d=None, hdrs=None):
    h = {"Content-Type": "application/json"}; h.update(hdrs or {})
    r = urllib.request.Request(B + u, data=json.dumps(d or {}).encode(), headers=h)
    try:
        with urllib.request.urlopen(r) as resp: return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read())
def pjx(u, d=None, hdrs=None):
    h = {"Content-Type": "application/json"}; h.update(hdrs or {})
    r = urllib.request.Request(B + u, data=json.dumps(d or {}).encode(), headers=h)
    try:
        with urllib.request.urlopen(r) as resp: return resp.status, json.loads(resp.read()), dict(resp.headers)
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read()), dict(e.headers or {})
def wait_done(t=25):
    for _ in range(int(t / 0.2)):
        time.sleep(0.2)
        if not json.loads(gj("/api/state")[1])["state"]["running"]: return
    raise AssertionError("run timeout")

# ---------- 1) 核心：下载 / 去重 / 增量 ----------
PAGES["0"] = {"status_code": 0, "has_more": 1, "max_cursor": "100",
              "aweme_list": [aweme_video("111", "视频一"), aweme_image("222", "", n=3)]}
PAGES["100"] = {"status_code": 0, "has_more": 0, "max_cursor": "0",
                "aweme_list": [aweme_video("333", "视频三", ("丙作者", "33333333333"))]}
st, j = pj("/api/start", {"config": dict(webui.CONFIG)}); ok("start", st == 200 and j["ok"])
wait_done()
s = json.loads(gj("/api/state")[1])["state"]
ok("下载成功 2 视频", s["stats"]["ok"] == 2, s["stats"])
ok("图文 1 条", s["stats"]["image"] == 1, s["stats"])
vids = json.loads(gj("/api/videos?size=100")[1])
ok("列表 3 条(2视频1图文)", vids["total"] == 3, vids["total"])
img = [i for i in vids["items"] if i["type"] == "image"][0]
ok("图文聚合3张", img["count"] == 3 and len(img["files"]) == 3)
ok("图文生成文案", ("黑丝" in img["title_disp"] or "丝袜" in img["title_disp"]), img["title_disp"])

st, j = pj("/api/start", {"config": dict(webui.CONFIG)}); wait_done()
s2 = json.loads(gj("/api/state")[1])["state"]
ok("重跑不重复下载", s2["stats"]["ok"] == 0 and s2["stats"]["dup"] >= 2, s2["stats"])

# ---------- 2) 类型筛选 / 排序 ----------
ok("type=video", json.loads(gj("/api/videos?type=video")[1])["total"] == 2)
ok("type=image", json.loads(gj("/api/videos?type=image")[1])["total"] == 1)
sj = json.loads(gj("/api/videos?sort=size_asc")[1])["items"]
ok("sort=size_asc 递增", all(sj[i]["size"] <= sj[i + 1]["size"] for i in range(len(sj) - 1)))
ok("搜索命中作者", json.loads(gj("/api/videos?q=" + urllib.parse.quote("丙作者"))[1])["total"] == 1)

# ---------- 3) Range 播放（含畸形/后缀/多段/越界） ----------
rel = [i for i in vids["items"] if i["type"] == "video"][0]["rel"]
ps = "/play?p=" + urllib.parse.quote(rel)
sc, body, hdr = gj(ps, {"Range": "bytes=0-99"})
ok("Range 206", sc == 206 and len(body) == 100 and hdr.get("Content-Range", "").startswith("bytes 0-99/"))
sc, body, hdr = gj(ps, {"Range": "bytes=-100"})
ok("suffix Range", sc == 206 and len(body) == 100 and "Content-Range" in hdr)
sc, body, _ = gj(ps, {"Range": "bytes=abc-10"})     # 畸形 -> 应忽略(200整体)
ok("畸形 Range 不崩", sc == 200 and len(body) == len(VIDEO), sc)
sc, body, _ = gj(ps, {"Range": "bytes=0-10,20-30"})  # 多段 -> 取第一段
ok("多段 Range 取首段", sc == 206 and len(body) == 11, sc)
sc, _, hdr = gj(ps, {"Range": "bytes=999999999-"})   # 越界
ok("越界 Range 416", sc == 416, sc)

# ---------- 4) 路径穿越 ----------
sc, _, _ = gj("/play?p=" + urllib.parse.quote("../../../../etc/passwd"))
ok("路径穿越 404", sc == 404, sc)
sc, _, _ = gj("/file?p=" + urllib.parse.quote("..\\..\\windows\\win.ini"))
ok("反斜杠穿越 404", sc == 404, sc)

# ---------- 5) 删除本地文件（删完可重新下载） ----------
st, j = pj("/api/delete", {"files": img["files"], "path": img["rel"]})
ok("删除图文相册", j["ok"] and len(j["removed"]) == 3, j)
img_dir = os.path.join(out, img["rel"].split("/")[0], img["rel"].split("/")[1])
ok("相册目录已清理", not os.path.isdir(img_dir), img_dir)
exc = os.path.join(out, ".dystate", "excluded_ids.txt")
ok("删除不写忽略名单",
   not os.path.isfile(exc) or "222" not in open(exc, encoding="utf-8").read())
PAGES["0"] = {"status_code": 0, "has_more": 0, "max_cursor": "0",
              "aweme_list": [aweme_image("222", "", n=3)]}
pj("/api/start", {"config": dict(webui.CONFIG)}); wait_done()
s3 = json.loads(gj("/api/state")[1])["state"]
ok("删除后可重新下载", s3["stats"]["image"] == 1 and s3["stats"]["skip"] == 0, s3["stats"])
left = json.loads(gj("/api/videos?size=100")[1])
ok("重新下载后列表恢复 3 条", left["total"] == 3, left["total"])

# ---------- 6) 配置 / 必填 / 日志持久化 ----------
sc, j = pj("/api/start", {"config": {"sec_user_id": "", "cookie": "x", "out": "/x"}})
ok("必填校验 sec_user_id", sc == 400 and not j["ok"])
sc, j = pj("/api/start", {"config": {"sec_user_id": "x", "cookie": "", "out": "/x"}})
ok("必填校验 cookie", sc == 400)
sc, j = pj("/api/start", {"config": {"sec_user_id": "x", "cookie": "x", "out": ""}})
ok("必填校验 out", sc == 400)
ok("校验失败不污染配置", bool(webui.CONFIG["out"]))
cfg = json.loads(gj("/api/config")[1])
ok("config 返回路径", cfg["config_path"] and cfg["out_resolved"])
ok("日志已落盘", os.path.isfile(webui.LOG_FILE) and os.path.getsize(webui.LOG_FILE) > 0)
lg = json.loads(gj("/api/logs?since=0")[1])
ok("日志序号单调", lg["last_seq"] > 0 and len(lg["lines"]) > 0)
lg2 = json.loads(gj(f"/api/logs?since={lg['last_seq']}")[1])
ok("日志增量不卡死", isinstance(lg2["lines"], list) and lg2["last_seq"] >= lg["last_seq"])

# ---------- 7) 鉴权 ----------
webui.CONFIG["password"] = "secret"
sc, _, _ = gj("/api/state"); ok("未登录 401", sc == 401)
sc, j = pj("/api/login", {"password": "wrong"}); ok("错误密码 401", sc == 401)
sc, j, hdrs = pjx("/api/login", {"password": "secret"})
ok("登录 200", sc == 200)
ck = hdrs.get("Set-Cookie", "")
ok("Cookie 含 HttpOnly/SameSite", "HttpOnly" in ck and "SameSite" in ck, ck)
cookie = ck.split(";")[0]
sc, _, _ = gj("/api/state", {"Cookie": cookie}); ok("带 cookie 200", sc == 200)
webui.CONFIG["password"] = ""

# ---------- 8) 边界：Windows 保留名 ----------
ok("保留名 CON 处理", core.sanitize_name("CON", "x", True) == "_CON", core.sanitize_name("CON", "x", True))
ok("保留名 nul 处理", core.sanitize_name("nul", "x", True).upper() != "NUL")
ok("尾部点空格去除", not core.sanitize_name("abc. ", "x", True).endswith((".", " ")))
ok("空标题回落", core.sanitize_name("", "12345", True) == "12345")

# ---------- 9) 命名：无描述用文案 + 旧目录自动改名（且不重下） ----------
webui.CONFIG["caption_untitled"] = True
cap = core.pretty_title("", "999", True)
ok("无描述生成文案名", cap != "999" and ("黑丝" in cap or "丝袜" in cap), cap)
ok("关闭文案退回作品ID", core.pretty_title("", "999", False) == "999")
ok("有描述保留原描述", core.pretty_title("今天穿搭", "999", True) == "今天穿搭")

outL = os.path.join(td, "outL")
leg = os.path.join(outL, "甲作者11111111111", "777")     # 旧版：纯 ID 目录 + 纯 ID 文件名
os.makedirs(leg, exist_ok=True)
open(os.path.join(leg, "777.mp4"), "wb").write(VIDEO)
open(os.path.join(leg, "777-poster.jpg"), "wb").write(IMG)
open(os.path.join(leg, ".dyid"), "w").write("777")
DL = []
_orig_dl = core.download_file
core.download_file = lambda urls, sp, *a, **k: (DL.append(sp), _orig_dl(urls, sp, *a, **k))[1]

PAGES["0"] = {"status_code": 0, "has_more": 0, "max_cursor": "0",
              "aweme_list": [aweme_video("777", "", ("甲作者", "11111111111"))]}
webui.CONFIG.update({"out": outL, "images": False, "pretty_names": True,
                     "caption_untitled": True, "no_cover": True, "no_avatar": True,
                     "download_timeout": 30, "min_interval": 0, "max_interval": 0,
                     "page_delay_min": 0, "page_delay_max": 0, "max_videos": 0,
                     "max_pages": 0, "password": ""})
pj("/api/start", {"config": dict(webui.CONFIG)}); wait_done()
sL = json.loads(gj("/api/state")[1])["state"]
ok("旧目录改名后不重下", sL["stats"]["ok"] == 0 and sL["stats"]["dup"] >= 1 and not DL, sL["stats"])
capdir = core.pretty_title("", "777", True)
adir = os.path.join(outL, "甲作者")
ok("目录已改为文案名", os.path.isdir(os.path.join(adir, capdir)), os.listdir(adir))
ok("旧纯ID目录已消失", not os.path.isdir(leg))
ok("文件已改为文案名", os.path.isfile(os.path.join(adir, capdir, capdir + ".mp4")),
   os.listdir(os.path.join(adir, capdir)))
ok("封面同步改名", os.path.isfile(os.path.join(adir, capdir, capdir + "-poster.jpg")))
lstL = json.loads(gj("/api/videos?size=100")[1])["items"]
ok("列表标题=新目录名", any(i["title_disp"] == capdir for i in lstL), [i["title_disp"] for i in lstL])
before = sorted(os.listdir(adir))
pj("/api/start", {"config": dict(webui.CONFIG)}); wait_done()
sL2 = json.loads(gj("/api/state")[1])["state"]
ok("重复同步稳定（不再改名/不重下）",
   not DL and sL2["stats"]["dup"] >= 1 and sorted(os.listdir(adir)) == before, sL2["stats"])
core.download_file = _orig_dl

# 旧版纯 ID 目录在列表里的「文案开关」仍然生效
outX = os.path.join(td, "outX")
legX = os.path.join(outX, "乙作者22222222222", "888")
os.makedirs(legX, exist_ok=True)
open(os.path.join(legX, "888.mp4"), "wb").write(VIDEO)
open(os.path.join(legX, ".dyid"), "w").write("888")
webui.CONFIG.update({"out": outX, "caption_untitled": True})


def legacy_disp():
    its = json.loads(gj("/api/videos?size=100")[1])["items"]
    return its[0]["title_disp"] if its else None


cap_on = legacy_disp()
pj("/api/config", {"config": dict(webui.CONFIG, caption_untitled=False)})
cap_off = legacy_disp()
ok("旧目录文案开关即时生效", cap_on != cap_off and cap_off == "888", (cap_on, cap_off))

# ---------- 10) 前端 JS 语法 + 元素引用完整性（防止再次出现“点击无反应”） ----------
def _js_blocks(html):
    return re.findall(r"<script>(.*?)</script>", html, re.S)

html = webui.INDEX_HTML
scripts = _js_blocks(html)
ok("前端含 script 块", len(scripts) >= 1)
js = scripts[-1]
jsp = os.path.join(td, "ui.js")
open(jsp, "w", encoding="utf-8").write(js)
try:
    r = subprocess.run(["node", "--check", jsp], capture_output=True, text=True)
    ok("前端 JS 语法正确(node --check)", r.returncode == 0, (r.stderr or "")[:300])
except FileNotFoundError:
    decls = re.findall(r"\b(?:let|const|var)\s+([A-Za-z_$][\w$]*)\s*=", js)
    dup = sorted({d for d in decls if decls.count(d) > 1})
    ok("前端无重复声明", not dup, dup)

# 关键：JS 里引用的元素 id 必须真实存在，否则运行期 null.value 会让整页按钮失灵
ids = set(re.findall(r'id="([^"]+)"', html))
refs = set(re.findall(r"getElementById\('([^']+)'\)", js)) | set(re.findall(r"\$\('([^']+)'\)", js))
missing = sorted(x for x in refs if x not in ids)
ok("JS 引用的元素都存在", not missing, missing)

# 内联事件里调用的函数必须已定义（否则点击报错）
handlers = set(re.findall(r'on(?:click|input|change|keydown)\s*=\s*"\s*([A-Za-z_$][\w$]*)\s*\(', html))
defined = set(re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", js))
undef = sorted(h for h in handlers if h not in defined and h != "if")
ok("内联事件函数均已定义", not undef, undef)

# 配置页已精简：这些字段已从界面移除，JS 里也不应再引用（配置靠 CURCFG 原样带回）
for token in ("f_min", "f_max", "f_pmin", "f_pmax", "f_mv", "f_mp", "f_pw",
              "f_nocover", "f_noavatar", "f_pretty", "f_dry", "openLogout"):
    ok("已精简字段无残留引用 " + token, token not in scripts[-1])
# 保留的字段必须还在
for token in ("f_sec", "f_cookie", "f_out", "f_count", "f_codec", "f_dlto",
              "f_images", "f_caption"):
    ok("保留字段仍在 " + token, ('id="' + token + '"') in html)

# 「打开文件位置」已整体移除（Windows 专用，NAS 上无效）：前后端都不应再有残留
for token in ("revealAt", "/api/reveal", "reveal_in_file_manager", "📂", "打开文件位置"):
    ok("打开位置已彻底移除 " + token, token not in scripts[-1] and token not in html)
ok("后端无 reveal 函数", not hasattr(webui, "reveal_in_file_manager"))

# 关键控件存在性（功能不被 UI 改动破坏的护栏）
for cid in ("btnStart", "btnStop", "btnTheme", "vbody", "logs", "page-videos", "page-logs",
            "page-config", "f_sec", "f_cookie", "f_out", "player", "playerVideo", "gallery", "galleryGrid"):
    ok("控件存在 " + cid, ('id="' + cid + '"') in html)

# 登录页 JS 也要语法正确
lo = _js_blocks(webui.LOGIN_HTML)
if lo:
    ljp = os.path.join(td, "login.js")
    open(ljp, "w", encoding="utf-8").write(lo[-1])
    try:
        r = subprocess.run(["node", "--check", ljp], capture_output=True, text=True)
        ok("登录页 JS 语法正确", r.returncode == 0, (r.stderr or "")[:200])
    except FileNotFoundError:
        ok("登录页含登录函数", "function login" in lo[-1])

# ---------- 11) 下载进度 / 实时速度（网页可见） ----------
class SlowMock(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        if p.path.startswith("/slow/"):
            size = 256 * 1024
            chunk = b"S" * (16 * 1024)
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            for _ in range(size // len(chunk)):
                self.wfile.write(chunk)
                time.sleep(0.05)          # 故意慢速：约 0.8s 下完，便于采样
        else:
            self.send_response(404); self.end_headers()

slow = ThreadingHTTPServer(("127.0.0.1", 0), SlowMock); SP = slow.server_address[1]
threading.Thread(target=slow.serve_forever, daemon=True).start()
core.FAVORITE_API = f"http://127.0.0.1:{PORT}/aweme/v1/web/aweme/favorite"
out2 = os.path.join(td, "out2")
webui.CONFIG.update({"out": out2, "images": False, "caption_untitled": True, "download_timeout": 30,
                     "min_interval": 0, "max_interval": 0, "page_delay_min": 0, "page_delay_max": 0,
                     "max_videos": 0, "max_pages": 0, "password": ""})


def slow_aweme(vid):
    return {"aweme_id": vid, "desc": "慢速视频",
            "author": {"nickname": "慢速作者", "uid": "44444444444"},
            "video": {"bit_rate": [{"is_h265": 0, "bit_rate": 2000, "format": "mp4",
                     "play_addr": {"url_list": [f"http://127.0.0.1:{SP}/slow/{vid}.mp4"]}}],
                     "cover": {"url_list": []}}}


PAGES["0"] = {"status_code": 0, "has_more": 0, "max_cursor": "0", "aweme_list": [slow_aweme("444")]}
pj("/api/start", {"config": dict(webui.CONFIG)})
saw_dl, max_speed, max_pct, seen_file = False, 0.0, 0.0, ""
for _ in range(150):
    time.sleep(0.08)
    s = json.loads(gj("/api/state")[1])["state"]
    if s.get("stage") == "download" and s.get("file"):
        saw_dl = True; seen_file = s["file"]
        max_speed = max(max_speed, float(s.get("speed") or 0))
        max_pct = max(max_pct, float(s.get("pct") or 0))
    if saw_dl and not s["running"]:
        break
ok("下载中暴露进度阶段", saw_dl, (saw_dl, seen_file))
ok("进度含实时速度", max_speed > 0, max_speed)
ok("进度百分比 >0", max_pct > 0, max_pct)
ok("进度文件名为 mp4", seen_file.endswith(".mp4"), seen_file)
s4 = json.loads(gj("/api/state")[1])["state"]
ok("慢速视频下载成功", s4["stats"]["ok"] >= 1, s4["stats"])
ok("结束后进度已清空", s4.get("file") == "" and float(s4.get("speed") or 0) == 0.0, s4.get("file"))
slow.shutdown()

# ---------- 12) 定时任务（到点自动开始 / 到点自动停止） ----------
from datetime import datetime as _dt
webui.SCHED_STATE.clear()

ok("时间窗·常规在窗内", webui.sched_window("02:00", "06:00", 3 * 60) == (True, False))
ok("时间窗·常规越过结束", webui.sched_window("02:00", "06:00", 7 * 60) == (False, True))
ok("时间窗·跨天在窗内", webui.sched_window("23:00", "02:00", 30) == (True, False))
ok("时间窗·跨天越过结束", webui.sched_window("23:00", "02:00", 5 * 60) == (False, True))

ns = webui.normalize_schedule([{"start": "2:5", "stop": "06:00", "days": [0, 7, "x"]},
                               {"start": "03:00", "stop": "03:00"}, "junk"])
ok("规范化·时间补零", ns and ns[0]["start"] == "02:05", ns)
ok("规范化·丢弃起止相同", len(ns) == 1, ns)
ok("规范化·空星期视为每天",
   webui.normalize_schedule([{"start": "01:00", "stop": "02:00", "days": []}])[0]["days"] == list(range(7)))

out3 = os.path.join(td, "out3")
PAGES["0"] = {"status_code": 0, "has_more": 0, "max_cursor": "0",
              "aweme_list": [aweme_video("555", "定时视频", ("定时作者", "55555555555"))]}
webui.CONFIG.update({"out": out3, "images": False, "password": "", "schedule_enabled": True,
                     "schedule": [{"start": "02:00", "stop": "06:00",
                                   "days": [0, 1, 2, 3, 4, 5, 6], "enabled": True}]})
sc, j = pj("/api/schedule", {"schedule": webui.CONFIG["schedule"], "schedule_enabled": True})
ok("定时设置保存", sc == 200 and j["ok"] and len(j["schedule"]) == 1, j)
ok("下次执行可计算", bool(j["next"]) and j["next"]["start"] == "02:00", j["next"])
st_ = json.loads(gj("/api/state")[1])["state"]
ok("state 带定时信息", st_["schedule_enabled"] is True and bool(st_["schedule_next"]), st_.get("schedule_next"))

# 「配置」表单保存（不含 schedule 字段）不应把定时设置冲掉
pj("/api/config", {"config": {k: v for k, v in webui.CONFIG.items()
                              if k not in ("schedule", "schedule_enabled")}})
ok("表单保存不冲掉定时设置",
   len(webui.CONFIG["schedule"]) == 1 and webui.CONFIG["schedule_enabled"], webui.CONFIG["schedule"])

# 到点自动开始
webui.SCHED_STATE.clear()
with webui.RUN_LOCK:
    webui.RUN["started_at"] = None
webui.sched_tick(now=_dt(2026, 10, 4, 2, 0))
wait_done()
s5 = json.loads(gj("/api/state")[1])["state"]
ok("到点自动开启下载", bool(s5["started_at"]), s5.get("started_at"))
ok("定时触发已记录", webui.SCHED_STATE.get(webui.CONFIG["schedule"][0]["id"], {}).get("started") is True,
   webui.SCHED_STATE)
ok("定时下载成功", (s5["stats"]["ok"] + s5["stats"]["image"]) >= 1, s5["stats"])

# 到点自动停止（模拟任务已在运行）
webui.SCHED_STATE.clear()
webui.SCHED_STATE[str(webui.CONFIG["schedule"][0]["id"])] = {"date": "2026-10-04", "started": True}
webui.STOP_EVENT.clear()
with webui.RUN_LOCK:
    webui.RUN["running"] = True
webui.sched_tick(now=_dt(2026, 10, 4, 6, 0))
ok("到点自动停止（已发信号）", webui.STOP_EVENT.is_set())
with webui.RUN_LOCK:
    webui.RUN["running"] = False
webui.STOP_EVENT.clear()
webui.CONFIG["schedule_enabled"] = False

# ---------- 13) 删除本地文件（删完可重新下载） ----------
outT = os.path.join(td, "outT")
tdir = os.path.join(outT, "测试作者", "测试标题")
os.makedirs(tdir, exist_ok=True)
tfile = os.path.join(tdir, "测试标题.mp4")
open(tfile, "wb").write(VIDEO)
open(os.path.join(tdir, "测试标题-poster.jpg"), "wb").write(IMG)
open(os.path.join(tdir, ".dyid"), "w").write("8888")
rel_t = os.path.relpath(tfile, outT).replace(os.sep, "/")
webui.CONFIG.update({"out": outT, "password": ""})

# 删除 = 只删本地文件；同时清掉该作品的「不再下载」记录 -> 之后可重新下载
exc3 = os.path.join(outT, ".dystate", "excluded_ids.txt")
os.makedirs(os.path.dirname(exc3), exist_ok=True)
open(exc3, "w").write("8888\n9999\n")            # 预置一条属于本作品的忽略记录
st, j = pj("/api/delete", {"files": [rel_t], "path": rel_t})
ok("删除成功（视频+封面）", j["ok"] and len(j["removed"]) == 2, j)
ok("本地文件已删除", not os.path.exists(tfile))
ok("空目录已清理", not os.path.isdir(tdir))
left = open(exc3, encoding="utf-8").read().split()
ok("删除后忽略记录已清除", left == ["9999"], left)

# 重新放回抖音列表 -> 应能重新下载
PAGES["0"] = {"status_code": 0, "has_more": 0, "max_cursor": "0",
              "aweme_list": [aweme_video("8888", "删除后重下", ("测试作者", "99999999999"))]}
webui.CONFIG.update({"out": outT, "pretty_names": True, "images": False,
                     "no_cover": True, "no_avatar": True, "download_timeout": 30,
                     "min_interval": 0, "max_interval": 0, "page_delay_min": 0,
                     "page_delay_max": 0, "max_videos": 0, "max_pages": 0, "password": ""})
pj("/api/start", {"config": dict(webui.CONFIG)}); wait_done()
sT = json.loads(gj("/api/state")[1])["state"]
ok("删除后可重新下载", sT["stats"]["ok"] == 1 and sT["stats"]["dup"] == 0, sT["stats"])
redir = os.path.join(outT, "测试作者", "删除后重下", "删除后重下.mp4")
ok("重下后文件已回归", os.path.isfile(redir), os.listdir(os.path.join(outT, "测试作者")))

# 清空忽略名单
open(exc3, "w").write("1111\n2222\n")
st, j = pj("/api/clear-exclusions", {})
ok("清空忽略名单", j["ok"] and j["cleared"] == 2, j)
ok("忽略名单已清空", open(exc3, encoding="utf-8").read().strip() == "")
ok("state 报告忽略数", json.loads(gj("/api/state")[1])["state"]["excluded_count"] == 0)

# ---------- 14) 作者目录去数字 + 批量删除 ----------
outA = os.path.join(td, "outA")
os.makedirs(outA, exist_ok=True)
ad = core.resolve_author_dir(outA, "椰椰大王", "61774286061", True, None)
ok("作者目录用纯昵称（无数字）", os.path.basename(ad) == "椰椰大王", ad)
ok("作者目录写了归属标记", core._read_author_marker(ad) == "61774286061")
ad_again = core.resolve_author_dir(outA, "椰椰大王", "61774286061", True, None)
ok("同一作者复用同一目录", ad_again == ad, (ad, ad_again))
ad2 = core.resolve_author_dir(outA, "椰椰大王", "99999999999", True, None)
ok("同名不同作者不混目录", os.path.basename(ad2) == "椰椰大王99999999999", ad2)
ok("两个作者目录并存", os.path.isdir(ad) and os.path.isdir(ad2))
ad3 = core.resolve_author_dir(outA, "另一博主", "123", False, None)
ok("关闭美化则保留uid目录", os.path.basename(ad3) == "另一博主123", ad3)

# 旧目录「昵称uid」（无下划线）自动改名为纯昵称
outM = os.path.join(td, "outM")
oldA = os.path.join(outM, "老作者555")
os.makedirs(os.path.join(oldA, "旧标题"), exist_ok=True)
open(os.path.join(oldA, "旧标题", ".dyid"), "w").write("777")
ad4 = core.resolve_author_dir(outM, "老作者", "555", True, None)
ok("旧作者目录自动改名", os.path.basename(ad4) == "老作者" and not os.path.isdir(oldA), ad4)
# 无需含本作品也会迁移（旧目录整体归位）
other = os.path.join(outM, "别人888")
os.makedirs(other, exist_ok=True)
ad5 = core.resolve_author_dir(outM, "别人", "888", True, None)
ok("旧作者目录整体归位", os.path.basename(ad5) == "别人" and not os.path.isdir(other), ad5)
# 属于别人的目录不会被误迁移
foreign = os.path.join(outM, "他人777")
os.makedirs(foreign, exist_ok=True)
open(os.path.join(foreign, ".author"), "w").write("999")
ad6 = core.resolve_author_dir(outM, "他人", "777", True, None)
ok("他人目录不被误迁移", os.path.isdir(foreign) and os.path.basename(ad6) == "他人", ad6)

# 批量删除：一次请求删多个作品
outB = os.path.join(td, "outB")
webui.CONFIG.update({"out": outB, "password": ""})
rels = []
for aid, nm in [("7001", "批量一"), ("7002", "批量二"), ("7003", "批量三")]:
    d = os.path.join(outB, "批量作者", nm)
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, nm + ".mp4"), "wb").write(VIDEO)
    open(os.path.join(d, ".dyid"), "w").write(aid)
    rels.append(os.path.relpath(os.path.join(d, nm + ".mp4"), outB).replace(os.sep, "/"))
# 真实使用中作者目录会带 .author 归属标记，删除后也应被清理
open(os.path.join(outB, "批量作者", ".author"), "w").write("70000000001")
pj("/api/rescan", {})          # 换了输出目录，强制重扫
vB0 = json.loads(gj("/api/videos?size=100")[1])["total"]
ok("批量删除前 3 条", vB0 == 3, vB0)
st, j = pj("/api/delete", {"files": rels, "path": rels[0]})
ok("批量删除 3 个作品", j["ok"] and len(j["removed"]) == 3, j)
ok("批量删除后空目录已清理", not os.path.isdir(os.path.join(outB, "批量作者")))
vB = json.loads(gj("/api/videos?size=100")[1])
ok("批量删除后列表为空", vB["total"] == 0, vB["total"])
# 批量删除同样可重新下载
PAGES["0"] = {"status_code": 0, "has_more": 0, "max_cursor": "0",
              "aweme_list": [aweme_video("7001", "批量一", ("批量作者", "70000000001"))]}
pj("/api/start", {"config": dict(webui.CONFIG)}); wait_done()
sB = json.loads(gj("/api/state")[1])["state"]
ok("批量删除后可重新下载", sB["stats"]["ok"] == 1, sB["stats"])

# ---------- 15) CDN 主机不可达：快速失败并换地址 ----------
ok("直链按主机分散", core.pick_url_list({"url_list": [
    "https://a.example.com/1", "https://a.example.com/2", "https://b.example.com/1"]})
   == ["https://a.example.com/1", "https://b.example.com/1", "https://a.example.com/2"])
ok("无主机时预检放行", core.preflight_host("", 443, 1) is True)
ok("未监听端口预检失败", core.preflight_host("127.0.0.1", 1, 2) is False)
ok("host_port 解析", core.host_port_of("https://x.example.com/a") == ("x.example.com", 443)
   and core.host_port_of("http://127.0.0.1:8080/a") == ("127.0.0.1", 8080))

outF = os.path.join(td, "outFail")
webui.CONFIG.update({"out": outF, "pretty_names": True, "images": False, "no_cover": True,
                     "no_avatar": True, "download_timeout": 10, "min_interval": 0,
                     "max_interval": 0, "page_delay_min": 0, "page_delay_max": 0,
                     "max_videos": 0, "max_pages": 0, "password": ""})
BAD = "http://127.0.0.1:1/dead.mp4"
GOOD = f"http://127.0.0.1:{PORT}/v/failover.mp4"
PAGES["0"] = {"status_code": 0, "has_more": 0, "max_cursor": "0", "aweme_list": [{
    "aweme_id": "9001", "desc": "换主机测试",
    "author": {"nickname": "换主机作者", "uid": "90000000001"},
    "video": {"bit_rate": [{"is_h265": 0, "bit_rate": 2000, "format": "mp4",
             "play_addr": {"url_list": [BAD, GOOD]}}], "cover": {"url_list": []}}}]}
_t0 = time.time()
pj("/api/start", {"config": dict(webui.CONFIG)}); wait_done()
_dt = time.time() - _t0
sF = json.loads(gj("/api/state")[1])["state"]
ok("不可达地址跳过后仍下载成功", sF["stats"]["ok"] == 1, sF["stats"])
ok("失败切换足够快(<15s)", _dt < 15, "%.1fs" % _dt)
_fdir = os.path.join(outF, "换主机作者", "换主机测试")
ok("换主机后文件已落盘", os.path.isfile(os.path.join(_fdir, "换主机测试.mp4")),
   os.listdir(_fdir) if os.path.isdir(_fdir) else "无")
_logF = open(os.path.join(outF, "dy_favorite_dl.log"), encoding="utf-8", errors="ignore").read()
ok("日志记录连接中", "正在连接" in _logF)
ok("日志记录主机不可达", "主机不可达" in _logF)

# ---------- 15b) 启动自检：目录不可写时不应崩溃（容器重启循环的根因） ----------
ok("可写目录自检通过", webui.ensure_writable(os.path.join(td, "wt_ok"), "输出目录") is None)
_notdir = os.path.join(td, "wt_file")
open(_notdir, "w").write("x")          # 用文件占住父路径，makedirs 必然失败
ok("路径冲突时自检返回提示", isinstance(webui.ensure_writable(os.path.join(_notdir, "sub"), "输出目录"), str))
ok("state 暴露环境告警字段",
   isinstance(json.loads(gj("/api/state")[1])["state"].get("warnings"), list))
ok("界面存在环境告警条", 'id="warnBar"' in html)
ok("界面存在输出目录提示", 'id="outHint"' in html and "renderOutHint" in scripts[-1])

# 容器里输出目录填成宿主机路径时必须告警（这正是"下载找不到"的坑）
_out_saved = webui.CONFIG.get("out")
_env_saved = os.environ.get("OUT_DIR")
os.environ["OUT_DIR"] = "/app/data"
try:
    webui.CONFIG["out"] = "/app/data"
    ok("输出目录为挂载点时无告警", webui.dyn_warnings() == [], webui.dyn_warnings())
    webui.CONFIG["out"] = "/vol1/1000/影视/抖音/喜欢"
    _ws = webui.dyn_warnings()
    ok("填宿主机路径时给出告警", bool(_ws) and "/app/data" in _ws[0], _ws)
    ok("告警说明文件在容器内部", bool(_ws) and "容器内部" in _ws[0], _ws)
    ok("state 暴露 mount_out", json.loads(gj("/api/state")[1])["state"]["mount_out"] == "/app/data")
finally:
    webui.CONFIG["out"] = _out_saved
    if _env_saved is None:
        os.environ.pop("OUT_DIR", None)
    else:
        os.environ["OUT_DIR"] = _env_saved

mock.shutdown(); srv.shutdown()
print("\n".join("PASS " + p for p in PASS))
print(f"\n==== 全部 {len(PASS)} 项回归通过 ====")
