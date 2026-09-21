# Dota2 历史同局查询
（coded by ds v4 flash）

输入两位 Dota2 玩家的 ID，查出他们**历史上同局过哪些比赛**——给出比赛编号、时间、双方阵容（10 人完整身份）与胜负。

## 解决什么问题

有些玩家关闭了 Dota2 客户端的「公开比赛数据」。这类账号在 OpenDota / STRATZ 的**玩家对局列表**里是空的，按常规 `account_id` 求交集永远查不到。

本项目用 STRATZ 的**比赛维度**数据逐场补全 10 人身份，把这类玩家也找出来。

## 两个数据源

| | OpenDota | STRATZ |
|---|---|---|
| 机制 | 双方各拉最近 2000 场求交集 | 以一方为基准反向扫描 2000 场，逐场补全 10 人 |
| 耗时 | 约 30 秒 | 约 60 秒 |
| 能查什么 | 常规账号 | **含已关闭公开数据的账号** |
| 免费配额 | 3000/天 | 15000/天 |

两个数据源的结果分别独立展示、互不覆盖。

## 快速开始

```bash
# 本地试跑（默认 http://127.0.0.1:8765/）
python standalone_server.py

# 或改端口 / 监听地址
python standalone_server.py --port 9000 --host 0.0.0.0 --no-open
```

仅依赖 Python 标准库，无需 `pip install`。

使用STRATZ数据源需要配置key，然后windows配置环境变量STRATZ_TOKEN=key

STRATZ的key从https://stratz.com/

## 部署到服务器

```bash
unzip dota2-direct-deploy.zip -d dota2 && cd dota2
sudo STRATZ_TOKEN=你的令牌 bash install.sh     # 一键装成 systemd 服务
```



## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `STRATZ_TOKEN` | 空 | **不配则 STRATZ 数据源不可用**。从 https://stratz.com → Settings → API 免费生成 |
| `D2D_HOST` | `127.0.0.1` | 监听地址，对外服务设 `0.0.0.0` |
| `D2D_PORT` | `8765` | 端口 |

> 令牌只从环境变量读取，不写入代码、不落盘。

## 从源码构建

`standalone_server.py` 是**自动生成**的单文件版（把源码与前端打包成一个文件，便于部署）。改完源码后重新生成：

```bash
python build_standalone.py
```

源码只有两个文件：

- `web_server.py` —— HTTP 服务、路由、内嵌的前端页面
- `teammate_intersection.py` —— OpenDota/STRATZ 调用、ID 解析、交集算法

## 文件说明

| 文件 | 用途 |
|---|---|
| `web_server.py` / `teammate_intersection.py` | 源码 |
| `build_standalone.py` | 构建脚本 |
| `standalone_server.py` | 构建产物（部署时只需这一个） |
| `start.sh` | 前台启动（排障用） |
| `install.sh` / `dota2.service` | systemd 一键部署 |

## 注意

服务**自带无鉴权**，监听 `0.0.0.0` 时任何人都能访问并消耗你的 API 配额。生产环境建议自行加一层认证或反向代理限制。

数据来源为 OpenDota / STRATZ 的公开数据。
