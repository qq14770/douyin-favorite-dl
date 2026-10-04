# 飞牛OS（fnOS）/ NAS Docker 部署说明

> 从 Windows 本地版**平移**到飞牛OS，**不需要改任何代码**，只需要改 3 个地方。

---

## 一、结论：可以直接平移

| 检查项 | 结论 |
|---|---|
| 依赖 | 纯 Python 标准库，**无需 pip 安装任何东西** |
| 跨平台代码 | **无任何 Windows 专用调用**（原「📂 打开文件位置」已整体移除），Linux 上行为与 Windows 完全一致 |
| 硬编码 Windows 路径 | 无 |
| 镜像体积 | `python:3.12-alpine` + 两个 .py，约 **60 MB** |
| 内存占用 | 下载单线程，**512 MB 足够**（128 MB 也能跑） |
| 中文输出 | Dockerfile 已加 `PYTHONIOENCODING=utf-8` + `LANG=C.UTF-8`，日志中文正常 |

---

## 二、需要改的 3 个地方

全部在 `docker-compose.yml` 里：

### 1️⃣ 磁盘映射路径（必改）

```yaml
volumes:
  - "/vol1/1000/影视/抖音/喜欢":/app/data   # ← 左边改成你自己的目录（带中文/空格要加引号）
```
- 左边 = 飞牛上的真实目录（**要改**）
- 右边 = 容器内目录（**永远不要改**）
- 视频、封面、配置、断点状态都存这里，删容器不丢数据

### 2️⃣ 运行用户 UID（强烈建议改）

```yaml
user: "1000:1000"      # ← 改成你自己的 UID
```
- 不加这行，容器以 **root** 运行 → 下载的文件在飞牛文件管理器里**可能删不掉/改不了**
- 查自己的 UID：飞牛 SSH 里执行 `id 你的用户名`（首个用户一般是 `1000`）
- 如果加上后报权限错误，说明目录属主对不上，就把它注释掉（回退到 root）

### 3️⃣ 访问密码（强烈建议设置）

```yaml
- PASS=改成你的密码
```
- **未设密码时，同一局域网任何人打开网页都能读到你的完整抖音 Cookie、删除文件、发起下载**
- 支持任意特殊字符（启动命令已做安全的引号处理）

---

## 三、部署步骤

### 方式 A：飞牛图形界面（推荐）

1. 把整个 `tools/dy_favorite_dl` 文件夹上传到飞牛，例如
   `/vol1/1000/docker/dy-liked-dl`
   （只需 `dy_favorite_dl.py`、`webui.py`、`Dockerfile`、`docker-compose.yml`、`.dockerignore` 这 5 个）
2. 飞牛 **「Docker」→「Compose」→「新建项目」**
3. 把 `docker-compose.yml` 的内容粘进去，改好上面 3 处
4. 启动 → 浏览器打开 `http://飞牛IP:8090`

### 方式 B：SSH 命令行

```bash
cd /vol1/1000/docker/dy-liked-dl
docker compose up -d --build
docker compose logs -f --tail=50      # 看启动日志
```

### 方式 C：不在 NAS 上构建（Windows 上构建好再搬）

```bash
# 在 Windows 上
docker build -t dy-liked-dl:latest .
docker save dy-liked-dl:latest -o dy-liked-dl.tar

# 传到飞牛后
docker load -i dy-liked-dl.tar
```
然后把 compose 里的 `build: .` 换成 `image: dy-liked-dl:latest`。

---

## 四、首次配置清单

打开 `http://飞牛IP:8090` → 「配置」标签：

| 字段 | 填什么 |
|---|---|
| `sec_user_id` | 你的抖音 sec_user_id（MS4w 开头那串） |
| `Cookie` | 浏览器 F12 → Network → 任一请求 → Headers → 完整 Cookie |
| **输出目录** | ⚠️ **必须是 `/app/data`**，不是 `/vol1/1000/影视/抖音/喜欢` |
| 每页条数 | 18（默认） |
| 优先编码 | H.264（默认，兼容性最好） |
| 保存图文作品 | 想在列表里看到图文就勾上 |
| 空标题自动生成文案 | 勾上（无描述的作品会用文案命名） |

> ### ⚠️ 「输出目录」为什么填 `/app/data`
>
> ```
> volumes:
>   - /vol1/1000/影视/抖音/喜欢:/app/data
>        ↑ 飞牛上的真实路径            ↑ 容器里的路径
> ```
>
> - 只有**右边**的 `/app/data` 才是"映射出去"的目录，网页里要填它；
> - 填**左边**的宿主机路径 → 容器里不存在这个映射 → 文件写进容器内部 → **飞牛文件管理器里当然找不到**；
> - 新版本会在网页顶部红色条**直接警告**这个错误，并在输入框下方给「一键改回」。

> `sec_user_id` / Cookie **不需要从 Windows 拷过来**，在飞牛网页上直接粘贴即可。
> 容器读的是 `/app/data/config.json`，你 Windows 上的 `D:\抖音\喜欢` 不会被带过去。

配置无误后点 **「💾 保存并开始」**。

---

## 五、NAS 上与 Windows 上的功能差异

**没有了 —— 完全一致。** 以下能力在飞牛上均可正常使用：

| 功能 | 说明 |
|---|---|
| 下载 / 断点续传 / 增量抓新 | ✅ |
| 多选批量删除（删完可重新下载） | ✅ |
| 命名整理（作者/作品去数字、旧目录自动改名） | ✅ |
| 定时任务（到点自动开始 / 自动停止） | ✅ 需容器常驻 |
| 网页播放、进度条 + 实时速度 | ✅ |
| 光/暗主题、多选、搜索、排序 | ✅ |

> 视频落在飞牛磁盘上，直接用**飞牛文件管理器 / Emby / Jellyfin** 打开即可。

---

## 六、常见问题

| 现象 | 原因 / 解决 |
|---|---|
| **容器一直「重启中」** | 启动崩了。① `docker compose logs --tail=50` 看报错；② 九成是 `volumes` 左边目录不存在/不可写：`ls -ld "/vol1/1000/影视/抖音/喜欢" \|\| mkdir -p "/vol1/1000/影视/抖音/喜欢"`；③ 若启用了 `user:` 行，多半是 UID 不匹配，先注释掉；④ 确认已用**最新代码**（新版本对目录不可写做了容错，不会再因此崩溃重启） |
| 日志返回 `blocked` | Cookie 缺 `ttwid`。工具会自动补；仍失败就去浏览器重新复制**完整** Cookie |
| 视频下不动，像卡住 | 现在会显示 `正在连接 xxx（第 n/3 次尝试）`——抖音偶尔分发**连不上的 CDN 节点**，工具会 6 秒内自动换节点，日志里能看到 `主机不可达…换下一个地址` |
| 飞牛文件管理器里删不掉视频 | `user:` 的 UID 不对，改成你实际 UID |
| 定时任务没触发 | ① 容器是否在跑（`docker compose ps`）② 飞牛系统时间是否正确 ③ `TZ=Asia/Shanghai` 是否被删 |
| 端口被占用 | 改 `ports` 左边（如 `"18090:8090"`），右边永远保持 8090 |
| 忘记密码 | 改 compose 里 `PASS` 后 `docker compose up -d`；或删掉 `/app/data/config.json` 里的 `password` 字段 |
| 改了代码/参数不生效 | `docker compose up -d --build`（带 `--build` 才会重新构建镜像） |

---

## 七、升级与卸载

**升级代码**：把新的 `dy_favorite_dl.py`、`webui.py` 覆盖进那个目录，然后
```bash
docker compose up -d --build
```
数据目录（`/app/data`）不受影响。

**卸载**：
```bash
docker compose down          # 删容器，保留数据
# 彻底删除（会连视频一起没）：docker compose down -v
```

---

## 八、安全建议

1. **一定要设访问密码**（`PASS=`）—— 面板的 `/api/config` 会返回完整 Cookie
2. 不要把容器端口暴露到公网；如需外网访问请用飞牛的内网穿透 + 反向代理 + HTTPS
3. Cookie 等于登录态，**不要截图/分享**包含 Cookie 的页面
4. 定期（几周）重新复制一次 Cookie，抖音 Cookie 会过期
