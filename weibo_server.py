# -*- coding: utf-8 -*-
"""
微博存档工具（本地 Web 版）

功能：
  1. 添加任意公开微博博主（粘贴主页链接或 uid），拉取其全部历史微博
  2. 后台串行拉取（一次只跑一个博主），页码进度落库，重启后可断点续爬
  3. 增量拉取：从最新一页翻到遇到库里已有的微博为止，顺带刷新重叠微博的互动数
  4. 转发微博内嵌被转原文快照；长微博自动补拉全文；每条微博保留接口原始 JSON
  5. 前端查询：按博主 / 关键词 / 日期区间筛选，时间倒序分页，图片经本地代理展示
  6. 登录信息（浏览器里复制的整串 Cookie）在页面顶部粘贴维护，失效会亮灯提示

用法：
  python weibo_server.py [端口]     # 默认 8766，启动后自动打开浏览器

决策背景见 weibo/docs/adr/（SQLite / m 站接口+小号 Cookie / raw 留底）。
"""
import concurrent.futures
import datetime
import email.utils
import glob
import html as html_mod
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if getattr(sys, 'frozen', False):            # 打包为 exe 时
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))   # 数据/日志/备份写到 exe 同目录
    BUNDLE_DIR = getattr(sys, '_MEIPASS', APP_DIR)               # HTML 等资源在打包内
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
    BUNDLE_DIR = APP_DIR
BASE_DIR = APP_DIR
DB_PATH = os.environ.get('WEIBO_DB') or os.path.join(BASE_DIR, 'weibo.db')
HTML_PATH = os.path.join(BUNDLE_DIR, 'weibo_web.html')

TEMPLATE_PATH = os.path.join(BUNDLE_DIR, 'yuque-sync-template.md')
YUQUE_URL_RE = re.compile(r'^https://www\.yuque\.com/([^\s/?#]+)/([^\s/?#]+)((?:/[^\s/?#]+)*)/?$')
SYNC_WORKERS = 2                 # 批内并发路数：2 路并行跑，墙钟约减半
SYNC_TIMEOUT = 240               # 单条 claude 调用超时（秒）

SCHED_MIN_MINUTES = 30           # 定时拉取间隔边界：低于半小时没意义还容易触发反爬
SCHED_MAX_MINUTES = 1440         # 上限 24 小时，防止手滑填出天文数字

API_BASE = 'https://m.weibo.cn'
PAGE_COUNT = 50                 # 列表接口每页条数（上限 100）
PAGE_SLEEP = (6, 10)            # 页间随机间隔（对齐社区反风控实践）
DETAIL_SLEEP = (5, 8)           # 长文补拉前间隔
BACKOFFS = (30, 120, 600)       # 被限制（432）时的退避重试序列
UA = ('Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) '
      'AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148')

# 博主状态机：idle 未开始 / queued 排队中 / fulling 拉取中 / paused 已暂停 / done 已完成 / error 出错了
STATES = ('idle', 'queued', 'fulling', 'paused', 'done', 'error')


# --------------------------------------------------------------- 异常 ----
class CookieExpired(Exception):
    """登录信息未配置或已失效"""


class Blocked(Exception):
    """被反爬拦截（432）"""

    def __str__(self):
        return '微博暂时限制了访问频率'


class ApiError(Exception):
    """接口异常 / 网络异常"""


# ------------------------------------------------------------------ db ----
DB_LOCK = threading.RLock()
CONN = None


def init_db():
    global CONN
    CONN = sqlite3.connect(DB_PATH, check_same_thread=False)
    CONN.row_factory = sqlite3.Row
    CONN.execute('PRAGMA journal_mode=WAL')
    CONN.executescript('''
    CREATE TABLE IF NOT EXISTS kv (
      k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE IF NOT EXISTS bloggers (
      uid TEXT PRIMARY KEY,
      nickname TEXT DEFAULT '',
      avatar TEXT DEFAULT '',
      intro TEXT DEFAULT '',
      homepage TEXT DEFAULT '',       -- 博主主页 https://weibo.com/u/{uid}
      yuque_dir TEXT DEFAULT '',      -- 语雀归档目录链接（可为空，ADR-0008）
      state TEXT DEFAULT 'idle',
      next_page INTEGER,              -- 全量拉取的断点页码（NULL=没有未完成的全量）
      pull_from INTEGER DEFAULT 0,    -- 重拉全量的起始时间戳（0=从头拉到底；暂停续拉时保持）
      sort_order INTEGER DEFAULT 0,   -- 博主列表排序序号（1 起，可上下移动）
      note TEXT DEFAULT '',           -- 人话进度/错误说明，直接展示给用户
      last_synced_at TEXT,
      created_at TEXT);
    CREATE TABLE IF NOT EXISTS posts (
      id TEXT PRIMARY KEY,            -- 微博 id（字符串，防长数字丢精度）
      uid TEXT NOT NULL,
      bid TEXT DEFAULT '',
      text TEXT DEFAULT '',
      created_ts INTEGER,             -- 解析后的发布时间戳
      created_raw TEXT DEFAULT '',    -- 接口原始时间串
      reposts INTEGER DEFAULT 0,
      comments INTEGER DEFAULT 0,
      atts INTEGER DEFAULT 0,         -- 点赞数
      media_json TEXT DEFAULT '{}',   -- {"imgs": [...], "video": "..."}
      retweeted_json TEXT DEFAULT '', -- 被转原文快照，非转发微博为空
      raw_json TEXT,                  -- 接口原始 JSON 留底（ADR-0003）
      fetched_at TEXT,
      counts_updated_at TEXT,
      deleted INTEGER DEFAULT 0,      -- 1=全量拉到底后微博上已不存在（ADR-0007）
      archived INTEGER DEFAULT 0,     -- 1=已同步到语雀（ADR-0008）
      yuque_doc_url TEXT DEFAULT '',
      archived_at TEXT,
      arch_fail TEXT DEFAULT '',      -- 归档失败原因（同步失败/更新失败，空=无失败）
      arch_skip INTEGER DEFAULT 0,    -- 1=无需归档（用户手动标记）
      arch_state TEXT DEFAULT '');    -- 瞬态：syncing 同步中 / updating 更新中（空=无）
    CREATE INDEX IF NOT EXISTS idx_posts_uid_ts ON posts(uid, created_ts DESC);
    CREATE INDEX IF NOT EXISTS idx_posts_ts ON posts(created_ts DESC);
    CREATE TABLE IF NOT EXISTS pull_seen (
      uid TEXT NOT NULL, id TEXT NOT NULL,
      PRIMARY KEY(uid, id));          -- 全量拉取中已见到的博文清单（跨断点续爬持久保留）
    ''')
    # 旧库迁移：补列（新库直接建表已含）
    for table, col, decl in (('bloggers', 'homepage', "TEXT DEFAULT ''"),
                             ('bloggers', 'yuque_dir', "TEXT DEFAULT ''"),
                             ('bloggers', 'pull_from', 'INTEGER DEFAULT 0'),
                             ('bloggers', 'sort_order', 'INTEGER DEFAULT 0'),
                             ('posts', 'deleted', 'INTEGER DEFAULT 0'),
                             ('posts', 'archived', 'INTEGER DEFAULT 0'),
                             ('posts', 'yuque_doc_url', "TEXT DEFAULT ''"),
                             ('posts', 'archived_at', "TEXT DEFAULT ''"),
                             ('posts', 'arch_fail', "TEXT DEFAULT ''"),
                             ('posts', 'arch_skip', 'INTEGER DEFAULT 0'),
                             ('posts', 'arch_state', "TEXT DEFAULT ''")):
        cols = [r[1] for r in CONN.execute('PRAGMA table_info(%s)' % table).fetchall()]
        if col not in cols:
            CONN.execute('ALTER TABLE %s ADD COLUMN %s %s' % (table, col, decl))
    # 老库补博主排序序号（按添加时间），保证 sort_order 连续不重复
    if any(r['c'] for r in CONN.execute(
            'SELECT COUNT(*) c FROM bloggers WHERE sort_order=0').fetchall()):
        for k, r in enumerate(CONN.execute(
                'SELECT uid FROM bloggers ORDER BY created_at, rowid').fetchall(), 1):
            CONN.execute('UPDATE bloggers SET sort_order=? WHERE uid=?', (k, r[0]))
    CONN.commit()


def db(sql, params=()):
    with DB_LOCK:
        cur = CONN.execute(sql, params)
        CONN.commit()
        return cur


def kv_get(k, default=None):
    row = db('SELECT v FROM kv WHERE k=?', (k,)).fetchone()
    return row[0] if row else default


def kv_set(k, v):
    db('INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v', (k, v))


def now_str():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


# ------------------------------------------------------------ m 站会话 ----
class MSession:
    """带 Cookie 的 m.weibo.cn 请求会话（首次使用前先预热拿指纹 Cookie）"""

    def __init__(self):
        raw = kv_get('cookie') or ''
        self.cookies = {}
        raw = raw.strip().strip('﻿').strip().strip('"').strip()
        if raw.lower().startswith('cookie:'):
            raw = raw[len('cookie:'):].strip()      # 可能带着 "Cookie: " 前缀
        for part in raw.split(';'):
            part = part.strip()
            if not part:
                continue
            if '=' in part:
                k, v = part.split('=', 1)
                self.cookies[k.strip()] = v.strip()
            else:
                self.cookies['SUB'] = part          # 只贴了 SUB 的值
        if not self.cookies.get('SUB'):
            raise CookieExpired('登录信息未配置')
        try:
            self.request(API_BASE + '/')            # 预热，补 _T_WM 等指纹
        except Exception:
            pass

    def _absorb(self, headers):
        for sc in headers.get_all('Set-Cookie') or []:
            m = re.match(r'\s*([^=;]+)=([^;]*)', sc)
            if m and m.group(2).strip():
                self.cookies[m.group(1).strip()] = m.group(2).strip()

    def request(self, url, referer=None):
        headers = {
            'User-Agent': UA,
            'Accept': 'text/html,application/json,text/plain,*/*',
            'Accept-Language': 'zh-CN,zh;q=0.9',
            'X-Requested-With': 'XMLHttpRequest',
            'Referer': referer or API_BASE + '/',
        }
        if self.cookies:
            headers['Cookie'] = '; '.join('%s=%s' % kv for kv in self.cookies.items())
        req = urllib.request.Request(url, headers=headers)
        try:
            resp = urllib.request.urlopen(req, timeout=25)
        except urllib.error.HTTPError as e:
            self._absorb(e.headers)
            if e.code == 432:
                raise Blocked('432')
            raise ApiError('连接微博出了问题')
        except (urllib.error.URLError, OSError) as e:
            raise ApiError('网络异常：%s' % e)
        self._absorb(resp.headers)
        return resp.read()

    def json(self, url, referer=None):
        data = json.loads(self.request(url, referer).decode('utf-8', 'replace'))
        if data.get('ok') == -100:
            raise CookieExpired('登录已过期')
        if data.get('ok') != 1:
            msg = str(data.get('msg') or '接口返回异常')
            if '登录' in msg:
                raise CookieExpired(msg)
            raise ApiError(msg)
        return data


def validate_cookie(session):
    """轻量校验 cookie 是否有效：GET /api/config 的 login 字段。
    返回 True=有效 / False=失效 / None=接口没给出明确结论"""
    try:
        data = session.json(API_BASE + '/api/config')
    except CookieExpired:
        return False
    login = (data.get('data') or {}).get('login')
    if login is None:
        return None
    return bool(login)


# ------------------------------------------------------------ 字段解析 ----
def parse_uid(s):
    """从粘贴的内容里解析 uid：主页链接 / 纯数字皆可"""
    s = (s or '').strip()
    m = re.search(r'weibo\.c[no]/(?:u/|profile/)?(\d{6,})', s) or re.search(r'(\d{6,})', s)
    return m.group(1) if m else None


def parse_time(s):
    """m 站时间串 → 时间戳：刚刚/N秒前/N分钟前/N小时前/昨天 HH:MM/MM-DD/YYYY-MM-DD/标准格式"""
    now = datetime.datetime.now()
    s = (s or '').strip()
    if not s:
        return int(now.timestamp())
    try:
        if s == '刚刚':
            return int(now.timestamp())
        m = re.match(r'(\d+)秒前$', s)
        if m:
            return int((now - datetime.timedelta(seconds=int(m.group(1)))).timestamp())
        m = re.match(r'(\d+)分钟前$', s)
        if m:
            return int((now - datetime.timedelta(minutes=int(m.group(1)))).timestamp())
        m = re.match(r'(\d+)小时前$', s)
        if m:
            return int((now - datetime.timedelta(hours=int(m.group(1)))).timestamp())
        m = re.match(r'昨天\s*(\d{1,2}):(\d{2})$', s)
        if m:
            d = (now - datetime.timedelta(days=1)).replace(hour=int(m.group(1)), minute=int(m.group(2)))
            return int(d.timestamp())
        m = re.match(r'(\d{1,2})-(\d{1,2})$', s)
        if m:
            d = datetime.datetime(now.year, int(m.group(1)), int(m.group(2)))
            if d > now + datetime.timedelta(days=1):        # 跨年：12月看到"1-1"应是去年
                d = d.replace(year=now.year - 1)
            return int(d.timestamp())
        m = re.match(r'(\d{4})-(\d{1,2})-(\d{1,2})', s)
        if m:
            return int(datetime.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).timestamp())
        return int(email.utils.parsedate_to_datetime(s).timestamp())   # 'Wed Aug 19 ... +0800 2026'
    except Exception:
        return int(now.timestamp())


EMOJI_IMG_RE = re.compile(
    r'<img[^>]*alt="(\[[^\]]+\])"[^>]*src="([^"]+)"[^>]*>'
    r'|<img[^>]*src="([^"]+)"[^>]*alt="(\[[^\]]+\])"[^>]*>')
LINK_RE = re.compile(r'<a[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.S)


def _safe_href(href):
    """只放行 http/https 绝对链接，其余（相对路径/非法协议）返回空"""
    href = (href or '').strip()
    return href if href.startswith(('http://', 'https://')) else ''


def _clean_link_inner(inner):
    """链接内文本：剥离标签但保留表情占位符"""
    inner = re.sub(r'<br\s*/?>', '\n', inner)
    inner = re.sub(r'<[^>]+>', '', inner)
    inner = html_mod.unescape(inner)
    inner = re.sub(r'[ \t]+', ' ', inner)
    return inner.strip()


def clean_text(h):
    """m 站 HTML 正文 → 纯文本；保留微博表情 <img class="wemoji"> 与外部链接 <a class="wlink"> 供前端渲染"""
    if not h:
        return ''
    emojis = []
    links = []

    def repl_emoji(m):
        name = m.group(1) or m.group(4)
        src = m.group(2) or m.group(3)
        emojis.append((name, src))
        return '\x00E%d\x00' % (len(emojis) - 1)

    def repl_link(m):
        href = _safe_href(m.group(1))
        inner = _clean_link_inner(m.group(2))
        links.append((href, inner))
        return '\x00L%d\x00' % (len(links) - 1)

    s = EMOJI_IMG_RE.sub(repl_emoji, h)          # 先保护表情 img
    s = LINK_RE.sub(repl_link, s)                # 再保护外部链接（链接内可能含表情占位符）
    s = re.sub(r'<br\s*/?>', '\n', s)
    s = re.sub(r'<img[^>]*alt=["\']([^"\']*)["\'][^>]*>', lambda m: m.group(1), s)
    s = re.sub(r'</?[^>]+>', '', s)
    s = html_mod.unescape(s)
    s = re.sub(r'[ \t]+', ' ', s)
    s = re.sub(r'\n{3,}', '\n\n', s).strip()
    s = re.sub(r'(全文|收起|展开)\s*$', '', s).strip()
    if emojis:
        def restore_emoji(m):
            name, src = emojis[int(m.group(1))]
            return '<img class="wemoji" src="%s" alt="%s">' % (src, name)
        s = re.sub('\x00E(\\d+)\x00', restore_emoji, s)
    if links:
        def restore_link(m):
            href, inner = links[int(m.group(1))]
            if not href:
                return inner
            return '<a class="wlink" href="%s" target="_blank" rel="noopener">%s</a>' % (href, inner)
        s = re.sub('\x00L(\\d+)\x00', restore_link, s)
        s = re.sub(r'(全文)\s*$', '', s).strip()   # 相对路径的「全文」链接已退化为文本，末尾去掉
    return s


def extract_media(mb):
    pics = mb.get('pics') or []
    imgs = []
    for p in pics:
        u = (p.get('large') or {}).get('url') or p.get('url')
        if u:
            imgs.append(u)
    video = ''
    mi = (mb.get('page_info') or {}).get('media_info') or {}
    for k in ('mp4_720p_mp4', 'mp4_hd_url', 'mp4_sd_url', 'stream_url'):
        if mi.get(k):
            video = mi[k]
            break
    return {'imgs': imgs, 'video': video}


def extract_retweeted(mb):
    rt = mb.get('retweeted_status')
    if not rt:
        return ''
    user = rt.get('user') or {}
    imgs = []
    for p in rt.get('pics') or []:
        u = (p.get('large') or {}).get('url') or p.get('url')
        if u:
            imgs.append(u)
    snap = {
        'nickname': user.get('screen_name') or '原微博',
        'created_raw': rt.get('created_at') or '',
        'text': clean_text(rt.get('text') or ''),
        'imgs': imgs,
    }
    return json.dumps(snap, ensure_ascii=False)


def is_pinned(mb):
    if mb.get('isTop'):
        return True
    t = mb.get('title')
    return isinstance(t, dict) and t.get('text') == '置顶'


def fetch_post_detail(session, mid):
    """抓取单条微博的完整数据（全文/计数/媒体/原始 JSON）。
    走 m.weibo.cn/statuses/show?id={mid}，返回标准严格 JSON，data 即完整博文。
    无有效登录态时抛 ApiError。"""
    data = session.json('%s/statuses/show?id=%s' % (API_BASE, mid))
    mb = data.get('data') or {}
    if not isinstance(mb, dict) or not mb.get('id'):
        raise ApiError('微博数据不完整')
    return mb


def fetch_page(session, uid, page):
    url = ('%s/api/container/getIndex?type=uid&value=%s&containerid=230413%s&page=%d&count=%d'
           % (API_BASE, uid, uid, page, PAGE_COUNT))
    data = session.json(url, referer='%s/u/%s' % (API_BASE, uid))
    cards = (data.get('data') or {}).get('cards') or []
    return [c['mblog'] for c in cards if c.get('mblog')]


def fetch_profile(session, uid):
    url = '%s/api/container/getIndex?type=uid&value=%s' % (API_BASE, uid)
    data = session.json(url)
    info = (data.get('data') or {}).get('userInfo') or {}
    if not info:
        raise ApiError('没找到这个博主，可能编号有误')
    return {
        'uid': uid,
        'nickname': info.get('screen_name') or uid,
        'avatar': info.get('avatar_hd') or info.get('profile_image_url') or '',
        'intro': info.get('description') or '',
    }


# --------------------------------------------------------------- 任务 ----
TASKQ = deque()
Q_COND = threading.Condition()
STOP = {}            # uid -> Event，置位表示请求暂停/取消当前任务
CANCEL = set()        # 已请求「取消」的博主（区别于暂停：取消→保留数据、丢弃断点）


def set_blogger(uid, **fields):
    if not fields:
        return
    cols = ', '.join('%s=?' % k for k in fields)
    db('UPDATE bloggers SET %s WHERE uid=?' % cols, tuple(fields.values()) + (uid,))


def enqueue(uid, mode):
    """mode: 'full'（全量/续爬）或 'incr'（增量）；已在队列/拉取中则忽略"""
    row = db('SELECT state FROM bloggers WHERE uid=?', (uid,)).fetchone()
    if not row:
        raise ApiError('博主不存在')
    if row['state'] in ('queued', 'fulling'):
        return False
    with Q_COND:
        TASKQ.append((uid, mode))
        set_blogger(uid, state='queued', note='排队中，等待开始')
        Q_COND.notify()
    return True


def upsert_post(session, uid, mb, force=False, full=False):
    """写入/更新一条博文。force：已存在也整体覆盖（批量更新用）；full：mb 已是完整数据，跳过长文补拉"""
    pid = str(mb.get('id') or '')
    if not pid:
        return 'skip'
    existing = db('SELECT id FROM posts WHERE id=?', (pid,)).fetchone()
    text_html = mb.get('text') or ''
    if not full and not existing:
        # 只有新博文才补拉全文；已存在的博文只刷新计数，不重拉正文，避免每次增量都为整页长微博白等 5~8 秒
        is_long = bool(mb.get('isLongText')) or ('全文' in text_html and '</a>' in text_html)
        if is_long:
            time.sleep(random.uniform(*DETAIL_SLEEP))
            try:
                mb = fetch_post_detail(session, pid)   # 补拉全文（整条完整数据）
                text_html = mb.get('text') or ''
            except Exception:
                pass          # 补拉失败：保留列表页正文（可能截断）
    text = clean_text(text_html)
    reposts = mb.get('reposts_count') or 0
    comments = mb.get('comments_count') or 0
    atts = mb.get('attitudes_count') or 0
    media = json.dumps(extract_media(mb), ensure_ascii=False)
    rt = extract_retweeted(mb)
    raw = json.dumps(mb, ensure_ascii=False)
    if existing and force:
        db('UPDATE posts SET text=?, media_json=?, retweeted_json=?, raw_json=?, '
           'reposts=?, comments=?, atts=?, counts_updated_at=?, fetched_at=?, deleted=0 WHERE id=?',
           (text, media, rt, raw, reposts, comments, atts, now_str(), now_str(), pid))
        res = 'update'
    elif existing:
        db('UPDATE posts SET reposts=?, comments=?, atts=?, counts_updated_at=?, deleted=0 WHERE id=?',
           (reposts, comments, atts, now_str(), pid))
        res = 'update'
    else:
        db('INSERT INTO posts(id,uid,bid,text,created_ts,created_raw,reposts,comments,atts,'
           'media_json,retweeted_json,raw_json,fetched_at,counts_updated_at,deleted) '
           'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)',
           (pid, uid, mb.get('bid') or pid, text, parse_time(mb.get('created_at')),
            mb.get('created_at') or '', reposts, comments, atts, media, rt, raw, now_str(), now_str()))
        res = 'insert'
    if rt:                # 转发微博不支持归档 → 拉取时直接标「无需归档」
        db("UPDATE posts SET arch_skip=1, archived=0, arch_fail='', arch_state='', yuque_doc_url='' "
           "WHERE id=?", (pid,))
    return res


def mark_cookie_expired():
    kv_set('cookie_status', 'expired' if kv_get('cookie') else 'none')


def run_sync(uid, mode):
    """一次同步任务主体：全量翻到底 / 增量翻到已有为止；任何退出路径都落好状态"""
    ev = STOP.setdefault(uid, threading.Event())
    ev.clear()
    total = db('SELECT COUNT(*) c FROM posts WHERE uid=?', (uid,)).fetchone()['c']
    start_ts = 0
    if mode == 'full':
        row = db('SELECT next_page, pull_from FROM bloggers WHERE uid=?', (uid,)).fetchone()
        page = (row['next_page'] if row and row['next_page'] else 1) or 1
        start_ts = (row['pull_from'] if row and row['pull_from'] else 0) or 0
        label = '全量拉取'
    else:
        page = 1
        label = '增量拉取'
    start_label = ''
    if start_ts:
        start_label = datetime.date.fromtimestamp(start_ts).strftime('%Y-%m-%d')
    start_hint = ('（从 %s 起）' % start_label) if start_label else ''
    try:
        session = MSession()
    except CookieExpired:
        mark_cookie_expired()
        set_blogger(uid, state='paused', note='还没有填写登录信息，填写后会自动继续')
        return
    set_blogger(uid, state='fulling', note='%s%s准备中' % (label, start_hint))
    inserted = updated = 0
    full_mode = mode == 'full'
    range_done = False                # 全量范围停止：翻到比所选起始时间更早的博文（置顶除外）
    if full_mode and page == 1:      # 全新全量：清空"已见"清单；断点续爬（page>1）不清，避免误判
        db('DELETE FROM pull_seen WHERE uid=?', (uid,))

    def revert_deleted():
        return                     # pull_seen 方案：拉取期间不打标记，中断无需回退

    def finish_paused():
        """用户点了暂停或取消：取消→done 保留数据丢弃断点；暂停→paused 保留断点"""
        if uid in CANCEL:
            CANCEL.discard(uid)
            set_blogger(uid, state='done', next_page=None, pull_from=0,
                        note='已取消，保留已拉取数据', last_synced_at=now_str())
        else:
            set_blogger(uid, state='paused', note='%s%s已暂停，点击继续接着拉' % (label, start_hint),
                        next_page=mode == 'full' and page or None)
        revert_deleted()

    def save_progress(p, extra=''):
        set_blogger(uid, next_page=mode == 'full' and p or None,
                    note='%s%s · 第%d页 · 新增%d条' % (label, start_hint, p - 1, inserted) + extra)

    while True:
        if ev.is_set():
            finish_paused()
            return
        try:
            mblogs = fetch_page(session, uid, page)
            kv_set('cookie_status', 'ok')
        except CookieExpired:
            mark_cookie_expired()
            set_blogger(uid, state='paused', note='登录已过期，请在页面顶部重新粘贴后点击继续',
                        next_page=mode == 'full' and page or None)
            revert_deleted()
            return
        except Blocked:
            gave_up = True
            for wait in BACKOFFS:
                for _ in range(wait):
                    if ev.is_set():
                        break
                    time.sleep(1)
                if ev.is_set():
                    finish_paused()
                    return
                try:
                    mblogs = fetch_page(session, uid, page)
                    kv_set('cookie_status', 'ok')
                    gave_up = False
                    break
                except CookieExpired:
                    mark_cookie_expired()
                    set_blogger(uid, state='paused', note='登录已过期，请在页面顶部重新粘贴',
                                next_page=mode == 'full' and page or None)
                    revert_deleted()
                    return
                except Blocked:
                    continue
                except ApiError as e:
                    set_blogger(uid, state='error', note='拉取出错：%s' % e,
                                next_page=mode == 'full' and page or None)
                    revert_deleted()
                    return
            if gave_up:
                kv_set('cookie_status', 'limited')
                set_blogger(uid, state='paused',
                            note='微博暂时限制了访问频率，已自动暂停；过段时间点击继续即可',
                            next_page=mode == 'full' and page or None)
                revert_deleted()
                return
        except ApiError as e:
            set_blogger(uid, state='error', note='拉取出错：%s' % e,
                        next_page=mode == 'full' and page or None)
            revert_deleted()
            return

        if not mblogs or range_done:
            if full_mode:            # 拉完：本次范围内从未见到的博文确认已删除（pull_seen 清单）
                if start_ts:         # 范围外（更早）的博文保持不动，不参与删除判定
                    db('UPDATE posts SET deleted=1 WHERE uid=? AND created_ts>=? AND id NOT IN '
                       '(SELECT id FROM pull_seen WHERE uid=?)', (uid, start_ts, uid))
                else:
                    db('UPDATE posts SET deleted=1 WHERE uid=? AND id NOT IN '
                       '(SELECT id FROM pull_seen WHERE uid=?)', (uid, uid))
            ndel = db('SELECT COUNT(*) c FROM posts WHERE uid=? AND deleted=1',
                      (uid,)).fetchone()['c']
            if full_mode:
                note = '全量拉取完成' + (start_label
                       and '，已从 %s 拉取到今天；更早的微博保持不动' % start_label or '，翻到底了')
            else:
                note = '%s完成，翻到底了' % label
            if full_mode and ndel:
                note += '，%d条已标记为博主已删除' % ndel
            set_blogger(uid, state='done', next_page=None, pull_from=0,
                        note=note, last_synced_at=now_str())
            return

        hit_existing = False       # 增量停止条件：本页出现库中已有的非置顶微博
        for mb in mblogs:
            if ev.is_set():
                finish_paused()
                return
            if full_mode and start_ts and not is_pinned(mb):
                if parse_time(mb.get('created_at')) < start_ts:
                    range_done = True
                    break
            pid_mb = str(mb.get('id') or '')
            if pid_mb and full_mode:
                db('INSERT OR IGNORE INTO pull_seen(uid,id) VALUES(?,?)', (uid, pid_mb))
            r = upsert_post(session, uid, mb)
            if r == 'insert':
                inserted += 1
            elif r == 'update':
                updated += 1
            if r == 'update' and not is_pinned(mb):
                hit_existing = True
        total = db('SELECT COUNT(*) c FROM posts WHERE uid=?', (uid,)).fetchone()['c']

        if mode == 'incr' and hit_existing:
            set_blogger(uid, state='done', next_page=None,
                        note='已同步到最新，共%d条（本次新增%d条、刷新%d条）' % (total, inserted, updated),
                        last_synced_at=now_str())
            return

        page += 1
        save_progress(page, ' · 库里共%d条' % total)
        time.sleep(random.uniform(*PAGE_SLEEP))


COOKIE_CHECK_INTERVAL = 300        # 后台每几分钟复验一次 cookie


def cookie_watcher():
    """后台定期验证 cookie：真失效就把状态翻成过期，前端轮询会自动看到"""
    while True:
        time.sleep(COOKIE_CHECK_INTERVAL)
        try:
            if not kv_get('cookie'):
                continue
            if any(b['state'] in ('queued', 'fulling') for b in blogger_rows()):
                continue          # 正在拉取就交给拉取流程去更新，不抢请求
            ok = validate_cookie(MSession())
            if ok:
                kv_set('cookie_status', 'ok')
            elif ok is False:
                mark_cookie_expired()
        except CookieExpired:
            mark_cookie_expired()
        except Blocked:
            kv_set('cookie_status', 'limited')
        except Exception:
            pass                  # 网络抖动等：保持原状，下轮再试


def sched_cfg():
    """定时拉取配置：开关 + 间隔（分钟，越界自动夹回 30~1440）"""
    try:
        minutes = int(kv_get('sched_minutes') or '60')
    except ValueError:
        minutes = 60
    return kv_get('sched_on') == '1', min(max(minutes, SCHED_MIN_MINUTES), SCHED_MAX_MINUTES)


def enqueue_all():
    """一键全部拉取的入队规则：逐个博主入队（有未完成全量的续拉，否则增量），在拉/排队的跳过。
    返回 (实际入队的 uid 列表, 跳过数)。手动一键与定时触发共用。"""
    started, skipped = [], 0
    for r in db('SELECT uid, state, next_page FROM bloggers '
                'ORDER BY sort_order, created_at, rowid').fetchall():
        if r['state'] in ('queued', 'fulling'):
            skipped += 1
            continue
        enqueue(r['uid'], 'full' if r['next_page'] else 'incr')
        started.append(r['uid'])
    return started, skipped


def schedule_worker():
    """定时拉取：到点按一键全部拉取规则跑一轮；上次执行时间落库，
    工具重启后发现已超间隔会在首轮循环自然补跑一次（只补一次）"""
    while True:
        time.sleep(20)
        try:
            on, minutes = sched_cfg()
            if not on:
                continue
            if time.time() - float(kv_get('sched_last') or 0) < minutes * 60:
                continue
            kv_set('sched_last', str(int(time.time())))   # 先落时间再入队，防重复触发
            if kv_get('cookie'):                          # 没登录信息就不入队，等下个间隔
                enqueue_all()
        except Exception:
            pass                  # 兜底：调度线程不能死


REFRESH = {'total': 0, 'done': 0}           # 批量更新的总数与已完成数（供前端显示进度）
DELETED_HINTS = ('删除', '不存在', '已删除')     # 接口提示「该微博不存在/已删除」的特征


def _is_deleted_error(e):
    s = str(e)
    return any(h in s for h in DELETED_HINTS)
REFRESH_QUEUE = deque()
RQ_COND = threading.Condition()
REFRESH_CANCEL = threading.Event()   # 批量更新取消标记：set=用户点了取消


def run_refresh(ids):
    """批量更新：逐条抓取最新完整数据并覆盖写入（长文截断借此修复），逐条上报进度；支持中途取消"""
    try:
        session = MSession()
    except CookieExpired:
        mark_cookie_expired()
        REFRESH['done'] = REFRESH['total']      # 标为全部完成，前端停止进度显示
        return
    for pid in ids:
        if REFRESH_CANCEL.is_set():             # 用户取消 → 立即停
            break
        row = db('SELECT uid FROM posts WHERE id=?', (pid,)).fetchone()
        if not row:
            REFRESH['done'] += 1
            continue
        try:
            mb = fetch_post_detail(session, pid)
            upsert_post(session, row['uid'], mb, force=True, full=True)
            kv_set('cookie_status', 'ok')
        except CookieExpired:
            mark_cookie_expired()
            REFRESH['done'] += 1
            break
        except Blocked:
            kv_set('cookie_status', 'limited')
            REFRESH['done'] += 1
            break
        except Exception as e:
            if _is_deleted_error(e):             # 微博上已不存在的博文 → 标记【博主已删除】
                db('UPDATE posts SET deleted=1 WHERE id=?', (pid,))
        REFRESH['done'] += 1
        time.sleep(random.uniform(*DETAIL_SLEEP))
    if REFRESH_CANCEL.is_set():                 # 取消收尾：清标记、标为结束
        REFRESH_CANCEL.clear()
        REFRESH['done'] = REFRESH['total']


def refresh_worker():
    """专职批量更新通道：进队即跑，不等待、不暂停正在进行的拉取"""
    while True:
        with RQ_COND:
            while not REFRESH_QUEUE and not REFRESH_CANCEL.is_set():
                RQ_COND.wait()
            ids = REFRESH_QUEUE.popleft() if REFRESH_QUEUE else None
        if ids is None:                         # 取消时队列被清空 → 清标记继续待命
            REFRESH_CANCEL.clear()
            continue
        try:
            run_refresh(ids)
        except Exception:                       # noqa: BLE001 —— 兜底，不能卡死更新通道
            REFRESH['done'] = REFRESH['total']


# ----------------------------------------------------------- 语雀归档 ----
SYNC = {'total': 0, 'done': 0, 'msg': '',      # 归档进度与结果消息（供前端展示）
        'created': 0, 'updated': 0, 'failed': 0, 'reasons': []}   # 本轮同步会话累计（可多批入队）
SYNC_QUEUE = deque()
SYNC_COND = threading.Condition()
SYNC_CANCEL = threading.Event()    # 语雀同步取消标记：set=用户点了取消


def find_claude():
    """找本机可调用的 claude CLI：PATH → VSCode 扩展内置二进制 → 常见安装位置"""
    p = shutil.which('claude')
    if p:
        return p
    home = os.path.expanduser('~')
    cands = []
    for pat in (os.path.join(home, '.claude', 'local', 'bin', 'claude*'),
                os.path.join(home, '.vscode', 'extensions', 'anthropic.claude-code-*',
                             'resources', 'native-binary', 'claude*'),
                os.path.join(home, '.vscode-insiders', 'extensions', 'anthropic.claude-code-*',
                             'resources', 'native-binary', 'claude*'),
                os.path.join(os.environ.get('APPDATA', ''), 'npm', 'claude*'),
                os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs',
                             'claude-code', 'claude*')):
        cands += glob.glob(pat)
    for c in cands:
        if os.path.isfile(c):
            return c
    return None


def find_yuque_mcp():
    """检查全局 claude 配置里是否注册了 yuque MCP（headless claude 会自动加载）"""
    for path in (os.path.join(os.path.expanduser('~'), '.claude', 'settings.json'),
                 os.path.join(os.path.expanduser('~'), '.claude.json'),
                 os.path.join(BASE_DIR, '.mcp.json')):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
        except Exception:
            continue
        servers = cfg.get('mcpServers') or {}
        if any('yuque' in name.lower() for name in servers):
            return True
    return False


def validate_yuque_dir(url):
    """校验语雀目录链接格式，返回 (账号, 知识库slug, 目录路径) 或 None"""
    url = (url or '').strip()
    m = YUQUE_URL_RE.match(url)
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3).strip('/')


MCP_NOT_READY_KW = ('未就绪', '未连接', 'MCP 未', '没有加载', '不可用', '无法使用 yuque')


def _spawn_claude(prompt, attempts=2):
    """调用无头 claude CLI（AI 总结 + 语雀 MCP 建文档），返回 (ok, 输出文本)。
    yuque MCP 是 npx 冷启动、可能比模型慢，首次调用若提示 MCP 未就绪则重试一次。"""
    exe = find_claude()
    if not exe:
        return False, '本机没找到 claude，请先安装 Claude Code 或 npm i -g @anthropic-ai/claude-code'
    last = (False, 'claude 未返回结果')
    for i in range(attempts):
        try:
            proc = subprocess.Popen(
                [exe, '-p', '-', '--output-format', 'json',
                 '--allowedTools', 'mcp__yuque__*'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, cwd=BASE_DIR,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            out, _ = proc.communicate(prompt.encode('utf-8'), timeout=SYNC_TIMEOUT)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
            return False, '同步超时（claude 调用超过 %d 秒）' % SYNC_TIMEOUT
        except Exception as e:
            return False, '调用 claude 失败：%s' % e
        text = out.decode('utf-8', 'replace')
        res = None
        for line in text.splitlines():      # 解析最后的 type=result 事件，取 result 文本
            line = line.strip()
            if not line.startswith('{'):
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get('type') == 'result':
                res = str(ev.get('result') or '')
        if res is None:
            return False, 'claude 返回无法解析：%s' % text[:300]
        if i < attempts - 1 and any(kw in res for kw in MCP_NOT_READY_KW):
            last = (False, '语雀 MCP 未就绪，重试后仍失败：%s' % res[:150])
            continue
        return True, res
    return last


def _build_archive_prompt(row, template, update_doc_url=None):
    """组装单条微博的归档提示词：新建或按最新模板更新语雀文档 + 模板 + 微博内容"""
    acc, book, folder = validate_yuque_dir(row['yuque_dir'])
    media = json.loads(row['media_json'] or '{}')
    nimgs = len(media.get('imgs') or [])
    ts = row['created_ts']
    when = datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M') if ts else ''
    if update_doc_url:
        target = '已有语雀文档：%s（repo 为 %s/%s，doc slug 为链接最后一段）' % (update_doc_url, acc, book)
        steps = (
            '1. 先生成整篇文档的完整内容：按模板结构包含元信息块、AI 总结、关键要点、AI 分析。\n'
            '2. 调用 yuque_update_doc 更新该文档：body 参数必须填完整生成的 Markdown 内容，禁止传空 body、禁止只传标题、禁止清空原有内容。\n'
            '3. 若更新报错「文档不存在」，则调用 yuque_create_doc 在该知识库（repo_id 传「%s/%s」）新建文档并按模板生成。\n'
            '4. 不要重复调用 yuque_update_doc；不要调用 get_doc / get_toc / 任何其他 yuque 工具。\n'
            % (acc, book))
    else:
        if folder:
            target = '语雀知识库链接：%s（账号 %s，知识库 %s，目录节点 slug 是「%s」，新文档要挂到它下面）' % (
                row['yuque_dir'], acc, book, folder)
            steps = (
                '1. 调用 yuque_create_doc 创建文档：repo_id 传「%s/%s」，标题按模板，正文严格按模板生成。\n'
                '2. 调用 yuque_get_toc 查看目录树：找到 slug 等于「%s」的节点（目录或普通文档都行），'
                '记下它的 uuid；再找到刚创建文档的节点（slug 是文档链接最后一段），记下它的 uuid。\n'
                '3. 调用 yuque_update_toc 把新文档挂到目标节点下，toc_data 传：'
                '{"action":"appendChild","target_uuid":"<目标节点uuid>","node_uuid":"<新文档uuid>"}。\n'
                '4. 如果第 2 步找不到目标节点或新文档节点，跳过第 3 步，不影响归档。\n'
                '5. 创建成功后，最终输出一行：SYNC_OK|文档URL。\n'
                '6. 如果创建文档失败（知识库不存在、没权限、限流等），最终输出一行：SYNC_ERR|具体错误原因，'
                '不要重试，不要调用其他 yuque 工具。\n'
                % (acc, book, folder))
        else:
            target = '语雀知识库链接：%s（账号 %s，知识库 %s，直接在该知识库下创建文档）' % (
                row['yuque_dir'], acc, book)
            steps = (
                '1. 调用 yuque_create_doc 创建文档：repo_id 传「%s/%s」，标题按模板，正文严格按模板生成。\n'
                '2. 创建成功后，最终输出一行：SYNC_OK|文档URL。\n'
                '3. 如果创建失败（知识库不存在、没权限、限流等），最终输出一行：SYNC_ERR|具体错误原因，'
                '不要重试，不要调用其他 yuque 工具。\n'
                % (acc, book))
    return (
        '你是一个「微博 → 语雀归档」助手。把下面这条微博按模板格式总结并同步到语雀。\n\n'
        '【目标】%s\n'
        '步骤：\n'
        '0. 如果 yuque MCP 的工具还没就绪，先调用 WaitForMcpServers 等待所有 MCP 服务器连接完成，然后再用。\n'
        '%s'
        '最后：文档内容严格按模板生成，微博ID 必须保留；如果任何 yuque MCP 调用返回限流（Too Many Requests / 429 / rate limited / 请求过多），立即停止重试，直接输出 SYNC_ERR|语雀接口限流，请稍后再试；最终输出必须只有一行：SYNC_OK|文档URL 或 SYNC_ERR|原因，禁止输出任何其他文字或解释。\n\n'
        '【模板】\n%s\n\n'
        '【微博信息】\n'
        '微博ID：%s\n博主：%s\n发布时间：%s\n原文链接：https://m.weibo.cn/detail/%s\n'
        '互动：转发 %s · 评论 %s · 赞 %s\n图片：%s 张\n\n'
        '【微博正文】\n%s'
        % (target, steps, template, row['id'], row['nickname'], when, row['bid'],
           row['reposts'], row['comments'], row['atts'], nimgs, row['text']))


def _extract_sync_result(out):
    """从 claude 结果里提取结果：优先 SYNC_OK|url / SYNC_ERR|原因，散文输出做关键词容错"""
    m = re.search(r'SYNC_OK\|\s*(\S+)', out)
    if m:
        return m.group(1).strip()
    if 'SYNC_ERR|' in out:
        m = re.search(r'SYNC_ERR\|\s*([^\n]*)', out)
        raise ApiError(m.group(1).strip() if m and m.group(1).strip() else '归档被拒绝')
    if '目录不存在' in out:
        raise ApiError('目录不存在')
    m = re.search(r'https://www\.yuque\.com/\S+', out)
    if m:
        return m.group(0).strip(').，,；;')
    raise ApiError('claude 未返回文档链接：%s' % out[:150])


def run_archive(ids):
    """批量归档：读模板 → 并发调 claude（AI 总结 + 建语雀文档，默认 2 路）→ 写回归档状态"""
    try:
        with open(TEMPLATE_PATH, 'r', encoding='utf-8') as f:
            template = f.read()
    except Exception as e:
        SYNC['msg'] = '读取同步模板失败：%s' % e
        SYNC['done'] = SYNC['total']
        return
    done_lock = threading.Lock()

    def archive_one(pid):
        try:
            row = db('SELECT p.*, b.nickname, b.yuque_dir FROM posts p '
                     'LEFT JOIN bloggers b ON b.uid=p.uid WHERE p.id=?', (pid,)).fetchone()
            if not row:
                raise ApiError('微博不存在')
            if not row['yuque_dir']:
                raise ApiError('博主未配置语雀同步目录')
            is_update = bool(row['archived'])
            prompt = _build_archive_prompt(row, template,
                                           update_doc_url=row['yuque_doc_url'] if is_update else None)
            ok, out = _spawn_claude(prompt)
            if not ok:
                raise ApiError(out)
            url = _extract_sync_result(out)
            db('UPDATE posts SET archived=1, yuque_doc_url=?, archived_at=?, '
               "arch_fail='', arch_state='' WHERE id=?", (url, now_str(), pid))
            return (pid, 'ok', is_update)
        except Exception as e:  # noqa: BLE001
            reason = str(e)
            db("UPDATE posts SET arch_fail=?, arch_state='' WHERE id=?", (reason, pid))
            return (pid, 'err', reason)
        finally:
            with done_lock:                  # 多路并发，进度计数要加锁
                SYNC['done'] += 1

    created = updated = 0
    reasons = []
    pending = list(ids)
    cancelled = False
    with concurrent.futures.ThreadPoolExecutor(max_workers=SYNC_WORKERS) as ex:
        futs = []
        while pending and not SYNC_CANCEL.is_set():
            while pending and len(futs) < SYNC_WORKERS:
                futs.append(ex.submit(archive_one, pending.pop(0)))
            done_set, _ = concurrent.futures.wait(
                futs, return_when=concurrent.futures.FIRST_COMPLETED)
            for f in done_set:
                futs.remove(f)
                pid, status, info = f.result()
                if status == 'ok':
                    if info:
                        updated += 1
                    else:
                        created += 1
                else:
                    reasons.append('%s：%s' % (pid, info))
        cancelled = SYNC_CANCEL.is_set()
        for f in futs:                        # 取消/收尾：把已提交的结果统计进去
            pid, status, info = f.result()
            if status == 'ok':
                if info:
                    updated += 1
                else:
                    created += 1
            else:
                reasons.append('%s：%s' % (pid, info))
    with done_lock:                          # 累计到本轮同步会话，供整队跑完后汇总
        SYNC['created'] += created
        SYNC['updated'] += updated
        SYNC['failed'] += len(reasons)
        if reasons:
            SYNC['reasons'] = (SYNC['reasons'] + reasons)[:5]
    if cancelled:                            # 用户取消：清标记、清掉没跑到的瞬态、收尾
        SYNC_CANCEL.clear()
        db("UPDATE posts SET arch_state='' WHERE arch_state IN ('syncing','updating')")
        SYNC['msg'] = '已取消：已归档 %d 条，其余未同步' % (created + updated)
        SYNC['done'] = SYNC['total']
        return
    if SYNC['done'] >= SYNC['total']:        # 等待队列全部跑完（可能含后续入队的批次）才给最终汇总
        if SYNC['failed']:
            SYNC['msg'] = '同步完成：新增 %d 条、更新 %d 条，失败 %d 条（%s）' % (
                SYNC['created'], SYNC['updated'], SYNC['failed'], '；'.join(SYNC['reasons']))
        else:
            SYNC['msg'] = '同步完成：新增 %d 条、更新 %d 条' % (SYNC['created'], SYNC['updated'])


def archive_worker():
    """归档 worker：一次处理一个批次（不限条数，批内 SYNC_WORKERS 路并发）"""
    while True:
        with SYNC_COND:
            while not SYNC_QUEUE and not SYNC_CANCEL.is_set():
                SYNC_COND.wait()
            ids = SYNC_QUEUE.popleft() if SYNC_QUEUE else None
        if ids is None:                     # 取消时队列被清空 → 清标记继续待命
            SYNC_CANCEL.clear()
            continue
        try:
            run_archive(ids)
        except Exception as e:  # noqa: BLE001 —— 兜底，不能卡死归档通道
            SYNC['msg'] = '同步出错：%s' % e
            SYNC['done'] = SYNC['total']


def sync_worker():
    """拉取 worker：一次处理一个博主（多个 worker 并发，最多同时 2 路）"""
    while True:
        with Q_COND:
            while not TASKQ:
                Q_COND.wait()
            uid, mode = TASKQ.popleft()
        try:
            run_sync(uid, mode)
        except Exception as e:  # noqa: BLE001 —— 兜底：任何异常都要落到人话状态，不能卡死队列
            set_blogger(uid, state='error', note='拉取出错：%s' % e)


# ---------------------------------------------------------------- API ----
def blogger_rows():
    counts = {r['uid']: r['c'] for r in db('SELECT uid, COUNT(*) c FROM posts GROUP BY uid').fetchall()}
    earliest = {r['uid']: r['t'] for r in db('SELECT uid, MIN(created_ts) t FROM posts GROUP BY uid').fetchall()}
    latest = {r['uid']: r['t'] for r in db('SELECT uid, MAX(created_ts) t FROM posts GROUP BY uid').fetchall()}
    rows = db('SELECT * FROM bloggers ORDER BY sort_order, created_at, rowid').fetchall()
    out = []
    for r in rows:
        out.append({
            'uid': r['uid'], 'nickname': r['nickname'], 'avatar': r['avatar'], 'intro': r['intro'],
            'homepage': r['homepage'] or 'https://weibo.com/u/%s' % r['uid'],
            'yuque_dir': r['yuque_dir'],
            'state': r['state'], 'note': r['note'], 'next_page': r['next_page'],
            'last_synced_at': r['last_synced_at'], 'count': counts.get(r['uid'], 0),
            'earliest': earliest.get(r['uid']), 'latest': latest.get(r['uid']),
        })
    return out


def api_state():
    cookie_status = kv_get('cookie_status') or ('unknown' if kv_get('cookie') else 'none')
    busy = any(b['state'] in ('queued', 'fulling') for b in blogger_rows())
    on, minutes = sched_cfg()
    return {'ok': True, 'cookie_status': cookie_status, 'cookie_set': bool(kv_get('cookie')),
            'bloggers': blogger_rows(), 'busy': busy,
            'schedule': {'on': on, 'minutes': minutes},
            'refresh_total': REFRESH['total'], 'refresh_done': REFRESH['done'],
            'yuque_total': SYNC['total'], 'yuque_done': SYNC['done'], 'yuque_msg': SYNC['msg']}


COOKIE_EXPIRED_MSG = '已保存，但这份登录信息无效或已过期：请用浏览器登录 m.weibo.cn 小号，把请求头里 Cookie: 后面那一整串复制过来'


def api_cookie(body):
    value = (body.get('value') or '').strip()
    if not value:
        return {'ok': False, 'error': '粘贴的内容是空的'}
    kv_set('cookie', value)
    kv_set('cookie_status', 'unknown')
    try:
        session = MSession()
    except CookieExpired:
        mark_cookie_expired()
        return {'ok': True, 'cookie_status': 'expired', 'message': COOKIE_EXPIRED_MSG}
    try:
        ok = validate_cookie(session)
    except Blocked:
        kv_set('cookie_status', 'limited')
        return {'ok': True, 'cookie_status': 'limited',
                'message': '已保存，但微博暂时限制了访问频率，稍后会自动重试验证'}
    except Exception:
        kv_set('cookie_status', 'unknown')
        return {'ok': True, 'cookie_status': 'unknown',
                'message': '已保存，暂时无法联网验证，会自动重试'}
    if ok:
        kv_set('cookie_status', 'ok')
        return {'ok': True, 'cookie_status': 'ok', 'message': '已保存，登录信息有效'}
    if ok is None:
        kv_set('cookie_status', 'unknown')
        return {'ok': True, 'cookie_status': 'unknown', 'message': '已保存，验证结果待确认'}
    mark_cookie_expired()
    return {'ok': True, 'cookie_status': 'expired', 'message': COOKIE_EXPIRED_MSG}


def api_preview(body):
    uid = parse_uid(body.get('input'))
    if not uid:
        return {'ok': False, 'error': '没认出这是哪位博主，请粘贴主页链接（形如 weibo.com/u/一串数字）'}
    exists = db('SELECT uid FROM bloggers WHERE uid=?', (uid,)).fetchone()
    try:
        profile = fetch_profile(MSession(), uid)
    except CookieExpired:
        mark_cookie_expired()
        return {'ok': False, 'error': '登录信息未填写或已过期，请先在页面顶部粘贴', 'need_cookie': True}
    except Blocked:
        return {'ok': False, 'error': '微博暂时限制了访问频率，请稍后再试'}
    except ApiError as e:
        return {'ok': False, 'error': str(e)}
    kv_set('cookie_status', 'ok')            # 能取到博主资料 = 登录信息有效
    profile['exists'] = bool(exists)
    return {'ok': True, 'profile': profile}


def api_add(body):
    uid = parse_uid(body.get('input'))
    if not uid:
        return {'ok': False, 'error': '没认出这是哪位博主，请粘贴主页链接（形如 weibo.com/u/一串数字）'}
    if db('SELECT uid FROM bloggers WHERE uid=?', (uid,)).fetchone():
        return {'ok': False, 'error': '这位博主已经在列表里了'}
    start = (body.get('start') or '').strip()
    start_ts = 0
    if start:
        try:
            start_ts = int(datetime.datetime.strptime(start, '%Y-%m-%d').timestamp())
        except ValueError:
            return {'ok': False, 'error': '起始日期格式不对，示例：2024-01-01'}
        if start_ts > time.time():
            return {'ok': False, 'error': '起始日期不能是未来'}
    try:
        profile = fetch_profile(MSession(), uid)
    except CookieExpired:
        mark_cookie_expired()
        return {'ok': False, 'error': '登录信息未填写或已过期，请先在页面顶部粘贴', 'need_cookie': True}
    except Blocked:
        return {'ok': False, 'error': '微博暂时限制了访问频率，请稍后再试'}
    except ApiError as e:
        return {'ok': False, 'error': str(e)}
    kv_set('cookie_status', 'ok')
    sort_order = db('SELECT COALESCE(MAX(sort_order),0)+1 s FROM bloggers').fetchone()['s']
    db('INSERT INTO bloggers(uid,nickname,avatar,intro,homepage,state,note,created_at,sort_order,pull_from) '
       'VALUES(?,?,?,?,?,?,?,?,?,?)',
       (uid, profile['nickname'], profile['avatar'], profile['intro'],
        'https://weibo.com/u/%s' % uid, 'idle', '已添加，准备开始拉取', now_str(), sort_order, start_ts))
    enqueue(uid, 'full')
    return {'ok': True}


def api_blogger_move(body):
    """上下移动博主在列表中的顺序"""
    uid = str(body.get('uid') or '')
    d = body.get('dir')
    if d not in ('up', 'down'):
        return {'ok': False, 'error': '移动方向不对'}
    ids = [r['uid'] for r in db(
        'SELECT uid FROM bloggers ORDER BY sort_order, created_at, rowid').fetchall()]
    if uid not in ids:
        return {'ok': False, 'error': '博主不存在'}
    i = ids.index(uid)
    j = i - 1 if d == 'up' else i + 1
    if j < 0 or j >= len(ids):
        return {'ok': True}                          # 已在最前/最后，无需移动
    ids[i], ids[j] = ids[j], ids[i]
    for k, u in enumerate(ids):                      # 重排序号，保证连续不重复
        db('UPDATE bloggers SET sort_order=? WHERE uid=?', (k + 1, u))
    return {'ok': True}


def api_sync(body):
    uid = str(body.get('uid') or '')
    row = db('SELECT next_page FROM bloggers WHERE uid=?', (uid,)).fetchone()
    if not row:
        return {'ok': False, 'error': '博主不存在'}
    mode = 'full' if row['next_page'] else 'incr'    # 有断点页码 → 续全量；否则增量
    enqueue(uid, mode)
    return {'ok': True}


def api_sync_all(body):
    """一键为全部博主拉取新微博：入队规则见 enqueue_all；返回本次实际启动的博主清单，供前端跟踪批量进度"""
    if not db('SELECT uid FROM bloggers LIMIT 1').fetchone():
        return {'ok': False, 'error': '还没有添加博主，先添加一位再拉取'}
    if not kv_get('cookie'):
        return {'ok': False, 'error': '还没有填写登录信息，请先在页面顶部粘贴'}
    started, skipped = enqueue_all()
    if not started:
        return {'ok': False, 'error': '所有博主都在拉取中，稍后再点'}
    msg = '已开始为 %d 位博主拉取新微博' % len(started)
    if skipped:
        msg += '，%d 位正在拉取已跳过' % skipped
    return {'ok': True, 'total': len(started), 'started': started, 'skipped': skipped, 'message': msg}


def api_schedule(body):
    """定时拉取设置：开关 + 间隔（分钟）；开启即从当前时刻起算一个新周期"""
    if not body.get('on'):
        kv_set('sched_on', '0')
        return {'ok': True}
    try:
        minutes = int(body.get('minutes'))
    except (TypeError, ValueError):
        return {'ok': False, 'error': '请填写拉取间隔（分钟）'}
    if minutes < SCHED_MIN_MINUTES or minutes > SCHED_MAX_MINUTES:
        return {'ok': False, 'error': '间隔需在 %d 分钟到 %d 分钟（24 小时）之间'
                % (SCHED_MIN_MINUTES, SCHED_MAX_MINUTES)}
    kv_set('sched_minutes', str(minutes))
    kv_set('sched_on', '1')
    kv_set('sched_last', str(int(time.time())))
    return {'ok': True}


def api_refull(body):
    """重拉全量：从头（或所选起始日期起）全量重拉并覆盖；不删数据，范围内微博已删的标记（ADR-0007）"""
    uid = str(body.get('uid') or '')
    row = db('SELECT state FROM bloggers WHERE uid=?', (uid,)).fetchone()
    if not row:
        return {'ok': False, 'error': '博主不存在'}
    if row['state'] in ('queued', 'fulling'):
        return {'ok': False, 'error': '正在拉取中，请稍候'}
    start = (body.get('start') or '').strip()
    start_ts = 0
    if start:
        try:
            start_ts = int(datetime.datetime.strptime(start, '%Y-%m-%d').timestamp())
        except ValueError:
            return {'ok': False, 'error': '起始日期格式不对，示例：2024-01-01'}
        if start_ts > time.time():
            return {'ok': False, 'error': '起始日期不能是未来'}
    set_blogger(uid, next_page=1, pull_from=start_ts)   # 从头（或所选日期）开始
    enqueue(uid, 'full')
    return {'ok': True}


def api_pause(body):
    uid = str(body.get('uid') or '')
    with Q_COND:                                     # 还在排队：直接出队
        for i, (u, m) in enumerate(TASKQ):
            if u == uid:
                del TASKQ[i]
                set_blogger(uid, state='paused', note='已暂停，点击继续接着拉',
                            next_page=m == 'full' and 1 or None)
                break
        else:
            STOP.setdefault(uid, threading.Event()).set()
    return {'ok': True}


def api_cancel(body):
    """取消本次拉取：保留已拉取数据，丢弃断点（之后为增量/重拉全量，不再续拉）"""
    uid = str(body.get('uid') or '')
    with Q_COND:                                     # 还在排队：直接出队并标记已取消
        for i, (u, _) in enumerate(TASKQ):
            if u == uid:
                del TASKQ[i]
                set_blogger(uid, state='done', next_page=None, pull_from=0, note='已取消')
                break
        else:
            CANCEL.add(uid)
            STOP.setdefault(uid, threading.Event()).set()
    return {'ok': True}


def api_delete(body):
    uid = str(body.get('uid') or '')
    row = db('SELECT state FROM bloggers WHERE uid=?', (uid,)).fetchone()
    if not row:
        return {'ok': False, 'error': '博主不存在'}
    if row['state'] in ('queued', 'fulling'):
        return {'ok': False, 'error': '正在拉取中，请先暂停再删除'}
    db('DELETE FROM bloggers WHERE uid=?', (uid,))
    db('DELETE FROM posts WHERE uid=?', (uid,))
    with Q_COND:
        for i, (u, _) in enumerate(TASKQ):
            if u == uid:
                del TASKQ[i]
    return {'ok': True}


def _clean_ids(body):
    return [str(i).strip() for i in (body.get('ids') or []) if str(i).strip()]


def api_batch_delete(body):
    """批量删除：只删勾选的博文，不碰其他博主的数据"""
    ids = _clean_ids(body)
    if not ids:
        return {'ok': False, 'error': '没有选中要删除的微博'}
    ph = ','.join('?' * len(ids))
    n = db('DELETE FROM posts WHERE id IN (%s)' % ph, tuple(ids)).rowcount
    return {'ok': True, 'deleted': n}


def api_batch_update(body):
    """批量更新：勾选博文整条重拉覆盖（正文/长文全文/计数/媒体），进独立更新通道即跑"""
    ids = _clean_ids(body)
    if not ids:
        return {'ok': False, 'error': '没有选中要更新的微博'}
    if REFRESH['total'] > REFRESH['done']:
        return {'ok': False, 'error': '上一批更新还在进行，请稍候'}
    with RQ_COND:
        REFRESH_CANCEL.clear()          # 新一轮更新：清除残留的取消标记
        REFRESH_QUEUE.append(ids)
        REFRESH['total'] = len(ids)      # 只统计本次操作：新批次从 0 开始
        REFRESH['done'] = 0
        RQ_COND.notify()
    return {'ok': True, 'queued': len(ids)}


def api_update_cancel(body=None):
    """取消正在进行的批量更新：停新任务、清排队、进度收尾"""
    with RQ_COND:
        REFRESH_CANCEL.set()
        REFRESH_QUEUE.clear()
        RQ_COND.notify_all()
    return {'ok': True}


def api_yuque_cancel(body=None):
    """取消正在进行的语雀同步：清排队、停新任务、清掉没跑到的「同步中」瞬态"""
    with SYNC_COND:
        SYNC_CANCEL.set()
        SYNC_QUEUE.clear()
        SYNC_COND.notify_all()
    db("UPDATE posts SET arch_state='' WHERE arch_state IN ('syncing','updating')")
    return {'ok': True}


def api_blogger_yuque_dir(body):
    """设置博主语雀归档目录链接（可留空）；格式不符合给出提示"""
    uid = str(body.get('uid') or '')
    url = str(body.get('dir') or '').strip()
    if not db('SELECT uid FROM bloggers WHERE uid=?', (uid,)).fetchone():
        return {'ok': False, 'error': '博主不存在'}
    if url and not validate_yuque_dir(url):
        return {'ok': False, 'error': '语雀目录链接格式不对，示例：https://www.yuque.com/账号/知识库/目录'}
    db('UPDATE bloggers SET yuque_dir=? WHERE uid=?', (url, uid))
    return {'ok': True}


def api_yuque_sync(body):
    """归档勾选的微博到语雀：不限条数；单个与批量互不拦截，已在同步中的自动跳过、其余进等待队列、顶部计数累加"""
    ids = _clean_ids(body)
    if not ids:
        return {'ok': False, 'error': '没有选中要同步的微博'}
    rows = db('SELECT p.id, p.retweeted_json, p.archived, p.arch_skip, b.nickname, b.yuque_dir '
              'FROM posts p LEFT JOIN bloggers b ON b.uid=p.uid '
              'WHERE p.id IN (%s)' % ','.join('?' * len(ids)), tuple(ids)).fetchall()
    wanted, no_dir = [], set()
    for r in rows:
        if r['retweeted_json'] or r['arch_skip']:
            continue
        if not r['yuque_dir']:
            no_dir.add(r['nickname'] or r['uid'])
            continue
        wanted.append(r['id'])
    if no_dir:
        return {'ok': False, 'error': '博主「%s」还没配置语雀同步目录，请先在左侧博主行设置' % '、'.join(sorted(no_dir))}
    if not wanted:
        return {'ok': False, 'error': '勾选的微博都是转发微博或无需归档，不支持同步'}
    if not find_claude():
        return {'ok': False, 'error': '本机没找到 claude，请先安装 Claude Code 或 npm i -g @anthropic-ai/claude-code'}
    if not find_yuque_mcp():
        return {'ok': False, 'error': '未配置语雀 MCP，请先用 claude mcp add 注册 yuque 服务'}
    with SYNC_COND:                          # 入队原子化：跳过已在同步/等待中的微博，其余追加到等待队列
        busy = {r['id'] for r in db(
            'SELECT id FROM posts WHERE id IN (%s) AND arch_state IN (?,?)'
            % ','.join('?' * len(wanted)),
            tuple(wanted) + ('syncing', 'updating')).fetchall()}
        todo = [pid for pid in wanted if pid not in busy]
        if not todo:
            return {'ok': False, 'error': '选中的微博都在同步中，稍等完成后再试'}
        todo_set = set(todo)
        todo_archived = {r['id'] for r in rows if r['id'] in todo_set and r['archived']}
        if SYNC['total'] == SYNC['done']:    # 空闲 → 新一轮同步会话：计数清零，会话内再累加排队
            SYNC['total'] = 0
            SYNC['done'] = 0
            SYNC['created'] = SYNC['updated'] = SYNC['failed'] = 0
            SYNC['reasons'] = []
        SYNC_CANCEL.clear()                  # 新一轮同步：清除残留的取消标记
        SYNC_QUEUE.append(todo)
        SYNC['total'] += len(todo)           # 顶部计数累加：已完成数不动
        SYNC['msg'] = ''
        SYNC_COND.notify()
        for pid in todo:                     # 瞬态落库：刷新页面仍显示同步中/更新中
            db('UPDATE posts SET arch_state=? WHERE id=?',
               ('updating' if pid in todo_archived else 'syncing', pid))
    create_n = sum(1 for pid in todo if pid not in todo_archived)
    update_n = len(todo_archived)
    return {'ok': True, 'queued': len(todo), 'created': create_n, 'updated': update_n,
            'skipped_busy': len(busy)}


def api_yuque_mark(body):
    """批量改归档状态：to='skip' 改为无需归档 / to='pending' 改回待归档"""
    ids = _clean_ids(body)
    to = str(body.get('to') or '')
    if not ids:
        return {'ok': False, 'error': '没有选中微博'}
    if to not in ('skip', 'pending'):
        return {'ok': False, 'error': '参数不对'}
    ph = ','.join('?' * len(ids))
    if to == 'skip':
        cur = db("UPDATE posts SET arch_skip=1, archived=0, arch_fail='', arch_state='', yuque_doc_url='' "
                 'WHERE id IN (%s)' % ph, tuple(ids))
    else:
        cur = db("UPDATE posts SET arch_skip=0, archived=0, arch_fail='', arch_state='', yuque_doc_url='' "
                 'WHERE id IN (%s)' % ph, tuple(ids))
    return {'ok': True, 'updated': cur.rowcount}


YUQUE_API = 'https://www.yuque.com/api/v2'


def load_yuque_token():
    """运行时读取语雀个人令牌：复用归档 MCP 配置的 YUQUE_PERSONAL_TOKEN（不入代码、不入库）"""
    home = os.path.expanduser('~')
    for path in (os.path.join(home, '.claude', 'settings.json'),
                 os.path.join(home, '.claude.json'),
                 os.path.join(BASE_DIR, '.mcp.json')):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
        except Exception:
            continue
        tok = ((cfg.get('env') or {}).get('YUQUE_PERSONAL_TOKEN') or '').strip()
        if tok:
            return tok
        for srv in (cfg.get('mcpServers') or {}).values():
            tok = (((srv or {}).get('env') or {}).get('YUQUE_PERSONAL_TOKEN') or '').strip()
            if tok:
                return tok
    return ''


def _yuque_api(path, token, method='GET'):
    """语雀 OpenAPI 调用，返回 (data, status)；404 原样返回供上层判断「已不存在」"""
    req = urllib.request.Request(YUQUE_API + path, method=method,
                                 headers={'X-Auth-Token': token,
                                          'User-Agent': 'weibo-archive',
                                          'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode('utf-8', 'replace')), resp.status
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, 404
        raise ApiError('语雀接口错误（%d）' % e.code)
    except (urllib.error.URLError, OSError) as e:
        raise ApiError('连接语雀失败：%s' % e)


def parse_yuque_doc_url(url):
    """语雀文档链接 → (账号/知识库, 文档slug)；认不出返回 None"""
    m = re.match(r'^https://www\.yuque\.com/([^/?#\s]+)/([^/?#\s]+)/([^/?#\s]+)', (url or '').strip())
    return ('%s/%s' % (m.group(1), m.group(2)), m.group(3)) if m else None


def yuque_delete_doc(url, token):
    """删除语雀文档：链接解析出知识库与 slug → 列表里查出数字 id → DELETE。
    文档或知识库已不存在（404 / 查不到）视为删除成功"""
    parsed = parse_yuque_doc_url(url)
    if not parsed:
        raise ApiError('语雀文档链接格式不对：%s' % url)
    namespace, slug = parsed
    doc_id = None
    for offset in range(0, 2000, 100):
        # ponytail: 线性翻页查 id（上限 2000 篇）；单库超过这个量再换语雀搜索接口
        data, st = _yuque_api('/repos/%s/docs?limit=100&offset=%d' % (namespace, offset), token)
        if st == 404:
            return                                  # 知识库都没了 → 文档自然不在了
        docs = (data or {}).get('data') or []
        for d in docs:
            if d.get('slug') == slug:
                doc_id = d.get('id')
                break
        if doc_id or len(docs) < 100:
            break
    if not doc_id:
        return                                      # 文档已不存在 → 视为删除成功
    _, st = _yuque_api('/repos/%s/docs/%s' % (namespace, doc_id), token, method='DELETE')
    if st == 404:
        return


def api_yuque_delete(body):
    """归档删除：逐条删语雀文档，成功后该微博重置为「待归档」；远端失败原样保留可重试。
    串行执行，中途失败继续删其余，最后汇总成功/失败数"""
    ids = _clean_ids(body)
    if not ids:
        return {'ok': False, 'error': '没有选中要删除的微博'}
    token = load_yuque_token()
    if not token:
        return {'ok': False, 'error': '未找到语雀令牌：请在 ~/.claude/settings.json 配置 YUQUE_PERSONAL_TOKEN'}
    rows = db('SELECT id, yuque_doc_url, arch_state FROM posts WHERE id IN (%s)'
              % ','.join('?' * len(ids)), tuple(ids)).fetchall()
    todo = [r for r in rows if r['yuque_doc_url'] and r['arch_state'] not in ('syncing', 'updating')]
    if not todo:
        return {'ok': False, 'error': '选中的微博没有可删除的语雀文档（未归档或正在同步中）'}
    ok = failed = 0
    first_err = ''
    for r in todo:
        try:
            yuque_delete_doc(r['yuque_doc_url'], token)
            db("UPDATE posts SET archived=0, arch_skip=0, arch_fail='', arch_state='', "
               "yuque_doc_url='', archived_at='' WHERE id=?", (r['id'],))
            ok += 1
        except Exception as e:  # noqa: BLE001 —— 单条失败不阻断其余
            failed += 1
            if not first_err:
                first_err = str(e)
    res = {'ok': True, 'deleted': ok, 'failed': failed}
    if failed:
        res['error_sample'] = first_err
    return res


def api_posts(query):
    try:
        page = max(1, int(query.get('page', ['1'])[0]))
    except ValueError:
        page = 1
    try:
        size = min(24, max(1, int(query.get('page_size', ['9'])[0])))
    except ValueError:
        size = 9
    where, params = [], []
    uid = query.get('uid', [''])[0]
    if uid:
        where.append('p.uid=?')
        params.append(uid)
    kw = query.get('kw', [''])[0].strip()
    if kw:
        where.append("p.text LIKE ? ESCAPE '\\'")
        params.append('%' + kw.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%')
    d_from = query.get('from', [''])[0].strip()
    d_to = query.get('to', [''])[0].strip()
    if d_from and re.fullmatch(r'\d{4}-\d{2}-\d{2}', d_from):
        where.append('p.created_ts>=?')
        params.append(int(datetime.datetime.strptime(d_from, '%Y-%m-%d').timestamp()))
    if d_to and re.fullmatch(r'\d{4}-\d{2}-\d{2}', d_to):
        where.append('p.created_ts<?')
        params.append(int((datetime.datetime.strptime(d_to, '%Y-%m-%d')
                           + datetime.timedelta(days=1)).timestamp()))
    year = query.get('year', [''])[0].strip()
    if re.fullmatch(r'\d{4}', year):
        y = int(year)
        where.append('p.created_ts>=? AND p.created_ts<?')
        params += [int(datetime.datetime(y, 1, 1).timestamp()),
                   int(datetime.datetime(y + 1, 1, 1).timestamp())]
    st = query.get('deleted', [''])[0].strip()
    if st == '1':
        where.append('p.deleted=1')
    elif st == '0':
        where.append('p.deleted=0')
    arch = query.get('arch', [''])[0].strip()
    if arch == 'pending':
        where.append('p.archived=0 AND p.arch_skip=0')
    elif arch == 'done':
        where.append("p.archived=1 AND p.arch_fail=''")
    elif arch == 'sync_fail':
        where.append("p.archived=0 AND p.arch_fail<>''")
    elif arch == 'update_fail':
        where.append("p.archived=1 AND p.arch_fail<>''")
    elif arch == 'skip':
        where.append('p.arch_skip=1')
    w = ('WHERE ' + ' AND '.join(where)) if where else ''
    wid = query.get('id', [''])[0].strip()
    if wid:                              # 微博ID直达：忽略其他筛选，精确匹配状态id或bid
        w = 'WHERE (p.id=? OR lower(p.bid)=lower(?))'
        params = [wid, wid]
    total = db('SELECT COUNT(*) c FROM posts p %s' % w, tuple(params)).fetchone()['c']
    rows = db('SELECT p.*, b.nickname, b.avatar FROM posts p '
              'LEFT JOIN bloggers b ON b.uid=p.uid %s '
              'ORDER BY p.created_ts DESC, p.id DESC LIMIT ? OFFSET ?' % w,
              tuple(params) + (size, (page - 1) * size)).fetchall()
    items = []
    for r in rows:
        items.append({
            'id': r['id'], 'uid': r['uid'], 'bid': r['bid'], 'text': r['text'],
            'created_ts': r['created_ts'], 'reposts': r['reposts'], 'comments': r['comments'],
            'atts': r['atts'], 'nickname': r['nickname'] or '', 'avatar': r['avatar'] or '',
            'media': json.loads(r['media_json'] or '{}'),
            'retweeted': json.loads(r['retweeted_json']) if r['retweeted_json'] else None,
            'deleted': r['deleted'],
            'archived': r['archived'], 'yuque_doc_url': r['yuque_doc_url'],
            'arch_fail': r['arch_fail'], 'arch_skip': r['arch_skip'], 'arch_state': r['arch_state'],
        })
    years = [r[0] for r in db("SELECT DISTINCT strftime('%Y', created_ts, 'unixepoch') y "
                              'FROM posts ORDER BY y DESC').fetchall()]
    if query.get('all_ids', [''])[0] == '1':          # 全选当前筛选结果用：返回全部匹配 id
        ids = [r[0] for r in db('SELECT id FROM posts p %s '
                                'ORDER BY p.created_ts DESC, p.id DESC' % w,
                                tuple(params)).fetchall()]
        return {'ok': True, 'ids': ids, 'total': total, 'years': years}
    return {'ok': True, 'items': items, 'total': total, 'page': page,
            'pages': max(1, -(-total // size)), 'years': years}


def proxy_image(url):
    host = (urllib.parse.urlparse(url).hostname or '')
    if not (host == 'sinaimg.cn' or host.endswith('.sinaimg.cn')):
        raise ApiError('只代理微博图片')
    req = urllib.request.Request(url, headers={
        'User-Agent': UA, 'Referer': 'https://weibo.com/', 'Accept': 'image/*,*/*'})
    try:
        resp = urllib.request.urlopen(req, timeout=20)
        data = resp.read(20 * 1024 * 1024)
        return data, resp.headers.get('Content-Type') or 'image/jpeg'
    except Exception as e:  # noqa: BLE001
        raise ApiError('图片取不到：%s' % e)


# -------------------------------------------------------------- HTTP ----
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):               # 安静模式：不再每请求刷屏
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode('utf-8'),
                   'application/json; charset=utf-8')

    def do_GET(self):
        path, _, qs = self.path.partition('?')
        try:
            if path == '/':
                with open(HTML_PATH, 'rb') as f:
                    self._send(200, f.read(), 'text/html; charset=utf-8')
            elif path == '/api/state':
                self._json(api_state())
            elif path == '/api/posts':
                self._json(api_posts(urllib.parse.parse_qs(qs)))
            elif path == '/img':
                data, ctype = proxy_image(urllib.parse.parse_qs(qs).get('u', [''])[0])
                self.send_response(200)
                self.send_header('Content-Type', ctype)
                self.send_header('Cache-Control', 'public, max-age=604800')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self._json({'ok': False, 'error': 'not found'}, 404)
        except Exception as e:  # noqa: BLE001
            self._json({'ok': False, 'error': str(e)}, 500)

    def do_POST(self):
        path = self.path.split('?')[0]
        try:
            n = int(self.headers.get('Content-Length') or 0)
            body = json.loads(self.rfile.read(n).decode('utf-8')) if n else {}
        except Exception:
            self._json({'ok': False, 'error': '请求体解析失败'}, 400)
            return
        try:
            if path == '/api/cookie':
                self._json(api_cookie(body))
            elif path == '/api/blogger/preview':
                self._json(api_preview(body))
            elif path == '/api/blogger/add':
                self._json(api_add(body))
            elif path == '/api/sync':
                self._json(api_sync(body))
            elif path == '/api/sync_all':
                self._json(api_sync_all(body))
            elif path == '/api/schedule':
                self._json(api_schedule(body))
            elif path == '/api/pause':
                self._json(api_pause(body))
            elif path == '/api/blogger/delete':
                self._json(api_delete(body))
            elif path == '/api/blogger/move':
                self._json(api_blogger_move(body))
            elif path == '/api/batch/delete':
                self._json(api_batch_delete(body))
            elif path == '/api/batch/update':
                self._json(api_batch_update(body))
            elif path == '/api/refull':
                self._json(api_refull(body))
            elif path == '/api/cancel':
                self._json(api_cancel(body))
            elif path == '/api/blogger/yuque_dir':
                self._json(api_blogger_yuque_dir(body))
            elif path == '/api/yuque/sync':
                self._json(api_yuque_sync(body))
            elif path == '/api/yuque/mark':
                self._json(api_yuque_mark(body))
            elif path == '/api/yuque/delete':
                self._json(api_yuque_delete(body))
            elif path == '/api/yuque/cancel':
                self._json(api_yuque_cancel(body))
            elif path == '/api/update/cancel':
                self._json(api_update_cancel(body))
            else:
                self._json({'ok': False, 'error': 'not found'}, 404)
        except Exception as e:  # noqa: BLE001
            self._json({'ok': False, 'error': str(e)}, 500)


def backup_db():
    """启动时把现有数据库连同 WAL 备份一份，防意外丢失无法回滚；保留最近 3 份"""
    try:
        if not os.path.exists(DB_PATH) or os.path.getsize(DB_PATH) == 0:
            return
        stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        dest = os.path.join(BASE_DIR, 'weibo-backup-%s.db' % stamp)
        with open(DB_PATH, 'rb') as src, open(dest, 'wb') as out:
            out.write(src.read())
        wal = DB_PATH + '-wal'
        if os.path.exists(wal) and os.path.getsize(wal) > 0:
            with open(wal, 'rb') as src, open(dest + '-wal', 'wb') as out:
                out.write(src.read())
        baks = sorted(f for f in os.listdir(BASE_DIR)
                      if re.match(r'weibo-backup-\d{8}-\d{6}\.db$', f))
        for f in baks[:-3]:
            for p in (os.path.join(BASE_DIR, f), os.path.join(BASE_DIR, f + '-wal')):
                if os.path.exists(p):
                    os.remove(p)
    except Exception:
        pass


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    backup_db()                     # 先备份现有数据，再初始化
    init_db()
    # 工具重启：把上次没跑完的任务标记为「已暂停」，用户点继续即断点续爬
    for r in db("SELECT uid, state FROM bloggers WHERE state IN ('queued','fulling')").fetchall():
        set_blogger(r['uid'], state='paused', note='工具重启过，点击继续接着拉')
        db('UPDATE posts SET deleted=0 WHERE uid=?', (r['uid'],))    # 中断的拉取不留下临时「已删除」标记
        db('DELETE FROM pull_seen WHERE uid=?', (r['uid'],))
    db("UPDATE posts SET arch_state='' WHERE arch_state IN ('syncing','updating')")   # 中断的归档不留假「同步中」
    for _ in range(2):                                  # 最多 2 路同时拉博主
        threading.Thread(target=sync_worker, daemon=True).start()
    threading.Thread(target=refresh_worker, daemon=True).start()   # 专职批量更新通道
    threading.Thread(target=archive_worker, daemon=True).start()   # 专职语雀归档通道
    threading.Thread(target=cookie_watcher, daemon=True).start()
    threading.Thread(target=schedule_worker, daemon=True).start()  # 定时拉取调度

    # pythonw 后台运行时没有控制台（stdout 为 None），print 全部丢失：日志转写到文件
    if sys.stdout is None:
        try:
            log = open(os.path.join(BASE_DIR, 'weibo_server.log'), 'a', buffering=1, encoding='utf-8')
            sys.stdout = sys.stderr = log
        except Exception:
            pass
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8766
    try:
        srv = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    except OSError:
        msg = ('端口 %d 已被占用，微博存档工具可能已在后台运行。\n\n'
               '浏览器直接打开 http://127.0.0.1:%d/ 即可使用。' % (port, port))
        print(msg)
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, msg, '微博存档工具', 0x40)
        except Exception:
            pass
        if not os.environ.get('WEIBO_NO_BROWSER'):
            webbrowser.open(url)          # 已运行则直接打开页面
        return
    url = 'http://127.0.0.1:%d/' % port
    print('=' * 56)
    print('  微博存档工具已启动: %s' % url)
    print('  Ctrl+C 停止服务；数据保存在 weibo.db')
    print('=' * 56)
    if not os.environ.get('WEIBO_NO_BROWSER'):
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        os._exit(0)


if __name__ == '__main__':
    main()
