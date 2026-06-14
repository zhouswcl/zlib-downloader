# Z-Library 每日图书下载器

每日自动从 Z-Library 下载图书并上传到阿里云盘。

## 功能

- 📥 **每日自动下载**: 按配置的关键词轮换，每天下载最多 10 本
- 📤 **阿里云盘上传**: 下载完成后自动上传
- 🔁 **双模式**: 自动轮换 + 手动指定关键词
- 🧠 **智能去重**: 已下载的书不会重复
- 🌐 **多域名容错**: 内置域名 Fallback 链，应对镜像不稳定

## 使用方式

### 自动模式

每天 UTC 00:00（北京时间 08:00），GitHub Actions 自动按 `config.json` 中的关键词列表轮换搜索并下载。

### 手动模式

在 GitHub 仓库页面点击 `Actions` → `Z-Library Daily Downloader` → `Run workflow`，填入自定义关键词：

```
关键词: 机器学习,深度学习,强化学习
最多下载本数: 5
```

## 配置

### config.json

```json
{
  "keywords": ["编程", "科技", "人工智能", ...],
  "max_daily": 10,
  "domains": ["https://singlelogin.re", "https://z-lib.sk", ...]
}
```

### GitHub Secrets

| Secret | 说明 |
|--------|------|
| `ZLIB_EMAIL` | Z-Library 登录邮箱 |
| `ZLIB_PASSWORD` | Z-Library 登录密码 |
| `ALIYUNDRIVE_REFRESH_TOKEN` | 阿里云盘 Refresh Token |
| `ALIYUNDRIVE_PARENT_ID` | 阿里云盘上传目录 ID（可选，默认 root） |

## 项目结构

```
├── main.py              # 主程序
├── zlib_client.py       # Z-Library API 客户端
├── config.json          # 关键词与配置
├── requirements.txt     # Python 依赖
├── .github/workflows/   # GitHub Actions
└── data/                # 下载记录
```
