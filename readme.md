# Subconverter & Config Sync

一个强大的跨平台订阅转换与配置同步工具，专为 Surge 和 Clash 设计。采用 Python (FastAPI) 构建，支持自动抓取、节点清洗、链式代理生成以及 GitHub Gist 自动备份。

## ✨ 核心特性

- **多平台支持**: 同时生成 Surge (`.conf`) 和 Clash (`.yaml`) 托管配置。
- **智能节点清洗**: 自动识别节点地区（HK, TW, JP, US, etc.）并标准化命名。
- **链式代理自动生成**: 
  - 自动为 JP/KR/TW 等地区的节点生成“链式代理”版本。
  - 使用指定的出口节点（`EXIT`）作为落地，通过通过这些中转节点进行访问。
- **Gist 自动同步**: 
  - 自动上传配置快照到 GitHub Gist，实现多端配置漫游。
  - 支持保留 Gist 的原始链接用于远程加载。
- **Docker 部署**: 开箱即用的 Docker Compose 配置，轻松部署在 NAS 或 VPS 上。

## 📂 目录结构

项目推荐结构如下：

```text
/subconverter
  ├── main.py              # 核心程序
  ├── docker-compose.yml   # Docker 部署文件
  ├── requirements.txt     # Python 依赖
  └── config/              # [核心配置目录]
       ├── config.ini      # 主配置文件：定义订阅源、策略组、排除词
       ├── manual.ini      # 手动节点：定义 EXIT 出口与其他自建节点
       ├── gist.ini        # Gist上传凭证
       ├── surge_template.ini    # Surge 配置模板 (General, Rule, Script)
       └── clash_template.yaml   # Clash 配置模板 (DNS, Tun, Rules)
```

## 🚀 快速开始 (Docker Compose)

### 1. 准备配置
确保 `config/` 目录下已有必要的文件。你可以基于提供的模板文件进行修改。

### 2. 启动服务
在项目根目录下运行：

```bash
docker-compose up -d --build
```

### 3. 获取配置链接

- **Surge**: `http://127.0.0.1:8000/sync?target=surge`
- **Clash**: `http://127.0.0.1:8000/sync?target=clash`

(将 `127.0.0.1` 替换为你服务器的 IP 地址)

## ⚙️ 配置指南

所有配置文件均位于 `config/` 目录下。

### 1. `config.ini` (核心控制)
**功能**: 管理订阅链接、过滤关键词、策略组逻辑。

```ini
[Settings]
# 排除包含这些关键词的节点
exclude_keywords = 过期, 剩余, 官网, 重置
# 自定义 User-Agent (可选)
user_agent = Surge/5.0
# Web 管理地址 (用于生成 Surge 头部托管信息)
web_managed_url = http://192.168.1.5:8000/sync

[Sources]
# 格式: 标识符 = URL | 前缀 tag
# 前缀 tag 会被加在节点名称前
Airport_A = https://example.com/subscribe | [机场A]
Airport_B = https://sub.provider.net/api | [机场B]

[Groups]
# 策略组定义
# 语法: GroupName = type, Rule1, Rule2...
# {all}: 所有节点
# {all filter=keyword}: 包含 keyword 的节点
# {all exclude=keyword}: 排除 keyword 的节点
# 链式代理筛选: filter=Chain (因为链式节点名包含 Chain)

Proxy = select, Auto, {all}
# 仅选择香港和新加坡节点
HK_SG = select, {all filter=HK,SG}
# 专用的链式代理组
Chain_Group = select, {all filter=Chain}
```

### 2. `manual.ini` (手动节点 & 链式出口)
**功能**: 定义静态节点。**它是链式代理的核心**。

程序会自动检测名为 `EXIT` 的节点：
- **EXIT**: 必须定义的出口节点。
- **Chain Logic**: 程序会自动扫描所有抓取到的 JP/KR/TW 节点，并为它们创建一个“链式版本”。
  - 链式版本名称: `[原名] Chain`
  - 逻辑: `原节点` (作为跳板) -> `EXIT` -> 目标网站

```ini
[Proxy]
# 格式参考 Surge 标准
# 必须定义 EXIT 节点以启用链式代理功能
EXIT = socks5, 1.2.3.4, 443, username=user, password=pass
```

### 3. `gist.ini` (Gist 同步)
**功能**: 控制是否上传配置到 Gist。

```ini
[Common]
# GitHub Token (需有 gist 权限)
token = ghp_HpXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
gist_id = xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
gist_raw_url_base = https://gist.githubusercontent.com/yourname/xxxx/raw/

[surge]
filename = surge.conf

[clash]
filename = clash.yaml
```

## 🔄 自动化更新

建议设置定时任务定期请求 API 以触发更新和 Gist 同步。

**Crontab (群晖/Linux):**
```bash
# 每 12 小时更新一次 Surge 配置
0 */12 * * * curl -s "http://127.0.0.1:8000/sync?target=surge" > /dev/null

# 每 12 小时更新一次 Clash 配置
5 */12 * * * curl -s "http://127.0.0.1:8000/sync?target=clash" > /dev/null
```