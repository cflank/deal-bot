# 比价机器人（多伦多 / 加拿大）

盯你想买的商品（默认 iPhone Air），定时扫描加拿大各渠道报价，到了你设的价格就用
[Bark](https://bark.day.app)（iPhone）推送到手机。有一个网页可以看行情、改设置。

- **看板和设置页**：<https://cflank.github.io/deal-bot/>（启用 GitHub Pages 后可用）
- **运行**：GitHub Actions 定时运行，公开仓库免费，不用开电脑
- **代码**：纯 Python 标准库；网页是单个 HTML 文件，不依赖任何框架

## 一次性设置（约 10 分钟）

1. **手机推送**：iPhone 在 App Store 装 Bark，打开后复制首页上的推送网址
   （形如 `https://api.day.app/你的Key/…`）。然后在本仓库 Settings → Secrets and variables →
   Actions → New repository secret，名字 `BARK_KEY`，值直接粘贴整段网址或其中的 Key 都可以。
   **多台手机**：每台手机的网址或 Key 各占一行（也可以用逗号隔开），每条推送会发给所有手机。
   自建 Bark 服务器的话，再加一个 `BARK_SERVER`（如 `https://bark.example.com`）。
2. **开启网页**：Settings → Pages → Build and deployment → Source 选
   “Deploy from a branch”，Branch 选 `main`，文件夹选 `/docs`，保存。
   一两分钟后打开 <https://cflank.github.io/deal-bot/>。
3. **让网页能保存设置**：打开网页 → 设置 → “去 GitHub 创建令牌”。
   Repository access 选 Only select repositories → `cflank/deal-bot`；
   Permissions 里 **Contents** 和 **Actions** 都选 Read and write。
   生成后把令牌粘贴到网页里保存（只保存在这台设备的浏览器里，手机和电脑各粘一次）。
4. **测试**：网页 → 设置 → “发送测试推送”，手机应收到消息；再回到看板点“立即检查”，
   1–3 分钟后点“刷新”就能看到报价和各数据源状态。

## 网页能做什么

| 看板 | 设置 |
|---|---|
| 每个商品的在售报价，按相对原价排序；达到提醒条件的会高亮 | 增加、删除、暂停商品 |
| 每日最低价走势图（按容量、二手分线） | 搜索词、标题必须包含的词、排除词、价格范围 |
| RedFlagDeals 相关新帖 | 原价：按容量分档 / 单一原价 / 不设；目标价 |
| 各数据源成功/失败及原因 | 新机、开箱、翻新、二手各自的提醒折扣 |
| “立即检查”按钮 | 检查频率、每日行情时间、再降价门槛等；开关或添加数据源 |

保存后，设置写进仓库里的 `config.json`，机器人会马上按新设置跑一次。

## 什么时候推送

- **好价**：价格 ≤ 原价的设定百分比（按成色），或全新机低于目标价。
  已经推过的报价，要再降“再降多少才再推”这么多才会再推。
- **RFD 新帖**：标题匹配任一商品的 RedFlagDeals 新帖。
- **每日行情**：每天设定的时间后发一次，内容是每个商品最划算的报价和数据源状态。
- **新加的商品**：第一次只发一条汇总，不刷屏。
- **Bark 提醒级别**：折扣达到“最高优先级”门槛的用“时效性通知”（专注模式下也会响）；
  每日行情静默送达；其余为普通通知。每个商品的推送在 Bark 里自动分组。

## 数据源

RedFlagDeals、Best Buy Canada、Apple 官方翻新、Amazon.ca、Walmart.ca、Staples.ca、Kijiji 大多伦多。
网址里写 `{query}` 的数据源会对每个商品分别搜索。任何带结构化商品数据的网页都可以在设置页里作为
“网页”数据源添加。Amazon、Walmart 反爬较严，可能间歇失败，失败原因会显示在看板上。

## 工作方式

```
cron（每 10 分钟唤醒）→ 未到设定的检查间隔就跳过
→ 抓取各数据源 → 按商品关键词过滤 → 识别容量/成色/运营商 → 和原价比
→ 对比上次状态决定是否推送 → Bark
→ 状态、看板数据写到 data 分支（latest.json、history.json、state.json）
网页：读 data 分支显示看板；读写 main 分支的 config.json 做设置
```

## 部署到 VPS（可选，定时更准）

GitHub 的定时任务经常被推迟或跳过。用一台 Ubuntu 24.04 / Debian 12 的 VPS（1 vCPU、1GB 内存就够）
以 root 登录后运行：

```bash
curl -fsSL https://raw.githubusercontent.com/cflank/deal-bot/main/deploy/install.sh | bash
```

脚本会依次：安装依赖 → 让你粘贴 Bark 网址 → 生成部署密钥并提示你加到仓库 Deploy keys（勾选写权限）→
发测试推送 → 立即检查一次 → 启动每 5 分钟唤醒的 systemd 定时器。完成后在仓库 Actions 变量里设
`GH_SCHEDULE=off` 关掉 GitHub 的定时运行。日志：`journalctl -u dealbot -n 100`。

## 本地运行

```bash
python3 -m dealbot --dry-run            # 只打印，不推送、不写文件
python3 -m unittest discover -s tests   # 单元测试（离线）
```
