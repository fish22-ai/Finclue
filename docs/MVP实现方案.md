# MVP 最小实现方案

> 状态：**待确认，未开发**（四支柱已全部实测通过，仅剩建表）
> 日期：2026-09-12，2026-09-13 修订（小红书接入）

## 0. 验证目标（只验证这四件事）

1. 能不能抓到有效公开信息
2. 能不能结构化成事实
3. 能不能生成有意义的洞察
4. 能不能写进飞书

**任何不服务于这四条的功能，一律不做。**

---

## 1. 架构

单入口脚本，八段顺序执行，段间用本地 JSON 文件解耦（每段可独立重跑）。

```
config.yaml
    │
[1] fetch      各源适配器 → data/raw/{date}/{source}.json
    │          ├─ 小红书：subprocess 调 socai xhs search → get-notes --ocr
    │          ├─ Reddit ：requests 拉 .rss → feedparser
    │          └─ LinkedIn：requests 拉 jobs-guest API → bs4 解析卡片
    │
[2] clean      正文提取 / 去 HTML / 脱敏 → data/clean/{date}.json
    │
[3] dedup      URL 归一 + 标题 SimHash → 过滤已知 URL（seen_urls.jsonl）
    │          注意：小红书用 note_id 去重，不用 URL（URL 带易变的 xsec_token）
    │
[4] filter     相关性关键词 + 噪音黑名单 + 长度阈值
    │
[5] extract    LLM 逐条抽取 → 严格 JSON → 反幻觉校验
    │
[6] write      飞书【Career Intelligence】事实表（先查后写，幂等）
    │
[7] insight    LLM 读近 30 天事实 → 六主题增量分析
    │
[8] write      飞书【Career Insights】分析表
    │
   log          data/logs/{date}.log
```

> **注意 fetch 的内部结构差异**：小红书走的不是 HTTP，而是 `subprocess` 调用 socai 二进制，
> 产物落在 `~/.socai/runs/`，需要拷贝回 `data/raw/`。这是三个源里唯一的非 HTTP 适配器。

### 技术栈

| 项 | 选择 | 理由 |
|---|---|---|
| 语言 | Python 3.13，纯脚本 | 已装，无框架 |
| **小红书抓取** | **外部二进制 `socai`（subprocess 调用）** | **不自研浏览器自动化，复用现成工具** |
| HTTP | `requests` | 已装 |
| RSS | `feedparser` | 已装（Reddit 源必需） |
| HTML | `lxml` + `beautifulsoup4` | 已装 |
| 配置 | `pyyaml` | **需安装** |
| LLM | `requests` 直调 OpenAI 兼容接口 | 不引入 SDK，多一层抽象没必要 |
| 调度 | Windows 任务计划程序 | 系统原生 |
| 存储 | 本地 JSONL + 飞书表 | 不引入数据库 |

**不需要安装**：pandas、playwright、selenium、SQLAlchemy、任何爬虫框架。

---

## 2. 分批实施

### 第一批：闭环打通（目标：跑出一条完整数据）

| 步 | 内容 | 产出 |
|---|---|---|
| 1 | 环境准备：装 `pyyaml`，建目录结构 | — |
| 2 | 飞书：**新建两张表**、把 `该自建应用（app_id 见 config.yaml 的 feishu.credential_file）` 加为协作者、写字段、填 `config.yaml` | 表可写 |
| 3 | `fetch`：做 **小红书（socai）** 一个源 —— 它字段最全，最能验证抽取质量 | `raw/*.json` |
| 4 | `clean` + `dedup` + `filter` | `clean/*.json` |
| 5 | `extract`：接 agentrouter，跑通 1 条 | 结构化 JSON |
| 6 | `write`：1 条写进飞书事实表（复用 `feishu_write.py` 改造） | **闭环达成** ✅ |

> 这一批结束时，应能在飞书里看到 1 行真实数据。**这是整个项目的成败分界点。**

### 第二批：跑量 + 洞察

| 步 | 内容 |
|---|---|
| 7 | `fetch` 加 **Reddit RSS** 与 **LinkedIn guest jobs** |
| 8 | 全量跑一天，验证抽取质量与成本 |
| 9 | `insight` + 写入分析表 |
| 10 | 配 Windows 任务计划，每日自动 |

### 第三批（暂不做）
eFinancialCareers、Bing 搜索、券商官方招聘页、socai 的 `dy`（抖音）通道。

---

## 3. 关键技术要点（实测得出，直接照做）

### 3.1 小红书抓取（经 socai）⭐ 最重要的源
```bash
SOCAI="C:/Users/吃鱿鱼的鱿鱼/.socai/bin/socai.exe"   # 绝对路径，不在 PATH 快照里

# 阶段一：搜索卡片（便宜）
"$SOCAI" xhs search "<query>" --preview --num-notes 30 --pretty
# 阶段二：取正文 + 逐图 OCR + 评论（贵，慢）
"$SOCAI" xhs get-notes --note <NOTE_ID>=<XSEC_TOKEN> --ocr --num-comments 8 --pretty
```
- **`--ocr` 必须开** —— 金融内容大量在图片里（JD 截图、岗位表格），纯正文漏一半
- 卡片里的 `xsec_token` 是 `get-notes` 的**必填参数**，缺了取不到正文
- socai **先打印一行 `run_dir:` 再输出 JSON** → 解析时从第一个 `{` 开始截取
- 选帖：按点赞排序取 Top 15 高赞池 → 池中随机抽 5 篇（不直接取 Top 5）
- 产物在 `~/.socai/runs/{ts}_{cmd}/`；`output.json` 报 `reason:"login_required"` = 需重新扫码
- 抓取放缓、不并发；单次调用可能很慢（OCR + 打开笔记），`timeout` 给到 900s

### 3.2 Reddit 抓取
```
GET https://www.reddit.com/r/{sub}/.rss
```
- ✅ 用 `.rss`，**不要**用 `.json`（403）
- ⚠️ 请求间隔 ≥5 秒，否则 429
- 正文在 `<content type="html">` 里，需去 HTML 标签 + `html.unescape`

### 3.3 LinkedIn 抓取
```
GET https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search
      ?keywords={kw}&location={loc}&start={0,10}
```
- 无需登录，每页 10 条
- 解析 `<li>` 卡片：`base-search-card__title` / `hidden-nested-link` / `job-search-card__location` / `datetime` 属性

### 3.4 LLM 调用（最容易踩坑的地方）
```python
headers = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {key}",
    # 以下三个头缺一不可，否则 unauthorized client detected
    "user-agent": "claude-cli/2.0.0 (external, cli)",
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "claude-code-20250219",
}
# POST https://agentrouter.org/v1/chat/completions
# body: {"model":"deepseek-v4-flash","max_tokens":4096,"temperature":0,"messages":[...]}
```
- ⚠️ `max_tokens` 必须给足（≥4096），该模型是推理模型，`reasoning_content` 会吃掉预算
- ⚠️ 返回是 OpenAI 格式：取 `response["choices"][0]["message"]["content"]`

### 3.5 反幻觉校验（代码层，必须有）
1. `json.loads` 失败 → 重试 1 次 → 再失败丢弃
2. `evidence` 中无对应条目的非 null 字段 → 置 null
3. `recruiting_bar` 与 `actual_bar` 若引用同一句话 → `actual_bar` 置 null
4. `evidence_count > 输入事实数` → 判为幻觉，丢弃该洞察

### 3.6 脱敏（正则兜底，不依赖 LLM）
```python
# 手机号 / 微信 / QQ / 邮箱 —— 在 clean 阶段和写入前各跑一次
1[3-9]\d{9}                    → [手机号已脱敏]
[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+ → [邮箱已脱敏]
(微信|VX|vx|wechat)[:：]?\s*[\w-]{5,} → [联系方式已脱敏]
```
> ⚠️ 小红书笔记的评论区是个人信息高发区，**评论进 LLM 前必须先脱敏**。

### 3.7 飞书写入（复用 `D:\xhs-teardown\scripts\feishu_write.py`）
- 日期字段传**毫秒时间戳 int**
- 超链接字段传 `{"link": "...", "text": "..."}` 对象
- 单选/多选字段的**选项必须预先在表里建好**，否则报错
- **补选项时必须全量回传已有选项**，否则老选项被覆盖丢失
- **必须读飞书表的 live 字段类型**（`GET .../fields`），不能只信本地 schema
- wiki 内嵌多维表：URL 里的 token 是 `wiki_token`，**不是 `app_token`**，需先经 wiki API 解析
- 单批 50 条
- 幂等：先按 `source_url` 查询，命中即跳过

---

## 4. 成本估算

| 项 | 量 | 单价 | 日成本 |
|---|---|---|---|
| 抽取 | ~30 条 | $0.015 | ~$0.45 |
| 洞察 | 6 主题 | $0.015 | ~$0.09 |
| **合计** | | | **~$0.55/天，~$16/月** |

可接受。但若抽取量涨到 200 条/天 → $3/天，需重新评估是否换更便宜的模型。

---

## 5. 风险登记

| # | 风险 | 影响 | 缓解 |
|---|---|---|---|
| 1 | agentrouter 中转失效/涨价 | pipeline 停摆 | 抽象 LLM 调用层，换 base_url 即可切换 |
| 2 | Reddit RSS 被限流或关闭 | 英文一手经历源断供 | 已有 429 保护；后续补官方招聘页源 |
| 3 | **socai 登录态失效** | **中文源断供，`actual_bar`/`return_offer` 断流** | `reason:"login_required"` 时告警提示扫码；这是最需要监控的失败模式 |
| 4 | socai 是闭源第三方二进制 | 供应链/隐私风险；停止维护则失效 | 仅用于小红书单一站点；抓取内容只含公开帖子 |
| 5 | socai 触发风控验证码 | 抓取中断 | 抓取放缓、不并发，风控需人工过一次 |
| 6 | LLM 幻觉污染事实表 | 数据不可信 | 反幻觉校验 + evidence 强制 + confidence 标注 |
| 7 | LinkedIn / 小红书页面结构变更 | 解析失败 | 解析层按源隔离，失败只影响该源 |
| 8 | 飞书单选选项未预建 / 忘加协作者 | 写入报错 403 | 建表时一次性建全选项并加协作者 |

---

## 6. 开工前置条件

- [x] ~~飞书凭据~~ → **已解决**，`该自建应用（app_id 见 config.yaml 的 feishu.credential_file）`，实测有效
- [x] ~~小红书源~~ → **已解决**，socai 已登录，正文/OCR/评论全量可取
- [x] ~~合规条款是否为小红书开口子~~ → **已确认**（本人账号正常登录，非反爬绕过）
- [ ] **新建两张多维表**，并把 `该自建应用（app_id 见 config.yaml 的 feishu.credential_file）` 加为**协作者**（只给权限会 403）
- [ ] 确认 `evidence` 字段方案（推荐方案 A：JSON 字符串，见 `docs/字段设计.md`）
- [ ] 确认首轮小红书查询词库（`config.yaml` 里已给 8 个初稿）
- [ ] 确认 LLM 上游：继续用 agentrouter 中转？
- [ ] 安装 `pyyaml`

**剩余阻塞很少了** —— 最关键的「新建两张表 + 加协作者」做完即可开工第一批。
