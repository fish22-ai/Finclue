#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""渲染本地阅读端：data/facts/*.json + data/pending/insights_*.json → site/*.html

2026-09-18 起这是主要的**阅读**入口 —— 飞书 App 太重，改成打开本地静态页。
（写飞书仍在，但默认关闭；见 config.yaml 的 feishu.write.enabled。

三条设计约束，都是踩过的坑：

1. **每次全量重渲所有期**，index.html 最后写。
   dailybrief 那边的教训：归档导航是渲染时烤进每一页的，只渲最新一期会让老页面的
   导航永远停在它自己被渲染的那天。全量重渲顺带让「某次渲染失败」能在下次运行自愈
   （定时任务 3 天一次，失败的当天不会有人回来单独补渲）。

2. **没有 JavaScript**。折叠一律用 <details>。这张页面要走 file:// 打开，
   file:// 下 service worker / fetch 都不可用，别引入依赖它们的写法。

3. **数据来源必须是「无条件落盘」的文件**：
   data/facts/<date>.json（抽取段写）与 data/pending/insights_*.json（推飞书之前写）。
   ⚠️ 不要读 data/written/ —— 那是推送**成功后**才落的 archive，飞书一关就停更。

版式与 dailybrief 同族不同形：共用一套设计 token（配色/字体/圆角/阴影那一层），
但**不**复刻它的卡片流 —— 那边是顺序读的长文；这边洞察是「六个主题的 tab，点开看」，
事实是「每个来源一张卡、卡内做书面门槛 vs 真实门槛的对照」。
"""

import html
import io
import os
import urllib.parse

from common import LOG, ensure_dir, local_dated_files, path, read_json
from schema import TOPICS

SITE_DIR = path("docs")

# 主题 → 中文名。取自 prompts/generate_insights.md 的「分析主题」小节，
# 那边是这六个主题的权威写法，别另起一套。
TOPIC_LABELS = {
    "recruiting_bar": "招聘 Bar",
    "wlb": "WLB",
    "salary": "薪资",
    "hc_trend": "HC 趋势",
    "interview": "面试特征",
    "return_offer": "留用机会",
}

LAYER_LABELS = {
    "fact": "Fact",
    "pattern": "Pattern",
    "interpretation": "Interpretation",
    "implication": "Implication",
}

CONF_LABELS = {"high": "高", "medium": "中", "low": "低"}

# 「这期没信号」的两种 finding：都要显示成灰色占位，不删 ——
# 「哪个主题这期没信号」本身就是信息，删掉就看不出来了。
NO_SIGNAL = ("数据不足", "无增量变化")

EMPTY = "—"


# --------------------------------------------------------------- 取值
def flat(v):
    """飞书风格字段值 → 字符串。本地 JSON 与飞书字段值形态一致（list/dict/标量）。"""
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        parts = []
        for x in v:
            if isinstance(x, dict):
                parts.append(x.get("text") or x.get("name") or x.get("link") or "")
            else:
                parts.append(str(x))
        return " ".join(p for p in parts if p).strip()
    if isinstance(v, dict):
        return (v.get("text") or v.get("link") or "").strip()
    return str(v).strip()


def source_link(v):
    """source_url → (href, 显示文本)。只放行 http/https。

    数据是从小红书抓来的，不是自己写的 —— 拼进 href 前必须过一遍 scheme，
    否则一个 javascript: 的 url 就是一个注入口子。
    """
    raw = flat(v)
    if not raw:
        return None
    u = urllib.parse.urlparse(raw)
    if u.scheme not in ("http", "https") or not u.netloc:
        return None
    return raw, "原文"


def esc(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def or_dash(s):
    return esc(s) if s else '<span class="na">%s</span>' % EMPTY


def tags_of(rec):
    t = rec.get("tags")
    if isinstance(t, list):
        return [str(x).strip() for x in t if str(x).strip()]
    s = flat(t)
    return [x.strip() for x in s.split(",") if x.strip()] if s else []


# --------------------------------------------------------------- 读数据
def load_issues():
    """→ {date: {"facts": [...], "insights": [...]}}，日期升序。

    事实文件是基准：没有任何事实的日期不成期（只有洞察没事实是异常状态）。
    """
    issues = {}
    for d, p in local_dated_files("facts"):
        rows = [r for r in (read_json(p, []) or []) if isinstance(r, dict)]
        if rows:
            issues.setdefault(d, {"facts": [], "insights": []})["facts"] = rows

    for d, p in local_dated_files("pending", prefix="insights_"):
        rows = [r for r in (read_json(p, []) or []) if isinstance(r, dict)]
        if rows:
            # ⚠️ 有洞察但没事实文件（或事实为空）的日子不单独成期，挂到已有期上，
            #    否则会渲出一个只有洞察、没有任何事实的残缺页面。
            issues.setdefault(d, {"facts": [], "insights": []})["insights"] = rows
    return issues


# --------------------------------------------------------------- CSS
# 设计 token 层与 dailybrief 同源（字体/圆角/阴影/结构那一层），但配色是另一族：
# **宣纸 + 朱红 + 金**（2026-09-18 用户指定，中国风方向）。要的是与 dailybrief 的
# 暖米+锈红一眼分得清，同时贴「金线索」这个名字，也避开绿色 —— 中国市场绿=跌，
# 用户明确说「不吉利」。中间试错过三轮（松石绿+烟雨蓝 → 绿色系 → 实底藏红），
# 都被否掉，别回头走这几条路。
#
# ⚠️ 「红太红」的解法是**面积**不是色相：整条顶栏铺实底藏红是最刺眼的那次。
#    现在顶栏是纸底 + 一道朱红细线，红只出现在细线/链接/小徽标/门槛左条上。
#
# 色锚点分工：
#   --accent 朱红  细线 / 链接 / 选中 tab / 薪资·强度 / 真实门槛左条 / lv-pattern
#   --gold   金    星标 logo / lv-implication / 书面门槛左条
#   --ink    墨    正文与标题（顶栏字也是墨色，不再是反白）
# dailybrief 在暗色模式下要逐条覆盖 .archive a 之类的组件规则（因为那边写死了颜色），
# 这里所有组件只用 var()，所以暗色模式只需要覆盖 token 块，不需要第二条暗色规则。
CSS = """*{box-sizing:border-box;margin:0;padding:0}
:root{
 --bg:#f8f4ed;--card:#fffdf9;--line:#e7ded1;--ink:#2a2320;--dim:#6b6058;
 --faint:#9d928a;
 --accent:#9e3d47;--accent-bg:#f7eae9;--accent-line:#e6cdcc;--accent-ink:#8e3741;
 --gold:#c9a227;--gold-bg:#f8f0d8;--gold-line:#e8d7a8;--gold-ink:#8a6c12;
 --bar-paper-bg:#f6efe3;--bar-paper-line:#e6d9c4;--bar-paper-ink:#8a6a44;
 --bar-real-bg:#f7eae9;--bar-real-line:#e6cdcc;--bar-real-ink:#8e3741;
 --pill:#f5efe6;--pill-line:#e6dccd;
 --serif:Georgia,"Songti SC","Noto Serif CJK SC","SimSun",serif;
 --card-r:12px;
 --shadow:0 1px 2px rgba(90,60,40,.05),0 10px 24px -14px rgba(90,60,40,.16);
}
@media(prefers-color-scheme:dark){:root{
 --bg:#1a1614;--card:#221d1a;--line:#3b332e;--ink:#f2ece5;--dim:#b8aca1;
 --faint:#8a7f76;
 --accent:#d29aa0;--accent-bg:#302325;--accent-line:#4d383b;--accent-ink:#e0b2b7;
 --gold:#d9b64a;--gold-bg:#2b2415;--gold-line:#473b1e;--gold-ink:#e0c169;
 --bar-paper-bg:#282119;--bar-paper-line:#42382a;--bar-paper-ink:#cbb493;
 --bar-real-bg:#302325;--bar-real-line:#4d383b;--bar-real-ink:#e0b2b7;
 --pill:#282220;--pill-line:#3d3430;
}}
body{font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;
 background:var(--bg);color:var(--ink);padding:22px 18px 60px}
.wrap{max-width:1560px;margin:0 auto}
a{color:var(--accent)}
/* 顶栏走中国风 + 报纸报头：居中排，字号拉开，上下各一道线（上细下朱红）。
   红只留在下面那道 2px 线上 —— 「红太红」的解法是面积，不是色相。 */
header{text-align:center;padding:13px 0 14px;border-top:1px solid var(--line);
 border-bottom:2px solid var(--accent);margin-bottom:2px}
header .logo{display:block;color:var(--gold);font-size:19px;line-height:1;margin-bottom:7px}
.hd{min-width:0}
h1{font-family:var(--serif);font-weight:700;font-size:27px;color:var(--ink);
 letter-spacing:.5px;line-height:1.15;display:flex;align-items:baseline;
 justify-content:center;gap:10px;flex-wrap:wrap}
h1 .cn{font-size:14px;font-weight:500;color:var(--accent);letter-spacing:3px}
.archive{display:flex;flex-wrap:wrap;gap:6px;margin:14px 0 4px}
.archive a{font-size:12px;padding:3px 10px;border-radius:11px;background:var(--pill);
 border:1px solid var(--pill-line);color:var(--dim);text-decoration:none}
.archive a:hover{border-color:var(--accent-line)}
.archive a.cur{background:var(--accent-bg);border-color:var(--accent-line);
 color:var(--accent);font-weight:600}
h2{font-family:var(--serif);font-size:16px;margin:26px 0 10px}
.badge{font-size:11px;padding:2px 8px;border-radius:9px;background:var(--pill);
 color:var(--dim);border:1px solid var(--pill-line);white-space:nowrap}
.badge.lv-pattern{background:var(--accent-bg);color:var(--accent-ink);border-color:var(--accent-line)}
.badge.lv-implication{background:var(--gold-bg);color:var(--gold-ink);border-color:var(--gold-line)}
.badge.none{background:transparent;color:var(--faint)}
.na{color:var(--faint)}
.muted{color:var(--faint);font-size:12.5px}
/* ── 洞察：tab 切换（纯 CSS，无 JS） ──
   radio + label + :checked ~ 兄弟选择器。file:// 下 service worker / fetch 都不可用，
   所以不引 JS；六个面板始终在 DOM 里，只是 display 切换。 */
/* 隐藏 radio。用 1px+opacity 而不是 left:-9999px —— 负向偏移在部分安卓浏览器上
   会撑出横向滚动。radio 仍可 Tab 聚焦、方向键切换，键盘操作不受影响。 */
.tabs>input{position:absolute;width:1px;height:1px;opacity:0;margin:0}
.tabbar{display:flex;flex-wrap:wrap;gap:6px;border-bottom:1px solid var(--line);
 padding-bottom:0;margin-top:12px}
.tabbar label{cursor:pointer;font-size:12.5px;padding:6px 13px;border-radius:9px 9px 0 0;
 background:var(--pill);border:1px solid var(--pill-line);border-bottom:none;
 color:var(--dim);display:inline-flex;align-items:center;gap:6px;
 user-select:none;margin-bottom:-1px}
.tabbar label:hover{color:var(--ink)}
/* 没信号的主题在 tab 上就要看得出来 —— 否则「六个主题固定占位」用来暴露
   「哪个主题这期没信号」的价值就没了。 */
.tabbar label.empty{opacity:.55}
.tabbar label.empty::after{content:"无信号";font-size:10px;color:var(--faint);
 background:var(--pill);border:1px solid var(--pill-line);border-radius:5px;
 padding:0 4px}
.panels{margin-top:14px}
.panel{display:none;background:var(--card);border:1px solid var(--line);
 border-radius:var(--card-r);padding:15px 17px;box-shadow:var(--shadow)}
.tc-h{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.tc-name{font-weight:600;font-size:14.5px;font-family:var(--serif)}
.tc-h .ev{margin-left:auto;font-size:11px;color:var(--faint)}
.finding{font-size:13.5px;line-height:1.68;margin:10px 0}
.panel .tc-f{margin-top:12px}
.tc-d summary{cursor:pointer;font-size:12px;color:var(--accent);list-style:none}
.tc-d summary::-webkit-details-marker{display:none}
.tc-d summary::before{content:"▸ ";color:var(--faint)}
.tc-d[open] summary::before{content:"▾ "}
.tc-d dl{margin-top:9px;padding-top:9px;border-top:1px dashed var(--line)}
.tc-d dt{font-size:11.5px;color:var(--faint);margin-top:9px}
.tc-d dt:first-child{margin-top:0}
.tc-d dd{font-size:13px;margin-top:2px;white-space:pre-wrap}
.tc-f{margin-top:auto;font-size:11px;color:var(--faint)}
/* ── 事实卡片：每个来源一张 ── */
/* min(500px,100%) 而不是 500px —— 手机宽度不足 500px 时，写死的 500px 下限
   会直接撑出横向滚动条。 */
.facts-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(500px,100%),1fr));gap:12px}
.fc{background:var(--card);border:1px solid var(--line);border-radius:var(--card-r);
 padding:13px 15px;box-shadow:var(--shadow);display:flex;flex-direction:column;gap:11px}
.fc-h{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.fc-h .co{font-weight:600;font-size:14.5px;font-family:var(--serif)}
.fc-grid{display:grid;grid-template-columns:1fr 1fr;gap:7px 14px}
.fc-f{display:flex;gap:7px;font-size:12.5px;min-width:0}
.fc-k{color:var(--faint);font-size:11.5px;flex:none;padding-top:1px}
.fc-v{min-width:0;word-break:break-word}
.fc-v.hi{color:var(--accent);font-weight:600}
.fc-f2{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:auto;
 padding-top:10px;border-top:1px dashed var(--line)}
.fc-tags{display:flex;flex-wrap:wrap;gap:4px;min-width:0}
.fc-src{margin-left:auto;font-size:12px;white-space:nowrap}
.tag{display:inline-block;font-size:10.5px;color:var(--dim);background:var(--pill);
 border:1px solid var(--pill-line);border-radius:7px;padding:1px 6px}
/* 门槛对照在卡片里改为上下叠放：卡片宽度撑不起并排两列长文本，
   并排会把 recruiting_bar 那种 200+ 字的段落挤成一条竖线。
   颜色区分（蓝＝书面 / 琥珀＝真实）保留 —— 那才是对照的锚点。 */
.fc .bars{grid-template-columns:1fr}
.bars{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:900px){.bars{grid-template-columns:1fr}}
/* 两块门槛：书面＝浅赭底（纸面上的条件），真实＝藏红实底反白字。
   两者共一条金色左侧边 —— 把「这还是同一组对照」这个意思留住，
   同时实底那块一眼就是结论所在。 */
.bar{background:var(--bar-paper-bg);border:1px solid var(--bar-paper-line);
 border-left:3px solid var(--gold);border-radius:9px;padding:9px 11px}
.bar h4{font-size:11px;color:var(--bar-paper-ink);font-weight:600;margin-bottom:4px}
.bar.actual{background:var(--bar-real-bg);border-color:var(--bar-real-line);
 border-left-color:var(--accent)}
.bar.actual h4{color:var(--bar-real-ink)}
.bar p{font-size:12.5px;line-height:1.62;white-space:pre-wrap}
.exp{margin-top:9px}
.exp summary{cursor:pointer;font-size:11.5px;color:var(--accent);list-style:none}
.exp summary::-webkit-details-marker{display:none}
.exp summary::before{content:"▸ "}
.exp[open] summary::before{content:"▾ "}
.exp p{font-size:12.5px;line-height:1.62;margin-top:6px;white-space:pre-wrap;
 color:var(--dim)}
footer{margin-top:34px;padding-top:12px;border-top:1px solid var(--line);
 font-size:11.5px;color:var(--faint);display:flex;gap:14px;flex-wrap:wrap}
/* ── 手机端 ──
   原来完全没有断点：事实卡写死 500px 下限会撑出横向滚动，字号也按桌面压到 12.5px。
   这里按手机阅读习惯调：单列、正文放大到 15px、行高放宽、触控目标（tab / 链接）
   撑到至少 ~40px 高，左右留白收窄把宽度让给正文。 */
@media(max-width:640px){
 body{padding:14px 12px 48px;font-size:15px;line-height:1.7}
 .wrap{max-width:none}
 header{padding:11px 0 12px}
 header .logo{font-size:17px;margin-bottom:5px}
 h1{font-size:22px;gap:8px}
 h1 .cn{font-size:13px;letter-spacing:2px}
 .archive{gap:6px;margin:12px 0 2px}
 .archive a{font-size:12.5px;padding:6px 11px}
 h2{font-size:17px;margin:22px 0 10px}
 /* insight tab：手机上让它换行铺开，而不是横向滑动 —— 只有六个且都是短标签，
    换行能全部看见；滑动的写法会把一半 tab 藏在屏幕外，看起来像坏了。 */
 .tabbar{flex-wrap:wrap;gap:5px;border-bottom:none;padding-bottom:0}
 .tabbar label{font-size:13px;padding:9px 12px;margin-bottom:0;border-radius:8px;
  border-bottom:1px solid var(--pill-line)}
 .panel{padding:14px 15px;border-radius:10px}
 .finding{font-size:15px;line-height:1.75}
 .tc-name{font-size:15px}
 .tc-d dd,.tc-d dt{font-size:14px}
 .facts-grid{grid-template-columns:1fr;gap:10px}
 .fc{padding:13px 14px;border-radius:10px}
 .fc-h .co{font-size:15px}
 /* 岗位/城市/薪资/强度：手机上仍保持两列，但缩小标签列宽、放大取值 */
 .fc-grid{grid-template-columns:1fr 1fr;gap:8px 10px}
 .fc-f{font-size:14px}
 .fc-k{font-size:12px}
 .bar{padding:10px 12px}
 .bar h4{font-size:12px}
 .bar p,.exp p{font-size:14px;line-height:1.7}
 .exp summary,.tc-d summary{font-size:13px;padding:3px 0}
 .fc-f2{padding-top:11px}
 .fc-src{font-size:13px}
 .tag{font-size:11px;padding:2px 7px}
 footer{font-size:12px;gap:8px;flex-direction:column}
}
"""


# --------------------------------------------------------------- 片段
def topic_state(item):
    """full / empty / missing —— tab 的灰化和默认选中都看它。"""
    if not item:
        return "missing"
    f = flat(item.get("finding"))
    return "empty" if (not f or f in NO_SIGNAL) else "full"


def render_topic_body(topic, item):
    """一个 tab 面板的内容。六个主题都会有面板，没信号的也保留。"""
    name = TOPIC_LABELS.get(topic, topic)
    state = topic_state(item)
    head = '<div class="tc-h"><span class="tc-name">%s</span>%s</div>'

    if state == "missing":
        return (head % (esc(name), '<span class="badge none">未产出</span>')
                + '<p class="muted">本期没有这个主题的分析。</p>')

    finding = flat(item.get("finding"))
    if state == "empty":
        note = ("较上期无实质变化，未产生新增分析" if finding == "无增量变化"
                else "本期无相关信号")
        return (head % (esc(name), '<span class="badge none">%s</span>'
                        % esc(finding or "本期无信号"))
                + '<p class="muted">%s</p>' % esc(note))

    layer = item.get("finding_layer")
    lv = LAYER_LABELS.get(layer)
    badge = ('<span class="badge lv-%s">%s</span>' % (esc(layer), esc(lv))
             if lv else '<span class="badge none">未分层</span>')
    ev = item.get("evidence_count")
    ev_html = ('<span class="ev">%s 条支撑</span>' % esc(ev)
               if isinstance(ev, (int, float)) else "")

    rows = [("为什么重要", item.get("why_it_matters")),
            ("行业含义", item.get("industry_implication")),
            ("对你的含义", item.get("career_implication")),
            ("个例", item.get("related_cases")),
            ("完整分析", item.get("ai_analysis"))]
    dl = "".join("<dt>%s</dt><dd>%s</dd>" % (esc(k), esc(flat(v)))
                 for k, v in rows if flat(v))
    detail = ('<details class="tc-d"><summary>展开分析</summary><dl>%s</dl></details>'
              % dl if dl else "")
    conf = CONF_LABELS.get(flat(item.get("confidence")), "")
    return (head % (esc(name), badge + ev_html)
            + '<p class="finding">%s</p>' % esc(finding)
            + detail
            + ('<div class="tc-f">置信度 %s</div>' % esc(conf) if conf else ""))


def tab_css(topics):
    """tab 选中态的样式，按 TOPICS 生成 —— 别手写六份规则跟 schema 脱钩。

    纯 CSS tab 的前提：radio 必须在 .tabbar 和 .panels 之前、且是同级兄弟，
    靠 `:checked ~` 同时管住「哪个标签高亮」和「哪个面板显示」。

    手机上 tab 从「贴着面板的页签」变成「一排独立药丸」，所以选中态要给一块强调底色
    —— 桌面那套 background:var(--card) 在药丸形态下几乎看不出选中。
    """
    desk, mob = [], []
    for t in topics:
        desk.append(
            '#tab-%(t)s:checked~.tabbar label[for="tab-%(t)s"]{background:var(--card);'
            'color:var(--accent);font-weight:600;border-color:var(--accent-line)}'
            '#tab-%(t)s:checked~.panels #panel-%(t)s{display:block}' % {"t": t})
        mob.append(
            '#tab-%(t)s:checked~.tabbar label[for="tab-%(t)s"]'
            '{background:var(--accent-bg)}' % {"t": t})
    return "".join(desk) + "@media(max-width:640px){" + "".join(mob) + "}"


def render_fact(rec):
    """一条事实（= 一个来源）一张卡片。"""
    itype = flat(rec.get("institution_type"))
    company = flat(rec.get("company"))
    role = flat(rec.get("role"))
    dept = flat(rec.get("department"))

    def field(k, v, hi=False):
        return ('<div class="fc-f"><span class="fc-k">%s</span>'
                '<span class="fc-v%s">%s</span></div>'
                % (esc(k), " hi" if hi else "", or_dash(v)))

    # 头部：机构名 + 小标签（机构类型，词表见 schema.INSTITUTION_TYPES）
    head = ('<div class="fc-h">%s%s</div>'
            % ('<h3 class="co">%s</h3>'
               % (esc(company) if company else '<span class="na">%s</span>' % EMPTY),
               '<span class="badge">%s</span>' % esc(itype) if itype else ""))

    # 基础信息两列网格。薪资与强度走强调色 —— 扫卡片时这两个最先想看。
    grid = ('<div class="fc-grid">%s</div>'
            % (field("岗位", " · ".join(p for p in (role, dept) if p))
               + field("城市", flat(rec.get("city")))
               + field("薪资", flat(rec.get("salary")), hi=True)
               + field("强度", flat(rec.get("wlb")), hi=True)))

    # ⚠️ 书面门槛与真实门槛是这个项目的核心差异点。
    #    但绝不在这里算「落差 ↑↓」之类的判定列 —— 那等于 AI 脑补，
    #    违反项目「无原文依据一律 null」的硬规则。判读权留给人。
    bars = ('<div class="bars">'
            '<div class="bar"><h4>书面门槛 recruiting_bar</h4><p>%s</p></div>'
            '<div class="bar actual"><h4>真实门槛 actual_bar</h4><p>%s</p></div>'
            '</div>'
            % (or_dash(flat(rec.get("recruiting_bar"))),
               or_dash(flat(rec.get("actual_bar")))))

    exp = flat(rec.get("experience_summary"))
    exp_html = ('<details class="exp"><summary>经历摘要</summary><p>%s</p></details>'
                % esc(exp)) if exp else ""

    link = source_link(rec.get("source_url"))
    src = ('<a class="fc-src" href="%s" target="_blank" rel="noopener noreferrer">%s ↗</a>'
           % (esc(link[0]), esc(link[1])) if link else
           '<span class="fc-src na">%s</span>' % EMPTY)
    tags = "".join('<span class="tag">%s</span>' % esc(t) for t in tags_of(rec))

    foot = ('<div class="fc-f2"><div class="fc-tags">%s</div>%s</div>'
            % (tags or '<span class="na">%s</span>' % EMPTY, src))

    return ('<article class="fc">%s%s%s%s%s</article>'
            % (head, grid, bars, exp_html, foot))


# --------------------------------------------------------------- 页面
def render_page(date_str, issue, dates):
    insights = issue.get("insights") or []
    facts = issue.get("facts") or []

    by_topic = {}
    for it in insights:
        t = it.get("topic")
        if t and t not in by_topic:      # 一个主题可能多条，只取第一条
            by_topic[t] = it

    states = {t: topic_state(by_topic.get(t)) for t in TOPICS}
    # 默认选中有信号的第一条；全都没信号就退回第一个 —— 免得一打开是个空面板
    selected = next((t for t in TOPICS if states[t] == "full"), TOPICS[0])

    # ⚠️ radio 必须排在最前、且与 .tabbar / .panels 同级 —— tab_css 靠
    #    `:checked ~` 兄弟选择器工作，顺序变了整套 tab 就失效。
    radios = "".join('<input type="radio" name="ttab" id="tab-%s"%s>'
                     % (esc(t), " checked" if t == selected else "")
                     for t in TOPICS)
    tabbar = ('<div class="tabbar">%s</div>'
              % "".join('<label for="tab-%s"%s>%s</label>'
                        % (esc(t), "" if states[t] == "full" else ' class="empty"',
                           esc(TOPIC_LABELS.get(t, t)))
                        for t in TOPICS))
    panels = ('<div class="panels">%s</div>'
              % "".join('<section class="panel" id="panel-%s">%s</section>'
                        % (esc(t), render_topic_body(t, by_topic.get(t)))
                        for t in TOPICS))
    topics_html = '<div class="tabs">%s%s%s</div>' % (radios, tabbar, panels)

    facts_html = "".join(render_fact(r) for r in facts)

    nav = "".join(
        '<a href="%s.html"%s>%s</a>'
        % (esc(d), ' class="cur"' if d == date_str else "", esc(d))
        for d in reversed(dates))

    return """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FinClue 金线索 · %(date)s</title>
<style>%(css)s%(tabcss)s</style></head><body><div class="wrap">
<header><span class="logo">★</span><div class="hd">
<h1>FinClue <span class="cn">金线索</span></h1>
</div></header>
<nav class="archive">%(nav)s</nav>
<h2>本期洞察</h2>
%(topics)s
<h2>事实对照</h2>
<div class="facts-grid">%(facts)s</div>
<footer><span>FinClue 金线索 · 本地阅读端</span>
<span>数据源：小红书 · 渲染自 data/facts 与 data/pending</span>
<span>每 3 天 10:00 自动更新</span></footer>
</div></body></html>
""" % {"date": esc(date_str), "css": CSS, "tabcss": tab_css(TOPICS),
       "nav": nav, "topics": topics_html, "facts": facts_html}


def write_atomic(p, text):
    """临时文件 + os.replace。

    common.write_json 是原地写，崩在中途会留下浏览器照样渲染的残缺页面 ——
    阅读端是现在唯一的消费面，宁可写不成功也不要写一半。
    """
    ensure_dir(os.path.dirname(p))
    tmp = p + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, p)


# --------------------------------------------------------------- 主流程
def render_all():
    """全量重渲所有期 + index.html（最新一期）。返回 (期数, 事实数, 洞察数)。"""
    issues = load_issues()
    dates = sorted(issues)
    if not dates:
        LOG.warning("没有可渲染的数据（data/facts/ 为空）")
        return 0, 0, 0

    n_facts = n_ins = 0
    for d in dates:
        page = render_page(d, issues[d], dates)
        write_atomic(os.path.join(SITE_DIR, "%s.html" % d), page)
        n_facts += len(issues[d].get("facts") or [])
        n_ins += len(issues[d].get("insights") or [])
        LOG.info("渲染 %s.html（%d 事实 / %d 洞察）", d,
                 len(issues[d].get("facts") or []),
                 len(issues[d].get("insights") or []))

    # index.html 最后写：崩在前面也不会留下一个指向半成品的首页
    latest = dates[-1]
    write_atomic(os.path.join(SITE_DIR, "index.html"),
                 render_page(latest, issues[latest], dates))
    LOG.info("站点已更新：%s（最新一期 %s，共 %d 期）",
             os.path.join(SITE_DIR, "index.html"), latest, len(dates))
    return len(dates), n_facts, n_ins
