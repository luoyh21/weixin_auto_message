# weixin_auto_message

每天早上 9 点自动抓取 **SpaceNews** 与 **企业自建 OPML 订阅源** 近一天的新闻，
调用 OpenAI（默认 `gpt-4.1-mini`，可走代理）生成一份中文「航天每日速递」，
并通过 **企业微信应用消息** 推送给指定成员。

同时提供一个 FastAPI 服务（默认 `0.0.0.0:8503/weixin`），用于接收企业微信
后台回调消息，使用 **官方 WXBizMsgCrypt** 完成签名/加解密，再用大模型基于
**前一日抓取到的新闻原始材料**进行问答回复。

## 目录结构

```
weixin_auto_message/
├── .env                       # 真实凭据（gitignore）
├── .env.example               # 模板
├── requirements.txt
├── README.md
├── data/
│   ├── zlzchat.opml           # 订阅源列表（OPML 格式）
│   └── cache/                 # 每日抓取与总结结果 daily_YYYY-MM-DD.json
├── vendor/                    # 官方 WXBizMsgCrypt（pycryptodome 版）
│   ├── WXBizMsgCrypt3.py
│   └── ierror.py
├── src/
│   ├── config.py              # 读取 .env
│   ├── wecom.py               # 企业微信应用消息发送
│   ├── spacenews.py           # spacelive.cn 列表页抓取（聚合 NASA/SpaceNews/...）
│   ├── verify_server.py       # ★ 仅做企业微信回调 URL 验证的最小服务
│   ├── opml_feeds.py          # OPML 解析 + 公众号摘要抓取
│   ├── summarizer.py          # OpenAI 摘要/问答
│   ├── daily.py               # 每日流程编排 + 缓存
│   └── server.py              # FastAPI /weixin 服务
└── scripts/
    ├── run_once.py            # 单次运行（用于测试发送）
    └── run_scheduler.py       # 常驻定时任务（每天 09:00）
```

## 快速开始

```bash
cd /root/workspace/weixin_auto_message

# 1. 创建虚拟环境（已为你创建过）
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. 编辑 .env，填入真实参数
cp .env.example .env  # 已为你写好默认值，复用即可
$EDITOR .env

# 3. 单次测试（抓取 + 总结 + 真实推送）
.venv/bin/python -m scripts.run_once

# 仅生成不发送
.venv/bin/python -m scripts.run_once --no-send

# 4. 启动每日 9:00 定时（前台阻塞）
.venv/bin/python -m scripts.run_scheduler

# 5. 启动 /weixin 回调服务（前台）
.venv/bin/python -m src.server
# 或 systemd / pm2 / docker 守护
```

后台跑：

```bash
# 定时任务
nohup .venv/bin/python -m scripts.run_scheduler > logs/scheduler.log 2>&1 &
# 回调服务
nohup .venv/bin/python -m src.server > logs/server.log 2>&1 &
```

## 配置项 (`.env`)

| 变量 | 说明 |
|---|---|
| `WECOM_CORP_ID` | 企业 ID |
| `WECOM_AGENT_ID` | 自建应用 AgentId |
| `WECOM_SECRET` | 应用 Secret |
| `WECOM_TO_USER` | 接收人 UserId，多人用 `\|` 分隔；填 `@all` = 该应用「可见范围」内全部成员 |
| `WECOM_CALLBACK_TOKEN` | 自建应用「接收消息」的 Token |
| `WECOM_CALLBACK_AES_KEY` | 自建应用「接收消息」的 EncodingAESKey (43 位) |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` | OpenAI 兼容接口 |
| `SERVER_HOST` / `SERVER_PORT` | 默认 `0.0.0.0:8503` |
| `SPACENEWS_RSS` | 默认 `https://spacenews.com/feed/` |
| `OPML_PATH` | 默认 `data/zlzchat.opml` |
| `DAILY_MORNING_HOUR` / `DAILY_MORNING_MINUTE` | 早间速递时间，默认 `08:00`；留空关闭该班次 |
| `DAILY_EVENING_HOUR` / `DAILY_EVENING_MINUTE` | 晚间速递时间，默认 `17:00`；留空关闭该班次 |
| `DAILY_TZ` | 时区，默认 `Asia/Shanghai` |
| `DAILY_WINDOW_HOURS` | 每次抓取覆盖过去 N 小时，默认 `12` |

## 在企业微信后台配置回调（分两步）

应用「接收消息」→ 设置 API 接收：

- URL: `http://<你的服务器公网IP>:8503/weixin`
- Token: 与 `WECOM_CALLBACK_TOKEN` 一致
- EncodingAESKey: 与 `WECOM_CALLBACK_AES_KEY` 一致
- 消息加解密方式：**安全模式**

> 企业微信只接受 80/443/8000~8999 等指定端口，本项目选用 `8503`。

点击「保存」时，企业微信会向上面的 URL 发起一次 `GET` 验证请求：
```
GET /weixin?msg_signature=...&timestamp=...&nonce=...&echostr=ENCRYPT_STR
```
后台需在 1 秒内：URL decode → 用 token+ts+nonce+echostr 计算 SHA1 校验签名 →
用 EncodingAESKey 解密 echostr → 原样回写明文 `msg`（无引号/BOM/换行）。

### 步骤 A：先只跑「URL 验证专用服务」`src/verify_server.py`

这是一个**最小**的 FastAPI 应用，**只挂载** `GET /weixin`、不接收任何 POST，
专门用于在企业微信后台第一次「保存」URL 时通过那一次握手。

```bash
cd /root/workspace/weixin_auto_message
.venv/bin/python -m src.verify_server
```
（必要时 `nohup ... > logs/verify.log 2>&1 &` 后台跑。）

在企业微信后台点「保存」→ 提示 *验证成功* 后，**停掉**本进程。

### 步骤 B：换成完整业务服务 `src/server.py`

```bash
.venv/bin/python -m src.server
```
该服务同时处理 `GET /weixin`（接入验证）和 `POST /weixin`（用户消息），
POST 时解密用户文本，调用 `gpt-4.1-mini`（以最近一份 `daily_*.json` 为上下文）
生成回复，再加密返回给企业微信。

> 之所以拆成两个文件：保存 URL 时你可能还没开发完业务逻辑、或不想让生产消息
> 打到没准备好的接口。先单独跑 `verify_server` 完成接入握手，再无缝切换到
> `server`，二者使用同一份 `.env`、同一组 Token/EncodingAESKey/CorpID。

## 数据缓存

每次 `run_daily` 会写入 `data/cache/daily_YYYY-MM-DD.json`，结构：

```json
{
  "date": "2026-05-22",
  "generated_at": "2026-05-22T09:00:01",
  "spacenews": [{"title": "...", "link": "...", "summary": "...", "published": "..."}],
  "opml":      [{"source": "国际太空", "title": "...", "link": "...", "description": "..."}],
  "summary":   "【🚀 航天每日速递 ...】\n...",
  "sent": true
}
```

`/weixin` 回调时会读取最近一份缓存作为 LLM 的上下文。

## 扩展

- **多接收人**：`WECOM_TO_USER=LuoYiHe|ZhangSan|LiSi`
- **改时间**：`DAILY_HOUR=8`、`DAILY_MINUTE=30`
- **改模型 / 走自有代理**：编辑 `OPENAI_BASE_URL` 和 `OPENAI_MODEL`
- **添加更多订阅**：编辑 `data/zlzchat.opml`，加 `<outline xmlUrl="..." text="名称" type="rss"/>`

## 已知的部署侧前置条件 / 排错

### 1. 企业微信 `errcode 60020 not allow to access from your ip`
出口 IP 没加白名单。前往：
**企业微信后台 → 应用管理 → 你的应用 → 开发者接口 → 企业可信 IP**，
添加运行本程序的服务器公网 IP（可用 `curl -s ifconfig.me` 查询）。
本机当前出口 IP：`8.130.209.181`。

### 2. 关于英文航天新闻源
当前 `src/spacenews.py` 抓取的是 **https://www.spacelive.cn/news**（国内可达），
该站把 NASA / SpaceNews / SpacePolicyOnline 等多源做了**聚合**，每条都有
标题/来源/时间/原文链接。代码会按近 24h 过滤，然后**尝试**访问每条原文链接抓取
`og:description` 或首段作为摘要：

- 原文站点国内可达 → 摘要被抓到，喂给 GPT 时上下文更丰富；
- 原文站点对国内 IP 不可达（例如 `spacenews.com` 在 CDN 层 429 全段封锁）→
  跳过摘要、只保留 spacelive 给出的标题/来源/时间/链接，GPT 仍可基于标题做总结。

如需提速可降低 `enrich`，或把外链抓取改成并发；详见 `src/spacenews.py`。

## License

MIT
