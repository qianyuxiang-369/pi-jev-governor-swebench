#!/bin/sh
# 用 pi-jev-governor 扩展启动 pi，并在启动前 source 本地环境变量。
#
# 用法（在任意草稿项目目录下）：
#   <仓库路径>/scripts/run.sh [额外的 pi 参数...]
# 例如全旁路冒烟：
#   <仓库路径>/scripts/run.sh --no-jev
# 模型路由的作用域由 --models（复数）定义，三档全列即可自由切换；想锁定单模型，
# 把它作为 --models 的唯一条目（超出作用域的档位切换会被 scopedModels 守卫跳过；
# --model 单数只设定初始模型，不构成作用域）：
#   <仓库路径>/scripts/run.sh --models qwen/qwen3.7-plus,qwen/qwen3.7-flash,qwen/qwen3.7-max
#
# 密钥只放在仓库根目录的 .env.local（已 gitignore）。
#
# pi 版本：优先使用项目本地 node_modules/.bin/pi（devDependencies 钉住的 1.1.0，
# 与扩展 API 基线一致）；本地缺失时退回 PATH 上的全局 pi。
# 注意：pi 0.84.x 缺少 sessionManager.buildSessionProjection 等 1.1.0 API，
# 用旧版启动时扩展会在加载时报 "Extension error" 并被静默跳过（agent 照常运行但
# governor 完全旁路，无决策日志）。

set -ea

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if [ ! -f "$ROOT/.env.local" ]; then
	echo "error: $ROOT/.env.local 不存在。先执行：" >&2
	echo "  cp $ROOT/.env.local.example $ROOT/.env.local  # 然后填入真实 key" >&2
	exit 1
fi

. "$ROOT/.env.local"

PI_BIN="$ROOT/node_modules/.bin/pi"
if [ ! -x "$PI_BIN" ]; then
	echo "warn: $ROOT/node_modules/.bin/pi 不存在，退回全局 pi；若版本 < 1.1.0 扩展会加载失败" >&2
	PI_BIN="pi"
fi

exec "$PI_BIN" -e "$ROOT" "$@"
