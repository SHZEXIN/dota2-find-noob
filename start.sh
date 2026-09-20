#!/usr/bin/env bash
# Dota2 历史同局查询 —— 前台启动脚本（用于首次验证 / 手动排障）
#
# 用途：不装 systemd，直接在前台把服务跑起来，Ctrl+C 即停。
#   优点：报错直接打在屏幕上，便于第一次确认"能不能跑"。
#   正式长期运行请用 install.sh（装成 systemd 服务，开机自启 + 崩溃重启）。
#
# 用法：
#   bash start.sh              # 默认监听 0.0.0.0:8765
#   bash start.sh 9000         # 指定端口
#   PORT=9000 bash start.sh    # 同上，环境变量方式
#
# 前台运行会占住当前终端，另开一个窗口测试，或加 & 放后台：
#   nohup bash start.sh > /var/log/dota2.log 2>&1 &

set -euo pipefail

# ---------- 可调参数 ----------
BIND_HOST="${D2D_HOST:-0.0.0.0}"
PORT="${1:-${D2D_PORT:-8765}}"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$APP_DIR/standalone_server.py"

export TZ="Asia/Shanghai"
export PYTHONUNBUFFERED=1

echo "=============================================="
echo " Dota2 历史同局查询 —— 前台启动"
echo " 程序: $APP"
echo " 监听: $BIND_HOST:$PORT"
echo "=============================================="

# ---------- 自检 ----------
if [ ! -f "$APP" ]; then
  echo "[错误] 找不到 $APP"
  echo "       请确认 standalone_server.py 与 start.sh 在同一目录。"
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "[错误] 未找到 python3。"
  echo "  Ubuntu/Debian: apt update && apt install -y python3"
  exit 1
fi
echo "[ok] python3 $(python3 -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])')"

# 端口占用检查。
#
# 方法选择（实测踩坑后定稿）：
#   - bind 是**唯一权威**判据：Linux 上端口被占用时 bind 必然抛 EADDRINUSE。
#   - connect 探测不可靠：本机实测对**空闲端口也会超时**（被本机代理/防火墙
#     吞掉连接），因此 connect 超时绝不能当作"被占用"。
#   - ss/netstat 输出格式随版本变化，Windows 版还不认 Linux 参数，同样不可靠。
#
# 结论：只信 bind。bind 成功 = 空闲；bind 失败 = 占用。
# 若 bind 因其他原因失败（如权限），报错信息里能看到原因，不会误判成"空闲"。
PORT_STATE="$(python3 - "$BIND_HOST" "$PORT" <<'PYEOF'
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
bind_host = host if host else "0.0.0.0"

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    s.bind((bind_host, port))
    print("FREE")
except OSError as e:
    print("BUSY %s" % (e.strerror or e))
finally:
    s.close()
PYEOF
)" || PORT_STATE="UNKNOWN"

case "$PORT_STATE" in
  FREE*) echo "[ok] 端口 ${PORT} 空闲" ;;
  BUSY*)
    echo "[错误] 端口 ${PORT} 已被占用：${PORT_STATE#BUSY }"
    echo "       换端口:  bash start.sh 9000"
    if command -v ss >/dev/null 2>&1; then
      echo "       占用者:"
      ss -lntp 2>/dev/null | grep ":${PORT}[[:space:]]" || true
    fi
    echo "       或杀死占用进程后重试。"
    exit 1
    ;;
  *)
    echo "[warn] 端口占用检查未能执行，直接尝试启动。"
    ;;
esac

if ! python3 -c "import http.server, urllib.request, json, csv" 2>/dev/null; then
  echo "[错误] Python 标准库不完整（http.server / urllib.request 无法导入）"
  echo "       可能是裁剪版 Python，请安装完整版：apt install -y python3-full"
  exit 1
fi
echo "[ok] 依赖模块齐全（纯标准库，无需 pip 安装）"

# 语法自检，避免把坏文件跑起来
if ! python3 -m py_compile "$APP" 2>/dev/null; then
  echo "[错误] $APP 语法校验失败，文件可能传输损坏。"
  echo "       请重新上传（建议以二进制方式传输）。"
  exit 1
fi
echo "[ok] 语法校验通过"
rm -rf "$APP_DIR/__pycache__" 2>/dev/null || true

echo ""
echo "----------------------------------------------"
echo " 启动中…（首次启动需拉取英雄表，约 2-5 秒）"
echo " 停止服务：Ctrl+C"
echo "----------------------------------------------"
echo ""

# ---------- 启动 ----------
# --no-open：服务器无 GUI，禁止尝试打开浏览器
exec python3 -u "$APP" --host "$BIND_HOST" --port "$PORT" --no-open
