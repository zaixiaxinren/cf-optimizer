FROM alpine:3.20

# ---- 镜像元数据：Docker Hub / GHCR 页面会展示这些信息 ----
LABEL org.opencontainers.image.title="cf-optimizer" \
      org.opencontainers.image.description="Cloudflare 优选 IP 自动测速 + 一键推送到 CFNext 面板，带 Web 控制台" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/zaixiaxinren/cf-optimizer" \
      org.opencontainers.image.documentation="https://github.com/zaixiaxinren/cf-optimizer/blob/main/README.md" \
      org.opencontainers.image.url="https://hub.docker.com/r/zaixiaxinren/cf-optimizer"

# buildx 注入的目标架构，普通 docker build 时默认为 amd64
ARG TARGETARCH=amd64
ARG TARGETVARIANT=""
# 固定版本可复现构建：--build-arg CFST_VERSION=2.3.5，留空则取最新
ARG CFST_VERSION=""

# jq 用于处理 CFNext 面板返回的 JSON；python3 用于 Web 控制台（标准库即可，无需 pip）
RUN apk add --no-cache bash curl ca-certificates tzdata jq python3

COPY . /src

# CloudflareST 二进制有三种来源，优先级从高到低：
#   1) 构建上下文里自带的 cfst / CloudflareST（离线可用）
#   2) 按目标架构从 GitHub 下载（发布包名形如 cfst_linux_arm64.tar.gz）
# 国内构建可加 --build-arg HTTPS_PROXY=http://<代理>:7890
RUN set -eux; \
    if [ -f /src/cfst ]; then \
      echo "[build] 使用构建上下文里的 cfst"; \
      install -m755 /src/cfst /usr/local/bin/cfst; \
    elif [ -f /src/CloudflareST ]; then \
      echo "[build] 使用构建上下文里的 CloudflareST"; \
      install -m755 /src/CloudflareST /usr/local/bin/cfst; \
    else \
      # arm 的发布包按 ARMv7 命名，armv7 -> armv7，armv6 -> armv6
      arch="${TARGETARCH}"; \
      if [ "$arch" = "arm" ]; then arch="arm${TARGETVARIANT}"; fi; \
      case "$arch" in \
        amd64|arm64|armv6|armv7|386) ;; \
        *) echo "[build] 不支持的架构: $arch（可自带 cfst 二进制绕过）" >&2; exit 1 ;; \
      esac; \
      base="https://github.com/XIU2/CloudflareSpeedTest/releases"; \
      if [ -n "$CFST_VERSION" ]; then \
        base="$base/download/v${CFST_VERSION#v}"; \
      else \
        base="$base/latest/download"; \
      fi; \
      echo "[build] 从 GitHub 下载 cfst_linux_${arch}.tar.gz"; \
      curl -fsSL --retry 3 -o /tmp/cfst.tgz "${base}/cfst_linux_${arch}.tar.gz" \
        || curl -fsSL --retry 3 -o /tmp/cfst.tgz "${base}/CloudflareST_linux_${arch}.tar.gz"; \
      tar -xzf /tmp/cfst.tgz -C /tmp; \
      install -m755 "$(find /tmp -maxdepth 2 -type f \( -name cfst -o -name CloudflareST \) | head -n1)" \
        /usr/local/bin/cfst; \
      rm -rf /tmp/cfst.tgz /tmp/cfst_linux_* /tmp/CloudflareST_linux_* /tmp/cfst /tmp/CloudflareST; \
    fi; \
    command -v cfst >/dev/null || { echo "[build] cfst 安装失败" >&2; exit 1; }

# 兜底去掉可能的 CRLF，否则 shebang 会报错；装好脚本和 Web 静态页；最后清掉构建上下文
RUN sed -i 's/\r$//' /src/run.sh /src/entrypoint.sh /src/push_panel.sh /src/server.py \
      /src/resolve.py /src/format_result.py /src/dltest.py /src/domaintest.py \
 && install -m755 /src/run.sh /run.sh \
 && install -m755 /src/entrypoint.sh /entrypoint.sh \
 && install -m755 /src/push_panel.sh /push_panel.sh \
 && install -m755 /src/server.py /server.py \
 && install -m755 /src/resolve.py /resolve.py \
 && install -m755 /src/format_result.py /format_result.py \
 && install -m755 /src/dltest.py /dltest.py \
 && install -m755 /src/domaintest.py /domaintest.py \
 && mkdir -p /static \
 && cp -r /src/static/. /static/ \
 && rm -rf /src

ENV TZ=Asia/Shanghai \
    WEB_PORT=8090

EXPOSE 8090

# 容器内探活：Web 服务能响应就算健康（docker ps 会显示 healthy）
HEALTHCHECK --interval=60s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${WEB_PORT}/api/health" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
