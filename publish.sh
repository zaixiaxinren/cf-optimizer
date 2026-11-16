#!/usr/bin/env bash
# 本地构建并推送到 Docker Hub（不想配 GitHub Actions 时用这个）
#
# 用法：
#   ./publish.sh -u <dockerhub用户名> -p <版本号> [-m] [-r 仓库名]
#
#   -u  你的 Docker Hub 用户名（必填）
#   -p  要打的版本标签，如 1.0.0（默认 latest）
#   -m  构建多架构（amd64 + arm64），需要 QEMU，首次会自动安装
#   -r  仓库名，默认 cf-optimizer
#
# 例子：
#   ./publish.sh -u wangbo -p 1.0.0          # 只构建本机架构，快
#   ./publish.sh -u wangbo -p 1.0.0 -m       # 构建 amd64 + arm64，慢但通用
#
# 前置：先 docker login（用 Access Token，不要用登录密码）
set -euo pipefail

REPO="cf-optimizer"
USER=""
TAG="latest"
MULTI=0

while getopts ":u:p:r:mh" opt; do
  case "$opt" in
    u) USER="$OPTARG" ;;
    p) TAG="$OPTARG" ;;
    r) REPO="$OPTARG" ;;
    m) MULTI=1 ;;
    h) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数，用 -h 看用法" >&2; exit 1 ;;
  esac
done

[ -n "$USER" ] || { echo "错误：必须用 -u 指定 Docker Hub 用户名" >&2; exit 1; }

cd "$(dirname "$0")"

# ---- 1. 检查登录状态 ----
if ! docker info 2>/dev/null | grep -q 'Username:'; then
  echo "==> 还没登录 Docker Hub，先执行："
  echo "    docker login -u $USER"
  echo "    （密码处填 Access Token，在 https://hub.docker.com/settings/security 生成）"
  exit 1
fi
echo "==> 已登录：$(docker info 2>/dev/null | grep 'Username:' | awk '{print $2}')"

# ---- 2. 决定平台 ----
if [ "$MULTI" = "1" ]; then
  PLATFORMS="linux/amd64,linux/arm64"
  echo "==> 多架构模式：$PLATFORMS"
  if ! ls /proc/sys/fs/binfmt_misc/qemu-aarch64 >/dev/null 2>&1; then
    echo "==> 安装 QEMU 模拟器（构建 arm64 用）"
    docker run --privileged --rm tonistiigi/binfmt --install arm64 >/dev/null
  fi
  BUILDER="cfopt-builder"
  if ! docker buildx inspect "$BUILDER" >/dev/null 2>&1; then
    docker buildx create --name "$BUILDER" --driver docker-container >/dev/null
  fi
  docker buildx use "$BUILDER"
  PUSH_FLAG="--push"
else
  PLATFORMS="linux/$(docker version -f '{{.Server.Arch}}' 2>/dev/null || echo amd64)"
  echo "==> 单架构模式：$PLATFORMS"
  BUILDER=""
  PUSH_FLAG="--push"
fi

FULL="$USER/$REPO"

# ---- 3. 构建并推送 ----
echo "==> 构建 $FULL:$TAG"
ARGS=(buildx build
  --platform "$PLATFORMS"
  --tag "$FULL:$TAG"
  --tag "$FULL:latest"
  --build-arg "CFST_VERSION=${CFST_VERSION:-}"
  $PUSH_FLAG
  .)

docker "${ARGS[@]}"

echo
echo "==> 完成。别人可以直接拉取："
echo "    docker pull $FULL:$TAG"
echo
echo "==> 查看多架构清单："
echo "    docker buildx imagetools inspect $FULL:$TAG"
