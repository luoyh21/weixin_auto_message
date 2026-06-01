# weixin_auto_message

每天定时抓取 **SpaceNews / NASASpaceflight / OPML 订阅源** 近一时段的新闻 +
配置好的 **抖音视频号** 最近发布的作品，**默认完全本地化**：

- 翻译：`deep-translator` → **MyMemory**（中国大陆可直连，无需 key）
- 总览：`textrank4zh` 抽取式中文摘要（jieba 分词 + TextRank 句子排序）

如需更高质量、或本地缺译/抽取不理想，可通过 `.env` 切到 LLM 兜底
（`TRANSLATE_USE_LLM=1` / `SUMMARIZER_USE_LLM=1`，默认 `gpt-4.1-mini`，可走代理）。
最终生成一份中文「航天速递」并通过 **企业微信应用消息** 推送给指定成员。

消息形态：
- **SpaceNews + 抖音** → `msgtype=mpnews`（企业微信原生图文）一次最多 8 篇，封面、标题、
  中文译文正文、内嵌图片全部在客户端内直接渲染，**不再有任何外链跳转**；抖音条目以
  「封面 + 抖音口令文本」原生展示，长按即可选中复制口令再回抖音 App 打开。
- **公众号** → 单独一条 `msgtype=news` 外链卡片，**点击直接打开 mp.weixin.qq.com 原文**，
  不经任何中间页（公众号文章本身已是微信原生页，没必要再套一层 mpnews 渲染）。

mpnews 发送失败时自动回退到 `msgtype=news`（外链卡片）+ 本机 `/news/`、`/dy/` 落地页，
保证不会漏播。所有图片仍走本机 `/img` 代理 + `images.weserv.nl` 兜底，跨客户端一致。

同时提供一个 FastAPI 服务（默认 `0.0.0.0:8503`），挂载以下路由：

- `GET /weixin` / `POST /weixin` —— 企业微信回调接入 + 基于最近一日新闻的 GPT 问答
- `GET /news/{batch}/{page_id}` —— 国际新闻的中文翻译页（卡片实际跳转目标）
- `GET /dy/{aweme_id}` —— 抖音作品中转页（App / 浏览器 / 口令 三种打开方式）
- `GET /img?u=...&r=...` —— 第三方图片代理（统一带 Referer/UA 抓源图、落盘缓存）
- `POST /ingest/spacenews` —— 远端 scraper（GitHub Actions 等）推送 SpaceNews 全文

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
│   ├── wecom.py               # 企业微信应用消息发送（text / image / news / mpnews / markdown + 临时素材上传）
│   ├── spacenews.py           # ingest 优先 + spacelive.cn 回退；统一过滤栏目/订阅/视频等非文章
│   ├── verify_server.py       # ★ 仅做企业微信回调 URL 验证的最小服务
│   ├── opml_feeds.py          # OPML 解析 + 公众号摘要抓取
│   ├── summarizer.py          # OpenAI 摘要 / 翻译 / 问答
│   ├── daily.py               # 每日流程编排 + 缓存（含抖音条目拼装）
│   ├── ingest.py              # 远端 scraper 推过来的 SpaceNews 全文入库
│   ├── news_pages.py          # 英文新闻 → 中文翻译页生成器
│   ├── douyin.py              # 调本机 docker API 抓抖音账号近 N 小时作品
│   ├── dy_pages.py            # 抖音作品落地页生成器（App/浏览器/口令三选一）
│   ├── img_proxy.py           # 第三方图片代理 + 预热（拉不到自动回退 weserv，再不行就放弃 picurl）
│   ├── wx_mp.py               # 公众号 freepublish 封装（草稿/发布/封面/正文图，不群发关注者）
│   └── server.py              # FastAPI 服务（/weixin /news /dy /img /ingest）
└── scripts/
    ├── run_once.py            # 单次运行（手工测试发送，默认只发 LuoYiHe）
    └── run_scheduler.py       # 常驻定时任务（按 .env 的早/晚两档时点 cron）
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
| `DAILY_WINDOW_HOURS` | 每次抓取覆盖过去 N 小时，默认 `12`（scheduler 会按早/晚时点之差自动计算实际窗口） |
| `PUBLIC_BASE_URL` | 对外可达的本服务 base URL，决定卡片里的 `/news /dy /img` 链接，例如 `http://links.he-ting.com` |
| `DOUYIN_API_BASE` | 抖音 API 容器地址，默认 `http://127.0.0.1:8504` |
| `DOUYIN_USERS` | 抖音账号列表，多个用 `,` 分隔。每项格式 `显示名:sec_user_id` 或仅 `sec_user_id` |
| `DOUYIN_MAX_TOTAL` | 单次速递最多转发抖音条数，默认 `2`（多账号时全局上限） |
| `DOUYIN_PER_USER_LIMIT` | 每个账号本次最多取几条最新作品，默认 `1` |
| `DOUYIN_WINDOW_HOURS` | 抖音抓取时间窗口（小时）；留空则复用 `DAILY_WINDOW_HOURS` |
| `OPML_MAX_CARDS` | 单次速递中公众号卡片的最大条数，默认 `2` |
| `WX_MP_APPID` / `WX_MP_APPSECRET` | 公众号 AppID / AppSecret（开发 → 基本配置） |
| `WX_MP_ENABLED` | `1` = 每次速递同步生成公众号草稿/文章；`0` = 关闭 |

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

## 抖音视频号转发（可选）

部署一个 [`evil0ctal/douyin_tiktok_download_api`](https://github.com/Evil0ctal/Douyin_TikTok_Download_API) 容器即可：

```bash
# dockerhub 走主流镜像通常 403，可用 1ms.run 镜像
docker pull docker.1ms.run/evil0ctal/douyin_tiktok_download_api:latest
docker tag  docker.1ms.run/evil0ctal/douyin_tiktok_download_api:latest \
            evil0ctal/douyin_tiktok_download_api:latest
docker run -d --name douyin_api --restart unless-stopped \
  -p 127.0.0.1:8504:80 evil0ctal/douyin_tiktok_download_api:latest
```

容器内置 cookie 是匿名状态，对国内账号只能看到部分滞后数据。要拿到真正的最新作品，
**复制浏览器登录态 Cookie** 后替换容器里的配置：

```bash
# 1) 在浏览器登录 https://www.douyin.com/，开发者工具复制完整 Cookie 字符串到 /tmp/dy.txt
# 2) 直接改容器内的 config.yaml（自带 update_cookie 接口对外部 cookie 校验过严）
docker cp douyin_api:/app/crawlers/douyin/web/config.yaml /tmp/dy_cfg.yaml
python3 -c "
import re,sys
cfg=open('/tmp/dy_cfg.yaml').read()
cookie=open('/tmp/dy.txt').read().strip()
open('/tmp/dy_cfg.yaml','w').write(re.sub(r'(\n\s*Cookie:\s*)[^\n]*\n', r'\g<1>'+cookie+'\n', cfg, 1))
"
docker cp /tmp/dy_cfg.yaml douyin_api:/app/crawlers/douyin/web/config.yaml
docker restart douyin_api
```

> 抖音 `sid_guard` 有效期 60 天，到期后接口会退化到匿名结果，按上面同样办法重新替换一次即可。

`.env` 配置示例：

```env
DOUYIN_API_BASE=http://127.0.0.1:8504
DOUYIN_USERS=我们的太空:MS4wLjABAAAA8tYhNulGyT_4NVlSylLBZKvSEkqACthevMPPXbTZgXI
DOUYIN_MAX_TOTAL=2
DOUYIN_PER_USER_LIMIT=1
DOUYIN_WINDOW_HOURS=
```

转发逻辑：
- 每个账号取最近 `DOUYIN_PER_USER_LIMIT` 条非置顶作品；
- 创建时间须落在抓取窗口内，否则该账号本次不参与；
- 多账号时按时间倒序合并，截断到 `DOUYIN_MAX_TOTAL`；
- 卡片标题前缀 `[抖音·{显示名}]`，封面图过本机 `/img` 代理；
- 卡片点击进入本机 `/dy/{aweme_id}` 中转页：同时展示**抖音 App 链接**、**浏览器链接**、**抖音口令文本**三种打开方式，避免企业微信内置浏览器拦截唤起时无路可走。

## 公众号同步（`wx_mp`，可选）

把每次「航天速递」同步成一篇可分享的公众号图文，**走 `freepublish/submit`，
不会触发群发推送给关注者**，仅生成一条永久 `mp.weixin.qq.com` URL 供后续做卡片 / 外链使用。

### 启用步骤

1. `.env` 填入公众号凭据并打开开关：

   ```env
   WX_MP_APPID=wxXXXXXXXXXXXXXXXX
   WX_MP_APPSECRET=********************************
   WX_MP_ENABLED=1
   ```

2. 在 [`mp.weixin.qq.com`](https://mp.weixin.qq.com) → **开发 → 基本配置 → IP 白名单**
   把本服务器出口 IP（`curl -s ifconfig.me`，当前是 `8.130.209.181`）加进白名单，
   否则所有调用会报 `40164 invalid ip ... not in whitelist`。

3. 重启 `run_scheduler` 让其加载新 `.env`。

### 行为说明

- 每次速递结束后，会用「正文摘要 + 抖音清单 + hero 大图」拼一篇 HTML，
  通过 `media/uploadimg`（无配额）上传内嵌图、`material/add_material` 上传一张永久封面，
  再 `draft/add` + `freepublish/submit`。
- 成功发布时（**认证服务号 / 认证订阅号**）会拿到 `mp.weixin.qq.com/s/...` 永久链接，
  会作为一条文本附在速递结尾发回到企业微信，且写入 `data/cache/<session>_<date>.json` 的 `wx_mp_url` 字段。
- **个人未认证订阅号** 没有 `freepublish` 接口权限，会触发 `errcode=48001`。
  程序会自动退化为「只写入草稿箱」，并发一条提醒到企业微信，
  你只要登录公众号后台 → **内容管理 → 草稿箱** → 找到最新一篇 → 点 **发表**，
  微信就会生成永久 mp 链接。
- 由于「每次都得人工点发表」对自动化体验是个明显回退，**默认 `WX_MP_ENABLED=0` 关闭这条链路**，
  日常仍走自建 `/news/` 落地页。等账号完成认证、API 直发跑通后再把开关打开即可。
- 图片上传前用 Pillow 重编码成 baseline JPEG，规避微信对个别 ICC / EXIF 段的
  `40113 / 40137` 报错；上传过的图按 SHA256 去重，避免重复占用 5000 张永久素材配额。

## 域名 / 反向代理（Cloudflare Tunnel）

业务地址挂在 **`https://links.he-ting.com`**，通过 **Cloudflare Tunnel（cloudflared）** 把
源站 `localhost:8080` 反代出去——不开任何入站端口、不需要 ICP 备案、自带 HTTPS。

> 为啥不直接挂 80/443：服务器在阿里云大陆区，未备案前阿里云会拦截入站到 80/443 的未备案
> 域名流量并返回引导页；非标端口（如 8080/8503）虽然不被拦，但企业微信 / 微信内置浏览器
> 会对非标端口报「无法评估安全性」。Cloudflare Tunnel 把流量从外部 443 端口接进 CF，
> 再通过 cloudflared 的**出站**长连接把数据送回源站，全程绕过阿里云 80/443 拦截 + 给用户呈现干净的 443 HTTPS。

部署后的关键事实：

- `cloudflared` 已注册为 systemd 服务（`systemctl status cloudflared`）；
- Cloudflare → Zero Trust → Networks → Tunnels 里只配了一条 ingress：
  `links.he-ting.com → HTTP → localhost:8080`；
- DNS：`links.he-ting.com` 在 CF 上是 tunnel 自动管理的 CNAME（不再是 A 记录指向源站 IP）；
- FastAPI 监听 `0.0.0.0:8080`（`SERVER_PORT=8080`），阿里云安全组也只对外开放 8080
  作为应急直连入口（`http://8.130.209.181:8080/...` 也能访问，但不建议放到公开消息里）。

`.env` 里 `PUBLIC_BASE_URL=https://links.he-ting.com`，所有抖音落地页 / `/news/` 翻译页 /
`/img` 代理 URL / GitHub Action `INGEST_URL` 都用这个干净域名。

## 图片代理 `/img`

很多源站（NSF / 抖音 douyinpic / 部分 SpaceNews 子图）对**未带 Referer / 国内 IP**会返
`403 / 429`。每张卡片的 `picurl` 是接收方客户端各自去拉的——发送方能看见、其他人看不见，
绝大部分都是这个原因。我们的解决：

1. `src/img_proxy.py::prefetch(url, referer)` 在装卡片前先用服务端身份带 Referer/UA 抓一次源图；
2. 直接拿不到（NSF / Cloudflare 全段 403、抖音 douyinpic Referer 校验等）就**自动回退**走
   `images.weserv.nl` 公共图片代理再试一次——绝大部分盗链 / 国内访问问题都能就此搞定；
3. 两次都拿不到就**不塞 picurl**，避免接收方看到一个「加载失败」灰框；
4. 抓得到的图落盘到 `data/img_cache/<sha>.bin`；
5. 卡片上的 `picurl` 全部重写成 `http://<PUBLIC_BASE_URL>/img?u=<src>&r=<referer>`，
   接收方一律从我们的服务器拉，跨客户端一致。

## 定时窗口

`scripts.run_scheduler` 会根据 `.env` 里的 `DAILY_MORNING_HOUR` / `DAILY_EVENING_HOUR` 自动算出
每次任务的"真实窗口"——晚间任务覆盖 *早 → 晚*、早间任务覆盖 *昨晚 → 今早*，相邻两次任务
首尾相接、既不漏播也不重复（旧的固定 `DAILY_WINDOW_HOURS` 仅在缺一档时兜底）。

公众号订阅源独立走另一套节奏：
- **早间速递**：固定抓过去 24 小时的公众号文章（覆盖昨天 8:00 至今天 8:00 的所有更新）；
- **晚间速递**：完全跳过公众号，避免一日内重复打扰；
- **手动 `run_once --session daily`**：保留旧行为，与 SpaceNews 同窗口。

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
本机当前出口 IP：`8.130.209.181`（同时也是 WeCom IP 白名单与微信公众号 IP 白名单要填的值）。

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
