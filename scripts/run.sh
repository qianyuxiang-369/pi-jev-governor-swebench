#!/bin/sh
# 用 pi-jev 扩展启动 pi，并在启动前 source 本地环境变量。
#
# 用法（在任意草稿项目目录下）：
#   /Users/xqy/Downloads/项目/pi-jev/scripts/run.sh [额外的 pi 参数...]
# 例如全旁路冒烟：
#   /Users/xqy/Downloads/项目/pi-jev/scripts/run.sh --no-jev
# 交互使用建议显式指定 --model（否则 pi 用自己的默认模型，可能未配置鉴权）：
#   /Users/xqy/Downloads/项目/pi-jev/scripts/run.sh --model qwen/qwen3.7-plus
#
# 密钥只放在仓库根目录的 .env.local（已 gitignore）。

set -ea

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if [ ! -f "$ROOT/.env.local" ]; then
	echo "error: $ROOT/.env.local 不存在。先执行：" >&2
	echo "  cp $ROOT/.env.local.example $ROOT/.env.local  # 然后填入真实 key" >&2
	exit 1
fi

. "$ROOT/.env.local"

exec pi -e "$ROOT" "$@"
