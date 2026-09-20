#!/usr/bin/env bash
# Dota2 历史同局查询 —— 一键部署脚本（直接部署版，不使用 Docker）
#
# 用法（在服务器上，root 身份）：
#   bash install.sh
#
# 脚本做的事：
#   1. 检查 python3（建议 3.8+）
#   2. 建目录 /opt/dota2-intersect
#   3. 安装 systemd 服务（含 STRATZ_TOKEN 注入）
#   4. 启动并做健康检查
#
# 幂等：可重复执行，会覆盖旧文件并重启服务。
#
# ★ 若先前用 systemctl edit 手工加了 override（如令牌），本脚本不会覆盖它，
#   两者会同时生效，systemd 里后写的 Environment 优先级更高。

set -euo pipefail

APP_DIR="/opt/dota2-intersect"
SVC_NAME="dota2"
PORT="${D2D_PORT:-8765}"

# ==============================================================
# ★★★ 在这里填入你的 STRATZ 令牌 ★★★
#
# 留空（默认）则跳过注入，此时 STRATZ 数据源在前端会置灰。
# 可从 https://stratz.com → Settings → API 生成（免费）。
#
# 也可以不改这个文件，改用环境变量覆盖：
#   sudo STRATZ_TOKEN=xxx bash install.sh
# ==============================================================
STRATZ_TOKEN="${STRATZ_TOKEN:-}"

# 是否允许脚本改动令牌
#   1 = 若上面填了令牌，就写入 systemd（覆盖同名项）
#   0 = 完全不动令牌配置（用于不想让脚本碰令牌的场景）
SET_TOKEN="${SET_TOKEN:-1}"

# 脚本所在目录（部署包解压后的位置）
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=============================================="
echo " Dota2 历史同局查询 —— 部署"
echo " 源目录: $SRC_DIR"
echo " 目标:   $APP_DIR"
echo " 端口:   $PORT"
if [ -n "$STRATZ_TOKEN" ]; then
  echo " 令牌:   已提供（长度 ${#STRATZ_TOKEN}）"
else
  echo " 令牌:   未提供（STRATZ 数据源将不可用）"
fi
echo "=============================================="

# ---------- 0. 必须 root ----------
if [ "$(id -u)" -ne 0 ]; then
  echo "[错误] 请用 root 运行：sudo bash install.sh"
  exit 1
fi

# ---------- 1. 检查 python3 ----------
if ! command -v python3 >/dev/null 2>&1; then
  echo "[错误] 未找到 python3。"
  echo "  Ubuntu/Debian: apt update && apt install -y python3"
  exit 1
fi
PY_VER="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
echo "[ok] python3 版本: $PY_VER"

# 版本必须 >= 3.7（代码用到 f-string、dataclass 之外的现代语法）
python3 - <<'PYEOF'
import sys
if sys.version_info < (3, 7):
    sys.exit("需要 Python 3.7 及以上")
PYEOF
echo "[ok] Python 版本满足要求"

# 确认纯标准库可导入（提前暴露环境问题）
python3 -c "import http.server, urllib.request, json, csv, ssl" \
  && echo "[ok] 依赖模块齐全（纯标准库）"

# ---------- 2. 拷贝文件 ----------
mkdir -p "$APP_DIR"
for f in standalone_server.py; do
  if [ ! -f "$SRC_DIR/$f" ]; then
    echo "[错误] 缺少文件: $SRC_DIR/$f"
    exit 1
  fi
  cp -f "$SRC_DIR/$f" "$APP_DIR/$f"
done
# 部署说明（有就拷）
[ -f "$SRC_DIR/README.md" ] && cp -f "$SRC_DIR/README.md" "$APP_DIR/README.md" || true
chmod 0644 "$APP_DIR/standalone_server.py"
echo "[ok] 程序文件已就位: $APP_DIR/standalone_server.py"

# ---------- 3. 语法自检 ----------
if ! python3 -m py_compile "$APP_DIR/standalone_server.py" 2>/dev/null; then
  echo "[错误] standalone_server.py 语法校验失败"
  exit 1
fi
echo "[ok] 语法校验通过"

# ---------- 4. 安装 systemd 服务 ----------
if [ -f "$SRC_DIR/dota2.service" ]; then
  cp -f "$SRC_DIR/dota2.service" "/etc/systemd/system/${SVC_NAME}.service"
else
  echo "[错误] 缺少 dota2.service"
  exit 1
fi
systemctl daemon-reload
echo "[ok] systemd 服务已安装: ${SVC_NAME}.service"

# ---------- 4.5 注入 STRATZ_TOKEN ----------
# ★ 用 override.d/ 目录而不是改 dota2.service 原文件，原因：
#   1) 保留「服务配置」与「密钥」的分离，原文件可以随包更新而无副作用
#   2) 用户若手工用 `systemctl edit` 加过其他配置，不会互相覆盖
#   systemd 会按字典序合并 override.d/*.conf，后应用的同名项优先级更高。
OVERRIDE_DIR="/etc/systemd/system/${SVC_NAME}.service.d"
TOKEN_DROPIN="${OVERRIDE_DIR}/10-stratz-token.conf"

if [ "$SET_TOKEN" = "1" ] && [ -n "$STRATZ_TOKEN" ]; then
  mkdir -p "$OVERRIDE_DIR"

  # ★ 用 printf 而非 echo：令牌是 JWT，含 . 和 _ 、- 等字符，
  #   echo 在某些 shell 下会解释转义序列，printf '%s' 是纯字面输出。
  printf '[Service]\nEnvironment=STRATZ_TOKEN=%s\n' "$STRATZ_TOKEN" > "$TOKEN_DROPIN"
  # 收紧权限：令牌只该被 root 读到
  chmod 600 "$TOKEN_DROPIN"

  systemctl daemon-reload
  echo "[ok] STRATZ_TOKEN 已注入: $TOKEN_DROPIN"
  echo "     令牌长度 ${#STRATZ_TOKEN}，文件权限 600"

elif [ "$SET_TOKEN" = "0" ]; then
  echo "[info] SET_TOKEN=0，跳过令牌注入（保持现有配置不变）"

elif [ -z "$STRATZ_TOKEN" ]; then
  # 未提供令牌：不动现有配置，只提示
  if [ -f "$TOKEN_DROPIN" ]; then
    echo "[info] 本次未提供令牌，保留已有的 $TOKEN_DROPIN"
  else
    echo "[警告] 未提供 STRATZ_TOKEN —— STRATZ 数据源将在前端置灰。"
    echo "       补配方式（任选其一）："
    echo "         a) 重新执行：sudo STRATZ_TOKEN=你的令牌 bash install.sh"
    echo "         b) 手工配置：sudo systemctl edit ${SVC_NAME}"
  fi
fi

# ---------- 5. 启动 ----------
systemctl enable "${SVC_NAME}" >/dev/null 2>&1 || true
systemctl restart "${SVC_NAME}"
echo "[ok] 服务已启动，等待健康检查…"

# ---------- 6. 健康检查 ----------
OK=0
for i in $(seq 1 20); do
  sleep 1
  if curl -fsS --max-time 5 "http://127.0.0.1:${PORT}/health" >/tmp/_d2d_health 2>/dev/null; then
    OK=1
    break
  fi
done

echo ""
if [ "$OK" -eq 1 ]; then
  echo "=============================================="
  echo " 部署成功"
  echo "=============================================="
  echo " 健康检查:"
  cat /tmp/_d2d_health; echo ""
  rm -f /tmp/_d2d_health
  echo ""

  # 若配了令牌，顺带报告 STRATZ 是否真的启用
  if [ -n "$STRATZ_TOKEN" ]; then
    echo -n " STRATZ 状态: "
    if curl -fsS --max-time 8 "http://127.0.0.1:${PORT}/api/stratz" 2>/dev/null \
        | grep -q '"enabled": *true'; then
      echo "已启用 ✓"
    else
      echo "未启用 ✗（令牌可能无效，请检查）"
    fi
    echo ""
  fi

  PUBIP="$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || echo '<你的公网IP>')"
  echo " 访问地址: http://${PUBIP}:${PORT}/"
  echo " 查看日志: journalctl -u ${SVC_NAME} -f"
  echo " 重启服务: systemctl restart ${SVC_NAME}"
  echo " 停止服务: systemctl stop ${SVC_NAME}"
  echo ""
  echo " ⚠️  别忘了在阿里云控制台『安全组』放行 TCP ${PORT} 端口，"
  echo "     否则公网无法访问（本机 curl 能通不代表外网能通）。"
else
  echo "=============================================="
  echo " 部署未通过健康检查，请查看日志"
  echo "=============================================="
  systemctl status "${SVC_NAME}" --no-pager -l | head -30
  echo ""
  echo "--- 最近日志 ---"
  journalctl -u "${SVC_NAME}" -n 40 --no-pager
  exit 1
fi
