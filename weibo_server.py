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
import datetime
import email.utils
import html as html_mod
import json
import os
import random
import re
import sqlite3
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
      state TEXT DEFAULT 'idle',
      next_page INTEGER,              -- 全量拉取的断点页码（NULL=没有未完成的全量）
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
      deleted INTEGER DEFAULT 0);     -- 1=全量拉到底后微博上已不存在（ADR-0007）
    CREATE INDEX IF NOT EXISTS idx_posts_uid_ts ON posts(uid, created_ts DESC);
    CREATE INDEX IF NOT EXISTS idx_posts_ts ON posts(created_ts DESC);
    CREATE TABLE IF NOT EXISTS pull_seen (
      uid TEXT NOT NULL, id TEXT NOT NULL,
      PRIMARY KEY(uid, id));          -- 全量拉取中已见到的博文清单（跨断点续爬持久保留）
    ''')
    # 旧库迁移：补列（新库直接建表已含）
    for table, col, decl in (('bloggers', 'homepage', "TEXT DEFAULT ''"),
                             ('posts', 'deleted', 'INTEGER DEFAULT 0')):
        cols = [r[1] for r in CONN.execute('PRAGMA table_info(%s)' % table).fetchall()]
        if col not in cols:
            CONN.execute('ALTER TABLE %s ADD COLUMN %s %s' % (table, col, decl))
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
    if not full:
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
        return 'update'
    if existing:
        db('UPDATE posts SET reposts=?, comments=?, atts=?, counts_updated_at=?, deleted=0 WHERE id=?',
           (reposts, comments, atts, now_str(), pid))
        return 'update'
    db('INSERT INTO posts(id,uid,bid,text,created_ts,created_raw,reposts,comments,atts,'
       'media_json,retweeted_json,raw_json,fetched_at,counts_updated_at,deleted) '
       'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)',
       (pid, uid, mb.get('bid') or pid, text, parse_time(mb.get('created_at')),
        mb.get('created_at') or '', reposts, comments, atts, media, rt, raw, now_str(), now_str()))
    return 'insert'


def mark_cookie_expired():
    kv_set('cookie_status', 'expired' if kv_get('cookie') else 'none')


def run_sync(uid, mode):
    """一次同步任务主体：全量翻到底 / 增量翻到已有为止；任何退出路径都落好状态"""
    ev = STOP.setdefault(uid, threading.Event())
    ev.clear()
    total = db('SELECT COUNT(*) c FROM posts WHERE uid=?', (uid,)).fetchone()['c']
    if mode == 'full':
        row = db('SELECT next_page FROM bloggers WHERE uid=?', (uid,)).fetchone()
        page = (row['next_page'] if row and row['next_page'] else 1) or 1
        label = '全量拉取'
    else:
        page = 1
        label = '增量拉取'
    try:
        session = MSession()
    except CookieExpired:
        mark_cookie_expired()
        set_blogger(uid, state='paused', note='还没有填写登录信息，填写后会自动继续')
        return
    set_blogger(uid, state='fulling', note='%s准备中' % label)
    inserted = updated = 0
    full_mode = mode == 'full'
    if full_mode and page == 1:      # 全新全量：清空"已见"清单；断点续爬（page>1）不清，避免误判
        db('DELETE FROM pull_seen WHERE uid=?', (uid,))

    def revert_deleted():
        return                     # pull_seen 方案：拉取期间不打标记，中断无需回退

    def finish_paused():
        """用户点了暂停或取消：取消→done 保留数据丢弃断点；暂停→paused 保留断点"""
        if uid in CANCEL:
            CANCEL.discard(uid)
            set_blogger(uid, state='done', next_page=None,
                        note='已取消，保留已拉取数据', last_synced_at=now_str())
        else:
            set_blogger(uid, state='paused', note='%s已暂停，点击继续接着拉' % label,
                        next_page=mode == 'full' and page or None)
        revert_deleted()

    def save_progress(p, extra=''):
        set_blogger(uid, next_page=mode == 'full' and p or None,
                    note='%s · 第%d页 · 新增%d条' % (label, p - 1, inserted) + extra)

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

        if not mblogs:
            if full_mode:            # 拉到底：本次从未见到的博文确认已删除（pull_seen 清单）
                db('UPDATE posts SET deleted=1 WHERE uid=? AND id NOT IN '
                   '(SELECT id FROM pull_seen WHERE uid=?)', (uid, uid))
            ndel = db('SELECT COUNT(*) c FROM posts WHERE uid=? AND deleted=1',
                      (uid,)).fetchone()['c']
            note = '%s完成，翻到底了' % label
            if full_mode and ndel:
                note += '，%d条已标记为博主已删除' % ndel
            set_blogger(uid, state='done', next_page=None, note=note, last_synced_at=now_str())
            return

        hit_existing = False       # 增量停止条件：本页出现库中已有的非置顶微博
        for mb in mblogs:
            if ev.is_set():
                finish_paused()
                return
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


REFRESH = {'total': 0, 'done': 0}           # 批量更新的总数与已完成数（供前端显示进度）
DELETED_HINTS = ('删除', '不存在', '已删除')     # 接口提示「该微博不存在/已删除」的特征


def _is_deleted_error(e):
    s = str(e)
    return any(h in s for h in DELETED_HINTS)
REFRESH_QUEUE = deque()
RQ_COND = threading.Condition()


def run_refresh(ids):
    """批量更新：逐条抓取最新完整数据并覆盖写入（长文截断借此修复），逐条上报进度"""
    try:
        session = MSession()
    except CookieExpired:
        mark_cookie_expired()
        REFRESH['done'] = REFRESH['total']      # 标为全部完成，前端停止进度显示
        return
    for pid in ids:
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


def refresh_worker():
    """专职批量更新通道：进队即跑，不等待、不暂停正在进行的拉取"""
    while True:
        with RQ_COND:
            while not REFRESH_QUEUE:
                RQ_COND.wait()
            ids = REFRESH_QUEUE.popleft()
        try:
            run_refresh(ids)
        except Exception:                       # noqa: BLE001 —— 兜底，不能卡死更新通道
            REFRESH['done'] = REFRESH['total']


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
    rows = db('SELECT * FROM bloggers ORDER BY created_at').fetchall()
    out = []
    for r in rows:
        out.append({
            'uid': r['uid'], 'nickname': r['nickname'], 'avatar': r['avatar'], 'intro': r['intro'],
            'homepage': r['homepage'] or 'https://weibo.com/u/%s' % r['uid'],
            'state': r['state'], 'note': r['note'], 'next_page': r['next_page'],
            'last_synced_at': r['last_synced_at'], 'count': counts.get(r['uid'], 0),
            'earliest': earliest.get(r['uid']), 'latest': latest.get(r['uid']),
        })
    return out


def api_state():
    cookie_status = kv_get('cookie_status') or ('unknown' if kv_get('cookie') else 'none')
    busy = any(b['state'] in ('queued', 'fulling') for b in blogger_rows())
    return {'ok': True, 'cookie_status': cookie_status, 'cookie_set': bool(kv_get('cookie')),
            'bloggers': blogger_rows(), 'busy': busy,
            'refresh_total': REFRESH['total'], 'refresh_done': REFRESH['done']}


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
    db('INSERT INTO bloggers(uid,nickname,avatar,intro,homepage,state,note,created_at) '
       'VALUES(?,?,?,?,?,?,?,?)',
       (uid, profile['nickname'], profile['avatar'], profile['intro'],
        'https://weibo.com/u/%s' % uid, 'idle', '已添加，准备开始拉取全部历史', now_str()))
    enqueue(uid, 'full')
    return {'ok': True}


def api_sync(body):
    uid = str(body.get('uid') or '')
    row = db('SELECT next_page FROM bloggers WHERE uid=?', (uid,)).fetchone()
    if not row:
        return {'ok': False, 'error': '博主不存在'}
    mode = 'full' if row['next_page'] else 'incr'    # 有断点页码 → 续全量；否则增量
    enqueue(uid, mode)
    return {'ok': True}


def api_refull(body):
    """重拉全量：从头全量重拉并覆盖；不删数据，拉到底后标记微博已删的博文（ADR-0007）"""
    uid = str(body.get('uid') or '')
    row = db('SELECT state FROM bloggers WHERE uid=?', (uid,)).fetchone()
    if not row:
        return {'ok': False, 'error': '博主不存在'}
    if row['state'] in ('queued', 'fulling'):
        return {'ok': False, 'error': '正在拉取中，请稍候'}
    set_blogger(uid, next_page=1)              # 从头开始
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
                set_blogger(uid, state='done', next_page=None, note='已取消')
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
        REFRESH_QUEUE.append(ids)
        REFRESH['total'] = len(ids)      # 只统计本次操作：新批次从 0 开始
        REFRESH['done'] = 0
        RQ_COND.notify()
    return {'ok': True, 'queued': len(ids)}


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
    w = ('WHERE ' + ' AND '.join(where)) if where else ''
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
            elif path == '/api/pause':
                self._json(api_pause(body))
            elif path == '/api/blogger/delete':
                self._json(api_delete(body))
            elif path == '/api/batch/delete':
                self._json(api_batch_delete(body))
            elif path == '/api/batch/update':
                self._json(api_batch_update(body))
            elif path == '/api/refull':
                self._json(api_refull(body))
            elif path == '/api/cancel':
                self._json(api_cancel(body))
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
    for _ in range(2):                                  # 最多 2 路同时拉博主
        threading.Thread(target=sync_worker, daemon=True).start()
    threading.Thread(target=refresh_worker, daemon=True).start()   # 专职批量更新通道
    threading.Thread(target=cookie_watcher, daemon=True).start()

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
