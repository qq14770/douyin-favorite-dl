# 抖音「喜欢视频」批量下载 · Web 面板
# 仅使用 Python 标准库，无需 pip 安装任何依赖，镜像很小。
FROM python:3.12-alpine

LABEL org.opencontainers.image.title="dy-liked-dl" \
      org.opencontainers.image.description="抖音喜欢视频批量下载 + Web 面板（飞牛OS/NAS 可用）"

WORKDIR /app

# 只复制程序本体（cookie/config 等敏感文件不打包进镜像，见 .dockerignore）
COPY dy_favorite_dl.py /app/dy_favorite_dl.py
COPY webui.py /app/webui.py

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    LANG=C.UTF-8 \
    PORT=8090 \
    OUT_DIR=/app/data \
    TZ=Asia/Shanghai

# 数据目录：配置 / 状态 / 已下载视频都在这里，映射到飞牛磁盘
VOLUME ["/app/data"]
EXPOSE 8090

# 健康检查（命中首页即视为正常，首页在未登录时会返回登录页）
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:${PORT}/')" || exit 1

# 启动 Web 面板；PASS 环境变量可选（设置后需要登录）
# 用 set -- + exec 的写法，密码含特殊字符/空格也不会被拆错
CMD set -- python webui.py --host 0.0.0.0 --port "${PORT}" --out "${OUT_DIR}" \
      --config "${OUT_DIR}/config.json" \
    && if [ -n "${PASS}" ]; then set -- "$@" --password "${PASS}"; fi \
    && exec "$@"
