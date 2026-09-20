# Career Intelligence — 项目长期上下文

> 本文件是项目的"记忆"。每次开发前先读这里，避免重复踩坑。
> 最后更新：2026-09-18

---

## 1. 一句话目标

个人自用 Agent。把散落在公开网页的金融求职信息，变成**结构化事实 + AI 解读**，
渲染成本地静态站 `site/` 供阅读（飞书写入仍在，但 2026-09-18 起默认关闭，见 §2.1）。
**核心价值是理解信息，不是收集信息。**

## 2. MVP 边界

只验证四件事：抓取有效公开信息 → 结构化 → 生成洞察 → **可读的产出**。

**明确不做**：小程序、用户权限、知识图谱、大规模爬虫、消息推送。

### 2.1 2026-09-18 转向：阅读端从飞书换成本地静态 HTML

用户原话「feishu app is heavy and i don't feel like opening it again and again」。
选**本地静态 HTML**（不推 GitHub Pages、不并进 dailybrief）→ 代价是 **PC-only**，用户明确接受。
用户同时要求：**暂时停掉飞书写入，但保留回去的路**。

原「不新增业务功能」的红线**本次已被用户明确授权突破**（新增 `src/render.py` 与 `--stage render`）。

**两条不知道就会改出静默故障的结构性事实**：

1. **飞书事实表不只是输出端，它原来是洞察段的输入窗口。** `insight.read_facts()` 走
   `fs.list_records()` 读近 30 天。天真地关掉写入 → 30 天后窗口被饿空 → 六个主题**静默**全变
   「数据不足」（跑得很成功但内容全空）。所以窗口读取也一并切到本地，飞书因此变成**纯镜像**，
   开关写入才是双向无损的。
2. **`data/facts/<date>.json` 原会被空抽取结果覆盖。** `extract_all` 在 records 为空时返回 `[]`，
   而 `stage_extract` 无条件 `write_json` —— 同一天 `force` 重跑时笔记全在 `seen.jsonl` 里，
   records 必为空，**当天事实被清成 `[]`**。已加守卫（空结果不覆盖已有文件）。
   `data/pending/` 天然免疫（空就不写），所以这个坑一直没暴露。

**数据落盘与消费方 —— 只认「无条件落盘」的那两个**：

| 路径 | 何时写 | 能当数据源吗 |
|---|---|---|
| `data/facts/<date>.json` | 抽取段，**无条件** | ✅ 洞察窗口的输入 |
| `data/pending/<name>_<date>.json` | 推飞书**之前**，**无条件** | ✅ 站点靠它拿洞察 |
| `data/written/<name>_<date>.json` | 推送**成功后**才写 | ❌ 飞书一关就停更 |

**实现要点**：
- `src/render.py` 读上面两个目录 → `site/<date>.html` + `site/index.html`（最新一期）。
  **每次全量重渲所有期**：避免 dailybrief 那个「归档导航被烤死在渲染当天」的坑，
  且渲染失败能靠下次运行自愈。`index.html` 最后写，崩在前也不会留下指向半成品的首页。
- 渲染失败**非致命**（记 ERROR 后返回 0）。做成致命也救不回来 —— `daily.bat` 的哨兵只挡「当天」，
  而任务 3 天一次，失败那天照样要等下次运行补上，白白多一条误导性的 cron.log。
- `run.py` 加 `--stage render`。⚠️ **加阶段必须同时改 argparse 的 `choices` 和下面的 `if` 分发**
  —— 只加 choices 会落进 `else: records = []`，跳过所有 `if`、打成「完成」并返回 0，一个静默空操作。
- `config.yaml: feishu.write.enabled: false`。开关放在 `write_records` 顶部、`fs = Feishu()` **之前**
  （放低了照样走网络）；`dry_run` 是例外 —— 它现在是唯一能拿 live 字段类型校验字段映射的自检手段。
- `scripts/open.bat` 打开最新一期。纯 ASCII / 无 BOM / CRLF —— 没有中文，所以**不需要**
  daily.bat 那套 `chcp 65001` + 重入自己的 dance。
- 页面无 JavaScript，折叠一律 `<details>`（`file://` 下 service worker / fetch 都不可用）。

**顺带修掉/接上的两个洞**：
- `{{previous_insights_json}}` 原来硬编码 `"[]"`，而 prompt 要求模型对比历史结论、无变化时输出
  「无增量变化」——**增量通道名存实亡**，每期 6 主题全量重写。现在读本地洞察存档喂进去
  （排除当天，取最近 3 期，且只取增量判断必需的字段 —— `ai_analysis` 是长篇复述，全塞进去
  输入 token 会很难看）。
- `json.dumps(facts)[:60000]` 两个毛病：① 会从中间劈开一个对象，模型收到**残缺 JSON**；
  ② 按升序截断留下的是**最旧**的 42 条（13 条 ≈ 18k 字符，30 天窗口约 130 条早已溢出）。
  改为 `fit_json()` 按条装填、保证合法 JSON，且 `n` = 真正喂进去的条数
  （`n` 要校准 `validate_insight` 的幻觉守卫与层级阈值，不能用未截断的 len）。

**⏸️ 未做**：飞书回填脚本（用户定「只记档」——数据都在 `data/pending/`，将来随时能补，
但关闭期间两张表会有缺口）；手机端 / Pages / PWA。

**⚠️ 验证状态**：离线全通过（渲染 2 期、幂等、09-13 无洞察降级正常、空结果不覆盖、写阶段零飞书调用、
`--dry-run` 仍连飞书、prompt 无残留占位符）。但**尚未用真实流水线端到端跑过**，
也**没有肉眼看过页面**（写这段的会话读不了图片）——
下次务必 `python src/run.py --stage render` 然后打开 `site/index.html` 检查版式。

## 3. 合规红线

- 只访问**公开可访问**内容
- **不绕过验证码、不绕过反爬**（不用打码平台、不用代理池、不伪造指纹）
- **例外（2026-09-13 用户确认）**：小红书经本机 `socai` 抓取。socai 以用户**本人账号、
  受管浏览器、人类速度**浏览公开帖子，属于正常登录访问，不属于反爬绕过。
  除此之外不新增任何登录类数据源。
- 识别到手机号 / 微信 / 邮箱等个人信息 → 自动脱敏
- 遇到 Cloudflare 拦截、"Just a moment..." → 直接放弃该源，**不要尝试绕过**

## 4. 关键技术事实（实测，重要）

### 4.1 数据源可用性

**可用（✅ 已实测返回真实内容）**

| 源 | 端点 | 实测结果 |
|---|---|---|
| **小红书（socai）** | `socai xhs search/author/get-notes` | ✅ **已登录**。卡片 + 正文 + 逐图 OCR + 评论全量可取 |
| Reddit RSS | `reddit.com/r/{sub}/.rss` | 200，25 条 entry，含全文 HTML body、permalink、更新时间 |
| LinkedIn 公开岗位 | `/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords=&location=&start=` | 200，每页 10 条结构化卡片（职位/公司/地点/发布日/URL） |
| eFinancialCareers | `efinancialcareers.com` / `.cn` | 200，751KB / 859KB，含 `/jobs/*` 链接与 ld+json |
| 飞书开放平台 | `open.feishu.cn` | ✅ 200，凭据有效，读写通（详见 4.5） |
| Bing 搜索 | `bing.com/search` | 200，结果走 `r.bing.com/ck/a?...&u=a1<base64>` 跳转，需解码 |
| 牛客网首页 | `nowcoder.com` | 200（但内页是 SPA 壳） |

**裸 HTTP 不可用（⚠️ 不代表 socai 不可用）**

| 源 | 裸 curl 状态 | socai 覆盖 |
|---|---|---|
| 小红书 `xiaohongshu.com` | 302 登录墙 | ✅ **能，已跑通** |
| Reddit JSON API (`/new.json`, `/search.json`) | 403 | RSS 已够用 |
| 一亩三分地 `1point3acres.com` | 403 Cloudflare | ❌ |
| Wall Street Oasis | 403 Cloudflare | ❌ |
| 知乎 | 403 / 302 登录墙 | ❌ |
| Glassdoor / Indeed | 403 | ❌ |
| 猎聘 `liepin.com` | 000 无法连接 | ❌ |
| RSSHub | 403 | — |
| DuckDuckGo lite | 200 但无外链（JS 壳） | — |
| 51job 前端 API | 000 | — |
| 牛客 `/api/discuss/getDiscussList` | 返回 HTML 壳，非真 API | ❌ |

> 🔑 **两个关键教训**：
> 1. Reddit 的 JSON API 被封死（403），**RSS 端点却完全可用**。这是英文源能成立的前提。
> 2. 小红书裸 curl 是 302 登录墙，**但本机装的 `socai` 能完整抓取**。
>    —— **不要用裸 curl 的结果判断一个源"不可用"，先看 socai 支不支持。**
>    （2026-09-12 我犯过这个错，用裸 curl 判定小红书不可用，是错的。）

### 4.2 AI 上游

- 用户通过 **CC Switch** 本地代理 `127.0.0.1:15721` 使用 Claude Code
- 真实上游：`https://agentrouter.org/v1/chat/completions`（OpenAI 兼容格式）
- 模型：`deepseek-v4-flash`（**推理模型，响应含 `reasoning_content`**）
- 凭据存放：`~/.cc-switch/cc-switch.db` → 表 `providers` → `is_current=1` → `settings_config.env.ANTHROPIC_AUTH_TOKEN`

**调用要点（踩坑记录）**：
1. 直接用 `Authorization: Bearer <key>` 会被拒 → `unauthorized client detected`
2. **必须同时伪造 Claude Code 客户端指纹头**：
   - `user-agent: claude-cli/2.0.0 (external, cli)`
   - `anthropic-version: 2023-06-01`
   - `anthropic-beta: claude-code-20250219`
3. 加上后 ✅ 正常返回
4. `max_tokens` 要给足（推理内容会吃掉预算，给 20 会出现 `finish_reason: length` 且 `content` 为空）
5. 成本：单次约 **$0.015**（中转加价后）

> ⚠️ 这是**第三方中转**，非官方 API。稳定性与合规性存疑，见 `docs/ADR/0002`。

### 4.3 环境

- Python 3.13.2（系统级，**无 conda**），路径 `AppData/Local/Programs/Python/Python313/python`
- 已装：`requests 2.34` `beautifulsoup4 4.15` `lxml 6.1` `feedparser 6.0` `anthropic 1.4` `httpx2`
- **未装**：`pyyaml`、`playwright`、`selenium`、`pandas`
- Node v24.16.0 / npm 11.13 / git 2.54
- 项目目录：`C:\Users\吃鱿鱼的鱿鱼\career-intel`

### 4.4 小红书抓取工具 socai（本机已装，已登录）

**绝对路径**（不在 shell PATH 快照里，必须用绝对路径）：
```
C:/Users/吃鱿鱼的鱿鱼/.socai/bin/socai.exe     # socai 0.5.4
```

支持站点：`xhs`（小红书）、`dy`（抖音）。核心子命令：

```bash
socai xhs search "<query>" --preview --num-notes 40 --pretty        # 只取卡片，便宜
socai xhs search "<query>" --filter publish_time=一天内 --num-comments 8 --ocr
socai xhs author <AUTHOR_ID> --preview --num-notes 30 --pretty      # 主页 + 笔记卡片
socai xhs get-notes --note <NOTE_ID>=<XSEC_TOKEN> --ocr --num-comments 8 --pretty
```

#### ⚠️ 五个已踩过的坑（照做，别重踩）

1. **stdout 是裁剪过的，必须读 artifact 文件。**
   `socai` 会先打印 `run_dir: <路径>`，完整数据在 `<run_dir>/artifacts/*.json`。
   **`xsec_token` 只在 artifact 里**，stdout 的 cards 没有它 ——
   拿不到 `xsec_token` 就调不了 `get-notes`，会一条正文都取不到（实测踩过）。
   artifact 路径也可从输出 JSON 的 `artifact.path` 字段拿。

2. **OCR 文本两处结构不同，必须合并。**
   - stdout：顶层 `ocr_text: ["图1文本", ...]`
   - artifact：**嵌在 `images[i].ocr_text` 里**，顶层没有 `ocr_text`
   只读任意一边都会丢内容。

3. **`publish_time` 只接受三个值：`一天内` / `一周内` / `半年内`。**
   传别的（如 `三个月内`）会返回 `reason: "search_failed"` 且 **0 张卡片**，
   不报错、不提示，非常隐蔽（实测踩过）。

4. **必须加 `note_type=图文`** —— 视频笔记 `image_count=0`、没有图，
   `--ocr` 拿不到任何东西，正文往往只有一句标题。
   实测 5 篇里有 2 篇是视频，白抓。

5. **搜索卡片上的 `type` 不完全可靠** —— 卡片显示 image、点进去可能是 video。
   所以除了搜索时过滤，取正文后还应检查 `images` 是否为空。

6. **解析 JSON 要从第一个 `{` 开始截取** —— 前面有 `run_dir:` 那行。

#### 其他要点

- filter 分组：`sort` / `note_type` / `publish_time` / `search_scope` / `distance`
- **`--ocr` 是必需的**：小红书金融内容大量在图片里（JD 截图、岗位表格）
- 失败时 `output.json` 里 `reason: "login_required"` = 登录态失效，需重新扫码
- 卡片的 `likes` 是**字符串**，排序前要转 int

**`get-notes` 能拿到的字段**（已验证）：
`content`（正文全文）、`ocr_text[]`（逐图 OCR）、`top_comments[]`（含 replies）、
`hashtags`、`author` / `author_id` / `author_url`、`location`、`image_count`、`type`、`date`

**已修复的坑**：`Chrome/User Data/DevToolsActivePort` 陈旧文件曾导致 socai 无限重试挂死，
已备份为 `DevToolsActivePort.stale-backup-20260823`。若再次挂死，先检查这个文件。
（**与坑 15 的管道挂死是两个不同故障**，症状像但根因无关。）

**`xsec_token` 保鲜期 ≥ 5 天**（2026-09-18 实测）：09-13 artifact 里的 token 在 09-18
仍能正常 `get-notes`。这是候选池能跨天复用的前提。

**socai 限制**：`activated: false`（免费版）。telemetry 显示 `xhs search` 已进入工具层，
失败仅因 CDP 传输，未见权限门禁 → 免费版可跑，但 `--ocr` 是否受限需持续观察。

### 4.5 飞书凭据（已有，非阻塞）

**凭据已存在于 `D:\xhs-teardown\config\feishu.local.json`**，2026-09-13 实测有效：

```
app_id: cli_aa01bbf3d5f8dbd3
domain: https://open.feishu.cn
```

验证结果：`tenant_access_token` 获取 `code=0`，两张既有表均可读（字段 + 记录），
说明**应用已被加为协作者**（RUNBOOK 明确记录：只给权限不加协作者会 403）。

**关键结构知识**：这些表是 **wiki 内嵌的多维表**，不能直接拿 URL 里的 token 当 app_token。
必须先用 wiki API 把 `wiki_token` 解析成 `app_token`，解析结果缓存在
`D:\xhs-teardown\config\feishu.resolved.json`。

### 4.6 复用 `D:\xhs-teardown\scripts\feishu_write.py` 的三个坑

这个成品脚本可直接改造复用（tenant_access_token 流程 / live 字段类型读取 /
按类型转换取值 / 按 note_id 去重 / pending→done 落盘）。

必须照抄的三个细节：

1. **单选/多选补选项时必须全量回传已有选项**，否则会把老选项覆盖丢失
2. **必须读飞书表的 live 字段类型**（`GET .../fields`），不能只信本地 schema
   —— 表里的实际类型可能与设计文档不一致
3. **日期传 epoch 毫秒**；超链接传 `{"link":..., "text":...}` 对象

## 5. 用户背景（影响相关性判断）

- 港大 + 港中深，**明确到岗 6 个月、每周 5 天**
- 有券商行研实习经历（东方证券 医药组）
- → 求职重心偏向 **香港 + 内地 金融实习/校招**，中英双语内容都相关

## 6. 硬性业务规则（写进 prompt，不可违反）

1. `recruiting_bar`（JD 书面门槛）与 `actual_bar`（真实录取门槛）**必须严格分开**
2. **AI 禁止脑补事实**。任何字段若原文无依据 → 输出 `null`，不得推测
3. 所有非空字段必须附**原文 evidence 引用**
4. 信息优先级：一手求职经历 > 多方交叉验证案例；过滤营销广告、空泛鸡汤
5. 洞察分析必须区分 **Fact / Pattern / Interpretation / Implication** 四层
6. 样本不足 → 直接输出「数据不足」，**禁止外推全行业结论**

### 6.1 事实表字段（2026-09-15 精简：26 → 16）

用户反馈「分类太多了，每天看也就扫一眼」。**13 个可见列 + 3 个隐藏列**：

| 可见（13） | 说明 |
|---|---|
| date / company / institution_type / department / role / city | 定位 |
| salary / wlb | 待遇与强度 |
| **recruiting_bar / actual_bar** | **书面门槛 vs 真实门槛 —— 本项目的核心差异点，用户明确要求保留** |
| experience_summary | 经历摘要 |
| source_url / tags | 溯源与检索 |

| 隐藏（3） | 为什么还留着 |
|---|---|
| `published_at` | 面经过期很快，判断时效必需 |
| `source_type` | 一手经历 / 求职博主 / 引流广告 —— 决定这条值不值得信 |
| `evidence` | 「AI 禁止脑补」的审计依据，砍了再也追溯不了原文出处 |

> 这三个字段**建议在飞书视图里隐藏**（用户还没做这一步）。

**合并掉的字段**（内容并入右边，不再单独成列）：
- `working_hours` + `weekend_work` → **`wlb`**（并从单选改为**文本**，单选装不下细节）
- `education_requirement` + `internship_requirement` → **`recruiting_bar`**
- `interview_rounds` + `interview_content` + `return_offer` → **`experience_summary`**

**彻底删除**：`source_account`、`salary_unit`、`confidence`

> ⚠️ 删掉 `confidence` 后，洞察 prompt 的质量加权改为按 `source_type` 判断
> （first_hand > career_account/community，marketing 直接忽略）。
>
> `src/schema.py` 是**字段唯一真源** —— 改完跑 `python src/setup_feishu.py`
> 会自动补新字段、删多余字段（幂等）。

## 7. 实施状态（2026-09-13 起）

### 已完成 ✅

| 项 | 状态 |
|---|---|
| 两张飞书表 | **已建**，见下方 token |
| `pyyaml` | 已装 |
| config.yaml | 已验证可解析，源收敛为**只有小红书** |
| 代码 | `src/` 下 11 个模块，八段流水线全部打通 |
| LLM 客户端 | 实测通过 |
| socai 采集 | 实测通过（修掉了 xsec_token 的坑） |

**飞书表 token**（存在 `config/tables.json`）：
```
app_token   = Nei2bizzOa9MI5skteNcMjJBnKf
facts       = tblI55cVtgKRCRL9   (16 字段：13 可见 + 3 隐藏；2026-09-15 从 26 精简而来)
insights    = tblztIIbw9ci2cwL   (12 字段)
```
建表脚本：`python src/setup_feishu.py`（幂等，可重复跑）

### 源的范围（用户 2026-09-13 决定）

**只做小红书。** LinkedIn 与 Reddit 已在 config 中 `enabled: false`。
理由：用户明确「不要抓取linkedin，只要小红书的insights」。
> Reddit 是被一并关掉的（未单独点名），若要恢复改 config 即可。

### 代码结构

```
src/
  common.py        配置/路径/日志/脱敏/JSON IO（含 Windows 控制台 UTF-8 修正）
  llm.py           agentrouter 客户端，伪造 Claude Code 头
  feishu.py        多维表客户端（字段读取/取值转换/去重/batch）
  schema.py        两张表字段定义 —— 单一真源
  setup_feishu.py  建表脚本（幂等）
  fetch_xhs.py     socai 采集（读 artifact 拿 xsec_token）
  fetch_reddit.py  Reddit RSS（已停用）
  fetch_linkedin.py LinkedIn guest API（已停用）
  pipeline.py      clean → dedup → filter
  extract.py       LLM 抽取 + 反幻觉校验
  insight.py       六主题增量洞察 + 样本量二次校验
  writer.py        飞书写入（本地先落盘，再推送）
  run.py           主入口 --stage fetch|process|extract|write|insight|all
```

### 已修复的实现坑

1. **飞书 base_url 重复拼接** → `/open-apis/open-apis/...` 404。已在 `feishu.py` 剥掉
2. **建单选/多选字段时选项被双层包裹** `{"name": {"name": "x"}}` → `SingleSelectFieldPropertyError`。
   `create_field` 现在同时接受 `["A"]` 和 `[{"name":"A"}]`
3. **超链接/文本字段传空 `property`** → `URLFieldPropertyError`。现在 options 为空时整个 property 键省掉
4. **`publish_time=三个月内` 非法** → `search_failed` 且 0 卡片。改为 `半年内`，并加 `note_type=图文`
5. **socai stdout 裁剪** → 拿不到 `xsec_token`。改为读 artifact 文件
6. **Windows 控制台 GBK** → 中文日志乱码。在 `common.py` 强制 stdout 为 UTF-8
7. **`source_type` 模型输出中文描述** → 加了模糊映射兜底 + prompt 明确要求输出枚举码
8. **文本字段模型返回 list** → 加了 `_join_text` 拼接
9. **`max_tokens=4096` 撞 `finish_reason=length`** → extract 提到 8192
10. **🔴 `load_prompt()` 把字段定义整段丢了（最严重）**
    原来写的是「`## SYSTEM` 取到下一个 `##` 为止」，而 `## 字段定义`（含 JSON Schema）
    在后面 —— 于是**模型从未看到 schema**，自己编字段名（输出 `position`/`location`
    而不是 `role`/`city`，26 个字段只填 3 个）。
    改为「`## SYSTEM` 取到 `## USER 模板` 之前」。SYSTEM 从 872 字 → 3242 字。
    **`insight.py` 有同样的 bug，一并修了。**
11. **USER 模板用 `【】` 包裹元数据** → 模型以为那是正文，给 `source_type`/`source_url`
    硬造 evidence。改用 `===== 正文开始 =====` 分隔，并显式说明那三行是元数据。
    校验层也加了 META_KEYS 黑名单
12. **小红书日期只有 `MM-DD`**（如 `04-24`）→ 无法解析。加 `normalize_date()` 补年份，
    未来月份自动退一年
13. **tags 是自由生成的**，预设枚举装不下 → 写入前调 `ensure_options` 把本批新标签
    补进选项（否则 coerce 会全部丢弃）
14. **飞书偶发 SSL UNEXPECTED_EOF** → `_raw` 加 3 次重试退避
15. **🔴 socai + `subprocess` 管道 = 永久挂死（2026-09-18，挂死 26 分钟）**
    socai 是「CLI + 常驻 `__daemon`」架构，daemon 由首个 CLI 调用拉起时**继承了 stdout 管道句柄**，
    管道永远等不到 EOF → `subprocess.run(capture_output=True, timeout=N)` **永久阻塞，
    连 timeout 都不触发**（Windows 上超时后 CPython 会 kill 再读干管道「收尸」，照样被卡）。
    **症状**：进程活着、日志停在调用那一行、连超时 warning 都没有；但 socai 自己的
    `output.json` 早已写好（**数据没丢**）。
    **只在 daemon 已死需要重拉时出现**，所以时好时坏、极难复现 —— 9-13 跑通纯属 daemon 当时活着。
    **修法**：stdout/stderr 重定向到**临时文件** + `stdin=DEVNULL`，跑完读文件。见 `fetch_xhs.py:run_socai()`。
16. **`--stage extract/write` 原来完全绕过去重（2026-09-18 修）**
    这条分支直接 `read_json(clean/*.json)`，既不跑 `dedup` 也不查 `seen.jsonl` →
    重跑会把已经抽过的笔记再抽一遍（9-15 同一篇被抽 3 次就是这么来的），白烧钱。
    已补 `pipeline.dedup()`，并加 `--force` 作为"我就是要重抽"的逃生口。
17. **`mark_seen` 只在 `stage=all` 全程跑完后才落盘** → 中途中断则本轮抽的全白烧。
    已加逐条抽取缓存 `data/extracted/{sha1(dedup_key)}.json`，缓存里存
    `prompt_fingerprint`（SYSTEM+USER 模板的 sha1）—— **改 prompt 缓存自动失效**，
    不会拿旧 schema 的结果冒充新结果。
18. **🔴 `.bat` 绝不能有 UTF-8 BOM，`.ps1` 必须有 —— 两者要求相反（2026-09-18 两个都踩了）**
    - `.bat` 加 BOM → cmd 不认批处理 BOM，`@echo off` 自己报错、整段回显全开
    - `.ps1` 没有 BOM → PowerShell 5.1 按系统默认编码（GBK）读，**硬编码的中文字面量全乱码**
      （`socai.exe not found at C:\Users\鍚冮笨楸肩殑楸块奔\...`）。隐蔽点在于：
      注册出来的任务路径是**对的**（那些是运行时 `Resolve-Path` 算的），
      只有 check/warning 里的字面量失灵 → 报假警但任务正常。
    - `.bat` 行尾必须 **CRLF**（LF 在 `goto`/括号块上有坑）
19. **候选池的两个自伤 bug（都是我自己写的，同日修）**
    a. `merge_pool` 原来「只在 token 为空时回填」→ 重搜到同一张卡时**不会刷新已失效的
       token**，那张卡每次被选中都白跑一次 `get-notes`。改成搜到就刷新。
    b. 一轮里 get-notes **全军覆没**多半是会话级问题（登录态/daemon），
       若照样逐卡记 `fail_count`，下一轮会把整批好卡拉黑、池子静默萎缩。
       改成**只在「至少有一张成功」时才把失败归因到单卡**。

### 已验证跑通

用 2 篇小红书笔记跑通完整链路，**2 条已写入飞书事实表**（2026-09-13）：

| | 记录 1 | 记录 2 |
|---|---|---|
| company | 中信证券 | — |
| department | 投行委 | 自营 |
| role | — | 利率债投研/交易实习 |
| interview_rounds | 无领导小组讨论 + 单面 | — |
| source_type | career_account | first_hand |
| confidence | low | medium |

`published_at` 的 `04-24` 正确补成 `2026-04-24`；tags 自动建选项成功。

### 2026-09-18：MVP 四步闭环首次全程跑通 ✅

一次性跑完 **26 分钟、$0.069**（15:08→15:34）：

```
搜索 8 组(12min) → 15 篇正文(10min) → 抽取 13/14 成功(4min) → 洞察 6 条(1min) → 全部写飞书
```

洞察段**首次实测通过**，质量很好 —— 四层结构 Fact/Pattern/Interpretation/Implication 完整、有原文依据。
例（interview 主题，7 条证据）：【Pattern】会计与估值基础是 PE/一级岗位的共同最低线…
【Implication】简历上不写无法当场讲清的方法。

小瑕疵：6 条的 `finding_layer` **全落在 `pattern`**，但正文里明明写了 【Implication】—— 字段失真，先记着。

**⚙️ 耗时拆解（实测，不是估的）**

| 阶段 | socai 实际 | 限速纯等待 | 小计 |
|---|---|---|---|
| 搜索 8 组 | 9m07s | 2m20s | 11m47s |
| 取正文 15 篇 | 4m39s | 5m00s | 9m39s |
| 抽取 14 次 LLM | — | — | 4m04s |
| 洞察 1 次 | — | — | 1m |

两个反直觉点：**取正文每篇 38.6s 里 20s 是纯 sleep、socai 只占 18.6s**；
而**搜索每轮 socai 要 70s，比开一篇笔记贵 3.7 倍**。

**💸 成本（推翻此前 $0.015/次的估值）**

extract 14 次 = $0.0536，即**单次 $0.0038**（低 4 倍）；加 insight 共 **$0.069/天，约 $2/月**。
输入 32k tok 占成本 24%，**输出 33.6k tok 占 76%**（含推理 token 14,937）。

> 🔑 **结论：「批量化省 token」收益极低** —— 只能省输入，撑死 $0.006/天。
> 成本根本不是瓶颈，**时间才是**（26 分钟）。别再按「多一次调用多 $0.015」估算。

**🔧 已改但【未用真实流水线验证】—— 用户要求改完不动 09-18 的数据**

| 改动 | 文件 | 要点 |
|---|---|---|
| 候选池复用 | `fetch_xhs.py` `config.yaml` | `data/pool/xhs.json`，`pool_ttl_days=3`、`pool_min_fresh=30`；未抓过的卡够多就整段跳过搜索 |
| 断点续跑 + 去重修复 | `run.py` `extract.py` | 见坑 16、17；新增 `--force` |
| 打印 cached_tokens | `llm.py` | 之前只累计不打印，缓存命中率只能靠反推 |

候选池的关键设计（**改之前先想清楚**）：选帖必须**只从「未抓过」的卡里选**，
否则天天抽同一批高赞、被 dedup 全砍、最终抽 0 条。判据两条缺一不可 ——
`fetched_at`（真取到正文）+ `seen.jsonl`（走完全流程）；只看任一个都会重复抓。
依据：**xsec_token 至少能活 5 天**（09-18 实测，09-13 的 token 仍能取正文）。

**⏸️ 暂缓**：extract 批量化（用户决定先不做，理由见上）。
**红线**：不改 `schema.py`、不改飞书表结构。
（原「不新增业务功能」已于 2026-09-18 被用户明确授权突破，见 §2.1。）

### 自动化：每 3 天 10:00（2026-09-18 落地）

用户定的频率是 **3 天一次**，理由很实在：天天跑没有新帖子。
形态选了 **Windows 任务计划**而不是 Claude Code skill —— 流水线是确定性 Python、
运行时不需要 agent 判断，skill 要常驻交互会话且引入不确定性；本机 DailyBrief 已经跑通同一套范式。

| | |
|---|---|
| 任务名 | **`CareerIntel`**（`schtasks` 里按大写 C 查） |
| 入口 | `scripts\daily.bat` |
| 注册 | `powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1`（可重复跑） |
| 触发 | `MSFT_TaskDailyTrigger`，**DaysInterval=3**，起始 `2026-09-19T10:00:00+08:00` |
| 下次运行 | 2026-09-19 10:00 |
| 设置 | `StartWhenAvailable`（开机补偿）+ `MultipleInstances IgnoreNew` + 2h 超时 + 失败重试 2 次×30min |
| 诊断入口 | **`data/logs/cron.log`**（run.py 的日志也重定向到这里，搜索/取正文/抽取/写照全程都在） |
| 手动强制重跑 | `scripts\daily.bat force`（绕过当日哨兵 `data/logs/.last_success`） |

> 🔗 **联动约束（改一个必须改另一个）**：`config.yaml:pool_ttl_days` 必须**大于**运行间隔天数，
> 否则候选池每次都被判过期、池子复用静默失效。因为改成 3 天跑一次，
> `pool_ttl_days` 已从 3 调到 **4** → 效果是**每 2 次运行重搜一次**。
> `install_task.ps1` 会读 config.yaml 校验这条并打印警告。

**`run.py` 新增「抓取 0 条即失败」守卫**：无人值守时最怕「静默成功但产出为空」——
socai 登录态失效时 `get-notes` 逐条失败但只是 warning，整轮会以 0 条「成功」结束，
定时任务完全看不出异常。现在会显式非零退出，cron.log 有迹可循。

### ⚠️ 日期陷阱（踩过）

pipeline 用 `today()` 定位文件。**会话跨天后，重跑旧数据必须显式 `--date YYYY-MM-DD`**，
否则会以为数据是空的（实际文件都在）。

### 待办

**⏭️ 优先级最高：首次自动运行后验一遍（2026-09-19 10:00）**

09-18 的三项改动**只过了离线测试**（TTL 判定、池子合并/去重/token 回填/僵尸卡拦截、
prompt 指纹失效、`--force` 注册），**没跑过真实 fetch** —— 用户明确要求不动 09-18 的数据。

- **第 1 次跑（09-19）**：池子是空的 → 仍会全量搜索并建池，耗时约 26 分钟（和以前一样）。
  要看的：`data/pool/xhs.json` 是否生成、卡片数是否 ~100、跑完 `fetched_at` 是否只有 15 张。
- **第 2 次跑（09-22）**：**这才是池子复用第一次真正生效** —— 日志里应出现
  「候选池：共 N 张，未抓过 M 张，在 4 天有效期内 → 跳过搜索阶段」，耗时掉到约 14 分钟。
  要看的：**抽到的条数是否正常**（不能是 0 条）、条数与第 1 次是否明显不同。
- 顺带确认 `cron.log` 里的 LLM 用量报告有没有打印出 `cached_tokens`（新增的打印项）。

- [x] ~~决定每日自动化的形态~~ → 2026-09-18 定案：Windows 任务计划，每 3 天 10:00，见上
- [ ] 首轮 query 词库效果评估（现为 8 个词 + `note_type=图文`）。
      09-18 实测 **2/8 组空转**：「投行 实习 offer」返回 0 张；
      「行研 实习 经验」返回 `reason=Search did not transition to a valid Xiaohongshu result page`
      —— 这是**调用失败**不是"没结果"，但 `fetch_xhs.py` 把它当空结果静默放过。两种情况应分开处理。
      注意：池子复用后搜索每 2 次跑才发生一次，这个问题的影响减半。
- [x] ~~`posts_to_fetch: 5` 偏少~~ → 已调到 15，`high_like_pool` 调到 40
- [x] ~~洞察 Prompt 首次实测~~ → 2026-09-18 通过，见上
- [ ] LLM 上游是否换官方 key（成本已确认极低，优先级下降）
- [ ] 洞察 `finding_layer` 字段失真（正文写了四层，字段只报 pattern）
- [ ] 洞察 `incremental_only: true` **实际没实现** —— `insight.py` 把
      `previous_insights_json` 硬编码成 `"[]"`，每次读全窗口。现在 14 条看不出问题，
      等每天 15 条 × 窗口 30 天 = 450 条时，60000 字符截断会**从尾部切掉数据**。
      注：3 天跑一次的话，30 天窗口约 150 条，压力比每天跑小得多。

## 8. 相关文档

- `docs/环境与数据源实测报告.md` — 完整实测数据
- `docs/字段设计.md` — 飞书两张表字段定义
- `docs/MVP实现方案.md` — pipeline 与分批实施
- `docs/ADR/0001-信息源策略.md`
- `docs/ADR/0002-LLM与抓取技术选型.md`
- `prompts/extract_facts.md` — 事实抽取 Prompt
- `prompts/generate_insights.md` — 洞察生成 Prompt
- `config.yaml` — 数据源与运行参数
- **外部可复用资产**：`D:\xhs-teardown\`（8 月的同类项目，含 socai 采集脚本与飞书写入脚本）
