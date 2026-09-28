# -*- coding: utf-8 -*-
"""进程内冒烟测试：灌测试数据 → 直接调 API 函数验证查询/筛选/删除/转义。
多用户版（ADR-0010）：API 以 user 字典为首参，数据挂在测试账号名下，全程离线打桩。
全程使用临时目录的独立数据库，绝不触碰真实 weibo.db（教训：2026-08-20 曾误删用户数据）。"""
import os
import sqlite3
import sys
import tempfile
import time

os.environ['WEIBO_NO_BROWSER'] = '1'
os.environ['WEIBO_DB'] = os.path.join(tempfile.gettempdir(), 'weibo_smoke_test.db')

import weibo_server as ws

DB = ws.DB_PATH
for f in (DB, DB + '-wal', DB + '-shm'):
    if os.path.exists(f):
        os.remove(f)
ws.init_db()

# 测试账号（can_archive=1，供语雀归档用例）；API 首参 user 用真实用户行
salt = ws.secrets.token_hex(16)
cur = ws.db('INSERT INTO users(username,pass_hash,pass_salt,role,can_archive,created_at) '
            "VALUES('tester','x',?,'user',1,?)", (salt, ws.now_str()))
USER_ID = cur.lastrowid
USER = ws.db('SELECT * FROM users WHERE id=?', (USER_ID,)).fetchone()

# 全程离线：网络/CLI 相关调用打桩，结果可复现
ws.MSession = lambda user_id: None
ws.validate_cookie = lambda session: True
ws.fetch_profile = lambda session, uid: {'uid': uid, 'nickname': '新博主' + uid, 'avatar': '', 'intro': ''}
ws.ukv_set(USER_ID, 'yuque_token', 'test-token')
# AI 归档旧全局三键（ADR-0012）当种子：首次读取应自动迁移成配置池（ADR-0013）
ws.kv_set('ai_base_url', 'https://ai.example.com')
ws.kv_set('ai_key', 'sk-abcdefg1234567')
ws.kv_set('ai_model', 'test-model')

t = int(time.time())
c = sqlite3.connect(DB)
c.execute("INSERT INTO posts(user_id,id,uid,bid,text,created_ts,reposts,comments,atts,media_json,retweeted_json,raw_json,fetched_at) "
          "VALUES(?,?,'1234567890','a1','第一条：介绍微博存档工具',?,12,3,50,'{\"imgs\":[\"https://wx4.sinaimg.cn/large/x.jpg\"]}','', '{}','2026-08-19 00:00:00')", (USER_ID, 'a1', t - 86400))
c.execute("INSERT INTO posts(user_id,id,uid,bid,text,created_ts,reposts,comments,atts,media_json,retweeted_json,raw_json,fetched_at) "
          "VALUES(?,?,'1234567890','a2','转发测试',?,1,0,2,'{}','{\"nickname\":\"原博主\",\"text\":\"被转的原文内容\",\"imgs\":[]}','{}','2026-08-19 00:00:00')", (USER_ID, 'a2', t - 3600))
c.execute("INSERT INTO posts(user_id,id,uid,bid,text,created_ts,reposts,comments,atts,media_json,retweeted_json,raw_json,fetched_at) "
          "VALUES(?,?,'999','a3','另一位的微博',?,0,0,0,'{}','','{}','2026-08-19 00:00:00')", (USER_ID, 'a3', t - 100))
c.commit()
c.close()

ok = True
def check(name, cond):
    global ok
    print(('PASS ' if cond else 'FAIL ') + name)
    if not cond:
        ok = False

def q(params):
    return ws.api_posts(USER, dict((k, [v]) for k, v in params.items()))

# 1. 全部，倒序
r = q({'page': '1'})
check('全部3条', r['total'] == 3)
check('倒序 a3在前', r['items'][0]['id'] == 'a3')

# 2. 按博主
r = q({'uid': '1234567890'})
check('按博主2条', r['total'] == 2)

# 3. 关键词
r = q({'kw': '工具'})
check('关键词命中1条', r['total'] == 1 and r['items'][0]['id'] == 'a1')

# 4. LIKE 转义：% 不应全命中
r = q({'kw': '%'})
check('%% 转义不命中', r['total'] == 0)
r = q({'kw': '_'})
check('_ 转义不命中', r['total'] == 0)

# 5. 日期区间（近1天内，博主1 → a2 一条）
from_d = time.strftime('%Y-%m-%d', time.localtime(t - 3600))
to_d = '2030-01-01'
r = q({'uid': '1234567890', 'from': from_d, 'to': to_d})
check('日期区间1条', r['total'] == 1 and r['items'][0]['id'] == 'a2')

# 6. 分页 + page_size
r = q({'page': '2', 'page_size': '2'})
check('page_size 生效', r['total'] == 3 and r['pages'] == 2 and len(r['items']) == 1)

# 6b. 年份筛选 + years 列表
cur_year = time.strftime('%Y', time.localtime(t))
r = q({'year': cur_year})
check('年份筛选当前年3条', r['total'] == 3)
r = q({'year': '1999'})
check('年份筛选无数据', r['total'] == 0)
check('years 列表含当前年', cur_year in (r['years'] or []))

# 7. 字段完整性（转发快照 / 图片 / 计数）
r = q({'uid': '1234567890'})
it = {i['id']: i for i in r['items']}
check('a2 带转发快照', it['a2']['retweeted'] and it['a2']['retweeted']['text'] == '被转的原文内容')
check('a1 带图片', it['a1']['media']['imgs'] == ['https://wx4.sinaimg.cn/large/x.jpg'])
check('a1 计数', it['a1']['reposts'] == 12 and it['a1']['atts'] == 50)

# 8. cookie 空值
check('cookie 空值拒绝', ws.api_cookie(USER, {'value': '  '})['ok'] is False)
check('cookie 保存', ws.api_cookie(USER, {'value': 'SUB=abc'})['ok'] is True)

# 9. 删除博主级联（a3 属于 999，先删 999 → 剩 a1,a2；再删 1234567890 → 空）
check('删除不存在', ws.api_delete(USER, {'uid': '0'})['ok'] is False)
ws.db("INSERT INTO bloggers(user_id,uid,nickname,state,note,created_at) VALUES(?,'999','X','done','', '2026-08-19 00:00:00')", (USER_ID,))
r = ws.api_delete(USER, {'uid': '999'})
check('删除999成功', r['ok'] is True and q({'page': '1'})['total'] == 2)
check('state移除', len(ws.blogger_rows(USER_ID)) == 0)

# 10. preview 非法 uid
check('非法uid', ws.api_preview(USER, {'input': 'hello'})['ok'] is False)

# 11. 断点续爬状态：暂停中的全量保留 next_page
ws.db("INSERT INTO bloggers(user_id,uid,nickname,state,next_page,note,created_at) "
      "VALUES(?,'1234567890','A','paused',5,'已暂停','2026-08-19 00:00:00')", (USER_ID,))
check('有断点→续全量', ws.api_sync(USER, {'uid': '1234567890'})['ok'] is True)
check('入队后queued', ws.blogger_rows(USER_ID)[0]['state'] == 'queued')
ws.api_pause(USER, {'uid': '1234567890'})
check('出队后paused', ws.blogger_rows(USER_ID)[0]['state'] == 'paused')

# 12. 批量操作
r = q({'all_ids': '1'})
check('all_ids 返回全部', r['ok'] is True and r['total'] == 2 and len(r['ids']) == 2)
r = ws.api_batch_delete(USER, {'ids': ['a1']})
check('批量删除1条', r['ok'] is True and r['deleted'] == 1)
check('删除后剩1条', q({'page': '1'})['total'] == 1)
r = ws.api_batch_update(USER, {'ids': ['a2']})
check('批量更新入队', r['ok'] is True and r['queued'] == 1)
check('独立更新队列', len(ws.REFRESH_QUEUE) == 1 and ws.refresh_prog(USER_ID)['total'] == 1)
check('批量更新不进拉取队列', len(ws.TASKQ) == 0)
r = ws.api_batch_delete(USER, {'ids': []})
check('空ids拒绝', r['ok'] is False)
r = ws.api_batch_update(USER, {'ids': []})
check('空ids更新拒绝', r['ok'] is False)
# 模拟上一批已完成，再开新批次应重置计数（只统计本次）
rp = ws.refresh_prog(USER_ID)
rp['total'] = 10
rp['done'] = 10
r = ws.api_batch_update(USER, {'ids': ['a2']})
check('新批次重置计数', r['ok'] is True and ws.refresh_prog(USER_ID)['total'] == 1 and ws.refresh_prog(USER_ID)['done'] == 0)
# 清理队列，避免影响后续
ws.TASKQ.clear()
ws.REFRESH_QUEUE.clear()
rp = ws.refresh_prog(USER_ID)
rp['total'] = 0
rp['done'] = 0

# 13. 重拉全量
r = ws.api_refull(USER, {'uid': '1234567890'})
check('重拉全量入队', r['ok'] is True)
b = ws.blogger_rows(USER_ID)[0]
check('next_page 重置为1', b['next_page'] == 1 and b['state'] == 'queued')
ws.api_pause(USER, {'uid': '1234567890'})
check('重拉全量可暂停', ws.blogger_rows(USER_ID)[0]['state'] == 'paused')
r = ws.api_refull(USER, {'uid': '0'})
check('重拉全量不存在拒绝', r['ok'] is False)
# 博主主页（无 homepage 时按 uid 推导）
check('主页链接推导', ws.blogger_rows(USER_ID)[0]['homepage'] == 'https://weibo.com/u/1234567890')

# 13b. 重拉全量可选起始日期（新交互：选择起始日期，范围=所选日期→今天）
pf_exp = int(ws.datetime.datetime.strptime('2024-01-01', '%Y-%m-%d').timestamp())
r = ws.api_refull(USER, {'uid': '1234567890', 'start': '2024-01-01'})
check('重拉全量带起始日期入队', r['ok'] is True)
check('pull_from 落库', ws.db("SELECT pull_from FROM bloggers WHERE user_id=? AND uid='1234567890'", (USER_ID,)).fetchone()['pull_from'] == pf_exp)
ws.api_cancel(USER, {'uid': '1234567890'})
check('取消清空 pull_from', ws.db("SELECT pull_from FROM bloggers WHERE user_id=? AND uid='1234567890'", (USER_ID,)).fetchone()['pull_from'] == 0)
check('非法起始日期拒绝', ws.api_refull(USER, {'uid': '1234567890', 'start': '昨天'})['ok'] is False)
check('未来起始日期拒绝', ws.api_refull(USER, {'uid': '1234567890', 'start': '2099-01-01'})['ok'] is False)

# 14. 已删除筛选
ws.db("UPDATE posts SET deleted=0 WHERE user_id=?", (USER_ID,))
ws.db("UPDATE posts SET deleted=1 WHERE user_id=? AND id='a2'", (USER_ID,))
check('已删除筛选1条', q({'deleted': '1'})['total'] == 1)
check('正常筛选0条', q({'deleted': '0'})['total'] == 0)
check('全部仍1条', q({'page': '1'})['total'] == 1)
ws.db("UPDATE posts SET deleted=0 WHERE user_id=?", (USER_ID,))

# 15. 批量更新的已删除识别
check('已删除错误识别', ws._is_deleted_error(ws.ApiError('该微博不存在')) is True)
check('普通网络错误不误标', ws._is_deleted_error(ws.ApiError('网络异常')) is False)

# 16. 全量拉取"已见清单"标记逻辑（拉到底时，未见到的才标记）
ws.db("INSERT INTO posts(user_id,id,uid,bid,text,created_ts,deleted) VALUES(?,'fakepost','1234567890','fp','x',100,0)", (USER_ID,))
ws.db("INSERT OR IGNORE INTO pull_seen(user_id,uid,id) VALUES(?,'1234567890','a2')", (USER_ID,))
ws.db("UPDATE posts SET deleted=1 WHERE user_id=? AND uid='1234567890' AND id NOT IN "
      "(SELECT id FROM pull_seen WHERE user_id=? AND uid='1234567890')", (USER_ID, USER_ID))
check('未见到的标记已删除', ws.db("SELECT deleted FROM posts WHERE user_id=? AND id='fakepost'", (USER_ID,)).fetchone()['deleted'] == 1)
check('见到的保持正常', ws.db("SELECT deleted FROM posts WHERE user_id=? AND id='a2'", (USER_ID,)).fetchone()['deleted'] == 0)
ws.db("DELETE FROM posts WHERE user_id=? AND id='fakepost'", (USER_ID,))
ws.db("DELETE FROM pull_seen WHERE user_id=?", (USER_ID,))

# 17. 取消拉取
ws.enqueue(USER_ID, '1234567890', 'full')
check('入队成功', ws.blogger_rows(USER_ID)[0]['state'] == 'queued')
r = ws.api_cancel(USER, {'uid': '1234567890'})
check('排队中取消→done', r['ok'] is True and ws.blogger_rows(USER_ID)[0]['state'] == 'done')
ws.db("UPDATE bloggers SET state='fulling', next_page=3 WHERE user_id=? AND uid='1234567890'", (USER_ID,))
ws.api_cancel(USER, {'uid': '1234567890'})
check('运行中取消已标记', (USER_ID, '1234567890') in ws.CANCEL)
ws.CANCEL.discard((USER_ID, '1234567890'))
ev = ws.STOP.setdefault((USER_ID, '1234567890'), ws.threading.Event())
ev.clear()
ws.db("UPDATE bloggers SET state='paused', next_page=NULL WHERE user_id=? AND uid='1234567890'", (USER_ID,))

# 18. 语雀归档：目录链接格式校验
check('目录格式 账号/库/目录', ws.validate_yuque_dir('https://www.yuque.com/aaa/bbb/ddd') == ('aaa', 'bbb', 'ddd'))
check('目录格式 多层路径', ws.validate_yuque_dir('https://www.yuque.com/aaa/bbb/1/2/ddd') == ('aaa', 'bbb', '1/2/ddd'))
check('目录格式 仅知识库', ws.validate_yuque_dir('https://www.yuque.com/aaa/bbb') == ('aaa', 'bbb', ''))
check('目录格式 非法协议', ws.validate_yuque_dir('http://yuque.com/aaa/bbb') is None)
check('目录格式 缺知识库', ws.validate_yuque_dir('https://www.yuque.com/aaa') is None)
check('目录格式 中文目录名', ws.validate_yuque_dir('https://www.yuque.com/someone/study/其他') == ('someone', 'study', '其他'))
check('目录格式 空格拒收', ws.validate_yuque_dir('https://www.yuque.com/aaa/bb b') is None)

# 19. 博主语雀目录设置
r = ws.api_blogger_yuque_dir(USER, {'uid': '1234567890', 'dir': 'https://www.yuque.com/aaa/bbb/ddd'})
check('设置目录成功', r['ok'] is True)
r = ws.api_blogger_yuque_dir(USER, {'uid': '1234567890', 'dir': 'not-a-url'})
check('非法目录拒绝', r['ok'] is False)
check('blogger_rows 带 yuque_dir', ws.blogger_rows(USER_ID)[0]['yuque_dir'] == 'https://www.yuque.com/aaa/bbb/ddd')

# 20. 归档预检与入队
check('归档空ids拒绝', ws.api_yuque_sync(USER, {'ids': []})['ok'] is False)
ws.db("INSERT INTO posts(user_id,id,uid,bid,text,created_ts,media_json,retweeted_json,raw_json,fetched_at) "
      "VALUES(?,'a4','1234567890','a4','普通微博',?,'{}','','{}','2026-08-19 00:00:00')", (USER_ID, t - 50))
ws.api_blogger_yuque_dir(USER, {'uid': '1234567890', 'dir': ''})
r = ws.api_yuque_sync(USER, {'ids': ['a4']})
check('未配目录报博主', r['ok'] is False and '博主' in r['error'])
ws.api_blogger_yuque_dir(USER, {'uid': '1234567890', 'dir': 'https://www.yuque.com/aaa/bbb/ddd'})
r = ws.api_yuque_sync(USER, {'ids': ['a2']})
check('转发微博不可归档', r['ok'] is False)
for _k in ('ai_base_url', 'ai_key', 'ai_model', 'ai_pool'):
    ws.kv_set(_k, '')
r = ws.api_yuque_sync(USER, {'ids': ['a4']})
check('AI未配置拒绝入队', r['ok'] is False and '还没开通' in r['error'])
ws.kv_set('ai_base_url', 'https://ai.example.com')
ws.kv_set('ai_key', 'sk-abcdefg1234567')
ws.kv_set('ai_model', 'test-model')
r = ws.api_yuque_sync(USER, {'ids': ['a4']})
check('单条归档入队', r['ok'] is True and r['queued'] == 1)
check('归档队列1条', len(ws.SYNC_QUEUE) == 1)
ws.SYNC_QUEUE.clear()
sp = ws.sync_prog(USER_ID)
sp['total'] = 0
sp['done'] = 0
sp['msg'] = ''

# 21. 归档筛选 + 状态字段
ws.db("UPDATE posts SET archived=1, yuque_doc_url='https://www.yuque.com/aaa/bbb/doc', arch_fail='', arch_skip=0, arch_state='' WHERE user_id=? AND id='a4'", (USER_ID,))
check('已归档筛选1条', q({'arch': 'done'})['total'] == 1)
check('待归档筛选1条(a2待归档)', q({'arch': 'pending'})['total'] == 1)
r = q({'page': '1'})
it = {i['id']: i for i in r['items']}
check('返回归档字段', it['a4']['archived'] == 1 and it['a4']['yuque_doc_url'].endswith('/doc'))
check('state 带归档进度', ws.api_state(USER)['yuque_total'] == 0)

# 22. 已归档可再同步（瞬态标记 updating + 入队）
r = ws.api_yuque_sync(USER, {'ids': ['a4']})
check('已归档可再同步入队', r['ok'] is True and r['updated'] == 1 and r['created'] == 0)
check('瞬态标记 updating', ws.db("SELECT arch_state FROM posts WHERE user_id=? AND id='a4'", (USER_ID,)).fetchone()['arch_state'] == 'updating')
ws.SYNC_QUEUE.clear()
sp = ws.sync_prog(USER_ID)
sp['total'] = 0
sp['done'] = 0
sp['msg'] = ''

# 22b. AI 归档配置池管理 API（ADR-0013）：增/改/删/停用/排序；key 只回掩码永不原文
r = ws.api_admin_ai_config(USER)
check('旧三键已自动迁移进池', r['ok'] and len(r['configs']) == 1
      and r['configs'][0]['base_url'] == 'https://ai.example.com'
      and r['configs'][0]['key_set'] and ws.kv_get('ai_key') == '')
check('ai_config 读取不回显 key 原文', 'sk-abcdefg1234567' not in str(r)
      and r['configs'][0]['key_masked'])
r = ws.api_admin_ai_config_save(USER, {'action': 'add', 'base_url': 'https://ai2.example.com/v1/',
                                       'key': 'sk-second0000000', 'model': 'm2'})
check('add 追加备用并去尾斜杠', r['ok'] and len(ws.api_admin_ai_config(USER)['configs']) == 2
      and ws.api_admin_ai_config(USER)['configs'][1]['base_url'] == 'https://ai2.example.com/v1')
r = ws.api_admin_ai_config_save(USER, {'action': 'add', 'base_url': 'https://ai3.example.com'})
check('add 缺三项拒绝', r['ok'] is False)
ws.api_admin_ai_config_save(USER, {'action': 'update', 'index': 1, 'model': 'm2b', 'key': ''})
c1 = ws.api_admin_ai_config(USER)['configs'][1]
check('update key 留空保持原值', c1['key_set'] and c1['model'] == 'm2b')
ws.api_admin_ai_config_save(USER, {'action': 'update', 'index': 1, 'clear_key': 1})
check('update clear_key 清除密钥', ws.api_admin_ai_config(USER)['configs'][1]['key_set'] is False)
ws.api_admin_ai_config_save(USER, {'action': 'update', 'index': 1, 'key': 'sk-second0000000'})
ws.api_admin_ai_config_save(USER, {'action': 'toggle', 'index': 1})
check('停用备用后主用仍可用', ws.api_admin_ai_config(USER)['configs'][1]['enabled'] is False
      and len(ws.ai_cfgs()) == 1)
r = ws.api_admin_ai_config_save(USER, {'action': 'toggle', 'index': 0})
check('全部停用即未开通', ws.ai_cfgs() == [] and r['configured'] is False)
ws.api_admin_ai_config_save(USER, {'action': 'toggle', 'index': 0})
ws.api_admin_ai_config_save(USER, {'action': 'toggle', 'index': 1})
ws.api_admin_ai_config_save(USER, {'action': 'move', 'index': 1, 'dir': 'up'})
check('move 上移换序（备用变主用）', ws.api_admin_ai_config(USER)['configs'][0]['model'] == 'm2b')
r = ws.api_admin_ai_config_save(USER, {'action': 'move', 'index': 0, 'dir': 'up'})
check('顶到头报错', r['ok'] is False)
ws.api_admin_ai_config_save(USER, {'action': 'move', 'index': 0, 'dir': 'down'})
check('move 换回原序', ws.api_admin_ai_config(USER)['configs'][0]['model'] == 'test-model')
r = ws.api_admin_ai_config_save(USER, {'action': 'delete', 'index': 9})
check('越界操作提示已不存在', r['ok'] is False and '刷新' in r['error'])
ws.api_admin_ai_config_save(USER, {'action': 'delete', 'index': 1})
check('delete 移除条目', len(ws.api_admin_ai_config(USER)['configs']) == 1)

# 22c. 云托管快照恢复语义（ADR-0011）：seed.db 优先且一次性、无 seed 走快照
import shutil as _shutil, tempfile as _tf
_real_db, _real_cos = ws.DB_PATH, ws.COS_DIR
_cos = _tf.mkdtemp(prefix='wb-cos-')
_dbp = os.path.join(_cos, 'run', 'weibo.db')
ws.COS_DIR, ws.DB_PATH = _cos, _dbp
open(os.path.join(_cos, 'weibo.db'), 'wb').write(b'SNAP')
open(os.path.join(_cos, 'seed.db'), 'wb').write(b'SEED')
ws.cos_restore()
check('恢复优先用 seed.db', os.path.exists(_dbp) and open(_dbp, 'rb').read() == b'SEED'
      and os.path.exists(os.path.join(_cos, 'seed.db.used'))
      and not os.path.exists(os.path.join(_cos, 'seed.db')))
os.remove(_dbp)
ws.cos_restore()
check('无 seed 时走快照', open(_dbp, 'rb').read() == b'SNAP')
open(_dbp, 'wb').write(b'LOCAL')
open(os.path.join(_cos, 'weibo.db'), 'wb').write(b'SNAP2')
ws.cos_restore()
check('本地已有库不动', open(_dbp, 'rb').read() == b'LOCAL')
os.remove(_dbp)
os.remove(os.path.join(_cos, 'weibo.db'))
ws.cos_restore()
check('两处皆无则跳过不报错', not os.path.exists(_dbp))
_shutil.rmtree(_cos, ignore_errors=True)
ws.DB_PATH, ws.COS_DIR = _real_db, _real_cos
ws.DB_PATH, ws.COS_DIR = _real_db, _real_cos
_shutil.rmtree(_cos, ignore_errors=True)
ws.db("UPDATE posts SET arch_state='' WHERE user_id=? AND id='a4'", (USER_ID,))

# 22d. 主备降级（ADR-0013）：主用任何失败自动试下一家，整链失败才报错
seen = []
_real_complete = ws._ai_complete
def _fake_ok(prompt, c):
    seen.append(c['name'])
    return 'TITLE: 格式漂移没有正文' if c['name'] == '主用' else 'TITLE: 标题\nBODY: 正文'
ws._ai_complete = _fake_ok
title, body_md = ws._ai_generate('p', [{'name': '主用'}, {'name': '备用'}])
check('主用失败自动换备用', title == '标题' and body_md == '正文' and seen == ['主用', '备用'])
def _fake_down(prompt, c):
    raise ws.ApiError('接口错误（500）')
ws._ai_complete = _fake_down
try:
    ws._ai_generate('p', [{'name': '主用'}, {'name': '备用'}])
    err = ''
except ws.ApiError as e:
    err = str(e)
check('整链失败报全部没成功并带各家原因', '全部没成功' in err and '主用' in err and '备用' in err)
# 成功次数：整链成功给成功那家累加（按 备注名|地址|模型 归并），失败不加
ws._ai_complete = lambda prompt, c: 'TITLE: 标题\nBODY: 正文'
cfg0 = ws.ai_cfgs()[0]
n0 = ws.ai_stats().get(ws._ai_stat_key(cfg0), 0)
ws._ai_generate('p', [cfg0])
check('成功次数记在对应配置并回显管理接口',
      ws.ai_stats().get(ws._ai_stat_key(cfg0), 0) == n0 + 1
      and ws.api_admin_ai_config(USER)['configs'][0]['ok_count'] == n0 + 1)
def _fake_down2(prompt, c):
    raise ws.ApiError('挂了')
ws._ai_complete = _fake_down2
try:
    ws._ai_generate('p', [cfg0])
except ws.ApiError:
    pass
check('失败不计数', ws.ai_stats().get(ws._ai_stat_key(cfg0), 0) == n0 + 1)
check('池条目带稳定 id', bool(ws.ai_pool()[0].get('id')))
ws.api_admin_ai_config_save(USER, {'action': 'update', 'index': 0, 'model': 'test-model-b'})
check('改模型名后成功次数保留（按条目 id 归并）',
      ws.api_admin_ai_config(USER)['configs'][0]['ok_count'] == n0 + 1)
ws.api_admin_ai_config_save(USER, {'action': 'update', 'index': 0, 'model': 'test-model'})
ws._ai_complete = _real_complete

# 22f. 正文代码拼装（ADR-0014）：AI 只出占位，全文由 _fill_body 填入
check('占位符替换为全文',
      ws._fill_body('前\n\n## 微博正文\n\n{{微博正文}}\n', '原文多行') == '前\n\n## 微博正文\n\n原文多行\n')
_filled = ws._fill_body('正文没给占位', '全文')
check('缺占位兜底追加章节', _filled.endswith('## 微博正文\n\n全文') and _filled.startswith('正文没给占位'))

# 22e. 数据库备份下载（断点续传）：200 全量 / Range 206 / 越界 416，临时副本用完即删
import io as _io, email.message as _em
def _dl(range_hdr=None):
    h = ws.Handler.__new__(ws.Handler)
    h.headers = _em.Message()
    if range_hdr:
        h.headers['Range'] = range_hdr
    h.request_version = 'HTTP/1.0'
    h.requestline = 'GET /api/admin/db_backup HTTP/1.0'
    h.close_connection = True
    h.wfile = _io.BytesIO()
    ws.Handler._send_db_backup(h)
    head, _, body = h.wfile.getvalue().partition(b'\r\n\r\n')
    return head, body
head, full = _dl()
check('备份下载 200 带附件名与续传声明', head.startswith(b'HTTP/1.0 200 OK')
      and b'weibo-backup-' in head and b'Accept-Ranges: bytes' in head)
check('备份内容是完整 SQLite 库', full[:15] == b'SQLite format 3'
      and ('Content-Length: %d' % len(full)).encode() in head)
head2, part = _dl('bytes=10-29')
check('Range 续传返回 206 与对应切片', head2.startswith(b'HTTP/1.0 206')
      and part == full[10:30] and ('bytes 10-29/%d' % len(full)).encode() in head2)
head3, _ = _dl('bytes=99999999999-')
check('起点越界返回 416', head3.startswith(b'HTTP/1.0 416')
      and ('bytes */%d' % len(full)).encode() in head3)
check('临时副本用完即删', not os.path.exists(ws.DB_PATH + '.dl'))

# 23. 失败状态筛选 + 原因返回
ws.db("UPDATE posts SET arch_fail='目录不存在' WHERE user_id=? AND id='a4'", (USER_ID,))
ws.db("UPDATE posts SET arch_fail='超时', arch_skip=0, archived=0 WHERE user_id=? AND id='a2'", (USER_ID,))
check('更新失败筛选1条', q({'arch': 'update_fail'})['total'] == 1)
check('同步失败筛选1条', q({'arch': 'sync_fail'})['total'] == 1)
r = q({'page': '1'})
it = {i['id']: i for i in r['items']}
check('返回失败原因', it['a4']['arch_fail'] == '目录不存在' and it['a2']['arch_fail'] == '超时')
ws.db("UPDATE posts SET arch_fail='' WHERE user_id=? AND id='a2'", (USER_ID,))
ws.db("UPDATE posts SET arch_fail='', archived=1 WHERE user_id=? AND id='a4'", (USER_ID,))

# 24. 批量改为无需归档 / 改回待归档
r = ws.api_yuque_mark(USER, {'ids': ['a2', 'a4'], 'to': 'skip'})
check('批量改为无需归档', r['ok'] is True and r['updated'] == 2)
check('无需归档筛选2条', q({'arch': 'skip'})['total'] == 2)
r = ws.api_yuque_mark(USER, {'ids': ['a2'], 'to': 'pending'})
check('改回待归档', r['ok'] is True)
check('待归档筛选1条', q({'arch': 'pending'})['total'] == 1)
check('无需归档剩1条', q({'arch': 'skip'})['total'] == 1)
r = ws.api_yuque_mark(USER, {'ids': [], 'to': 'skip'})
check('mark 空ids拒绝', r['ok'] is False)

# 25. 添加博主带起始日期 + 博主排序 + 转发自动标「无需归档」
pf_add = int(ws.datetime.datetime.strptime('2024-06-01', '%Y-%m-%d').timestamp())
check('添加未来日期拒绝', ws.api_add(USER, {'input': '888999', 'start': '2099-01-01'})['ok'] is False)
check('添加非法日期拒绝', ws.api_add(USER, {'input': '888999', 'start': '昨天'})['ok'] is False)
r = ws.api_add(USER, {'input': '777888', 'start': '2024-06-01'})
check('添加带起始日期入队', r['ok'] is True)
check('添加起始日期落库', ws.db("SELECT pull_from FROM bloggers WHERE user_id=? AND uid='777888'", (USER_ID,)).fetchone()['pull_from'] == pf_add)
check('新博主排最后', ws.blogger_rows(USER_ID)[-1]['uid'] == '777888')
# 上移：777888 与 1234567890 换位
r = ws.api_blogger_move(USER, {'uid': '777888', 'dir': 'up'})
check('上移成功', r['ok'] is True and [x['uid'] for x in ws.blogger_rows(USER_ID)] == ['777888', '1234567890'])
# 最前的再上移应保持不动
r = ws.api_blogger_move(USER, {'uid': '777888', 'dir': 'up'})
check('最前上移无变化', [x['uid'] for x in ws.blogger_rows(USER_ID)] == ['777888', '1234567890'])
check('移动方向校验', ws.api_blogger_move(USER, {'uid': '777888', 'dir': 'left'})['ok'] is False)
check('移动不存在博主', ws.api_blogger_move(USER, {'uid': '0', 'dir': 'up'})['ok'] is False)
# 转发微博拉取 → 自动标「无需归档」
import json as _json
rt_mb = {'id': 'rt_auto', 'bid': 'rt_auto', 'text': '转发测试',
         'created_at': time.strftime('%Y-%m-%d %H:%M:%S'),
         'reposts_count': 0, 'comments_count': 0, 'attitudes_count': 0,
         'retweeted_status': {'user': {'screen_name': '原博主'}, 'text': '原文',
                              'created_at': '2026-08-19 00:00:00'}}
ws.upsert_post(USER_ID, None, '777888', rt_mb)
row = ws.db("SELECT arch_skip, retweeted_json FROM posts WHERE user_id=? AND id='rt_auto'", (USER_ID,)).fetchone()
check('转发自动标无需归档', row['arch_skip'] == 1 and _json.loads(row['retweeted_json'])['nickname'] == '原博主')
# 普通微博不误标
ws.upsert_post(USER_ID, None, '777888', {'id': 'nrt_1', 'bid': 'nrt_1', 'text': '普通微博',
                                         'created_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                                         'reposts_count': 0, 'comments_count': 0, 'attitudes_count': 0})
check('普通微博不误标', ws.db("SELECT arch_skip FROM posts WHERE user_id=? AND id='nrt_1'", (USER_ID,)).fetchone()['arch_skip'] == 0)

# 26. 已有长微博增量刷新不再补拉全文（避免每次增量都为整页长微博白等）
ws.time.sleep = lambda s: None
called = {'n': 0}
ws.fetch_post_detail = lambda session, pid: called.__setitem__('n', called['n'] + 1) or {}
ws.db("INSERT INTO posts(user_id,id,uid,bid,text,created_ts,deleted) "
      "VALUES(?,'long_exist','777888','le','旧正文',?,0)", (USER_ID, t - 100))
r = ws.upsert_post(USER_ID, None, '777888', {'id': 'long_exist', 'bid': 'long_exist', 'text': '列表正文',
                                             'isLongText': True,
                                             'created_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                                             'reposts_count': 5, 'comments_count': 2, 'attitudes_count': 1})
check('已有长微博不补拉全文', called['n'] == 0 and r == 'update')
check('已有长微博计数已刷新', ws.db("SELECT reposts FROM posts WHERE user_id=? AND id='long_exist'", (USER_ID,)).fetchone()['reposts'] == 5)
called['n'] = 0
ws.fetch_post_detail = lambda session, pid: called.__setitem__('n', called['n'] + 1) or {'id': pid, 'text': '完整长文'}
r = ws.upsert_post(USER_ID, None, '777888', {'id': 'long_new', 'bid': 'long_new', 'text': '列表截断',
                                             'isLongText': True,
                                             'created_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                                             'reposts_count': 0, 'comments_count': 0, 'attitudes_count': 0})
check('新长微博仍补拉全文', called['n'] == 1)

# 27. 重拉全量·选起始日期：范围停止 + 范围外博文不动（打桩直跑 run_sync，不发网络）
ws.random.uniform = lambda a, b: 0.1
def _ts(y, m, d):
    return int(ws.datetime.datetime(y, m, d).timestamp())
def _mb(pid, ts, pinned=False):
    d = {'id': pid, 'bid': pid, 'text': '微博' + pid,
         'created_at': time.strftime('%Y-%m-%d %H:%M', time.localtime(ts)),
         'reposts_count': 0, 'comments_count': 0, 'attitudes_count': 0}
    if pinned:
        d['isTop'] = True
    return d
def _post(id_):
    return ws.db("SELECT * FROM posts WHERE user_id=? AND id=?", (USER_ID, id_)).fetchone()
def _run_range(pages, start_ts, pre_old_ts, pre_gone_ts):
    ws.fetch_page = lambda session, uid, page: pages.get(page, [])
    ws.db("INSERT INTO bloggers(user_id,uid,nickname,state,next_page,pull_from,note,created_at) "
          "VALUES(?,'99001','范围测试','idle',1,?,'','2026-08-19 00:00:00')", (USER_ID, start_ts))
    ws.db("INSERT INTO posts(user_id,id,uid,bid,text,created_ts,deleted) VALUES(?,'r_old','99001','x','旧',?,0)",
          (USER_ID, _ts(*pre_old_ts)))
    ws.db("INSERT INTO posts(user_id,id,uid,bid,text,created_ts,deleted) VALUES(?,'r_gone','99001','x','表',?,0)",
          (USER_ID, _ts(*pre_gone_ts)))
    ws.run_sync(USER_ID, '99001', 'full')
    b = ws.db("SELECT state, next_page, pull_from, note FROM bloggers WHERE user_id=? AND uid='99001'", (USER_ID,)).fetchone()
    return b
# 选起始 2024-01-01：翻到比它更早的非置顶博文就停
b = _run_range(
    {1: [_mb('r0', _ts(2020, 1, 1), pinned=True), _mb('r1', _ts(2025, 12, 31)), _mb('r2', _ts(2025, 6, 1))],
     2: [_mb('r3', _ts(2024, 6, 15)), _mb('r4', _ts(2024, 1, 2)), _mb('r5', _ts(2023, 12, 31))]},
    _ts(2024, 1, 1), (2023, 6, 1), (2025, 3, 1))
check('范围内博文全部入库', all(_post(p) for p in ('r0', 'r1', 'r2', 'r3', 'r4')))
check('更早博文不入库', _post('r5') is None)
check('置顶旧博文也入库', _post('r0') is not None)
check('范围外更早博文不动', _post('r_old')['deleted'] == 0)
check('范围内已删博文被标记', _post('r_gone')['deleted'] == 1)
check('范围任务完成并清场', b['state'] == 'done' and b['next_page'] is None and b['pull_from'] == 0)
check('完成说明含起始日期', '2024-01-01' in b['note'])
ws.db("DELETE FROM bloggers WHERE user_id=? AND uid='99001'", (USER_ID,))
ws.db("DELETE FROM posts WHERE user_id=? AND uid='99001'", (USER_ID,))
ws.db("DELETE FROM pull_seen WHERE user_id=? AND uid='99001'", (USER_ID,))
# 不选起始（pull_from=0）：老行为，范围外更早博文一并标记删除
b = _run_range(
    {1: [_mb('q1', _ts(2025, 12, 31)), _mb('q2', _ts(2025, 6, 1))],
     2: [_mb('q3', _ts(2024, 6, 15))]},
    0, (2023, 6, 1), (2025, 3, 1))
check('无起始→更早博文也标记删除', _post('r_old')['deleted'] == 1)
check('无起始→范围内已删标记', _post('r_gone')['deleted'] == 1)
ws.db("DELETE FROM bloggers WHERE user_id=? AND uid='99001'", (USER_ID,))
ws.db("DELETE FROM posts WHERE user_id=? AND uid='99001'", (USER_ID,))
ws.db("DELETE FROM pull_seen WHERE user_id=? AND uid='99001'", (USER_ID,))
check('新长微博落库完整正文', ws.db("SELECT text FROM posts WHERE user_id=? AND id='long_new'", (USER_ID,)).fetchone()['text'] == '完整长文')

# 28. 一键全部博主拉取新微博
ws.TASKQ.clear()
ws.db("UPDATE bloggers SET state='done', next_page=NULL WHERE user_id=? AND uid IN ('1234567890','777888')", (USER_ID,))
r = ws.api_sync_all(USER, {})
check('全部拉取入队', r['ok'] is True and r['total'] == 2 and len(r['started']) == 2)
check('全部入队后 queued', all(b['state'] == 'queued' for b in ws.blogger_rows(USER_ID)))
ws.TASKQ.clear()
ws.db("UPDATE bloggers SET state='done', next_page=NULL WHERE user_id=? AND uid IN ('1234567890','777888')", (USER_ID,))
ws.db("UPDATE bloggers SET state='fulling' WHERE user_id=? AND uid='1234567890'", (USER_ID,))
r = ws.api_sync_all(USER, {})
check('已在拉取的跳过', r['ok'] is True and r['total'] == 1 and r['skipped'] == 1 and r['started'] == ['777888'])
ws.TASKQ.clear()
ws.db("UPDATE bloggers SET state='done' WHERE user_id=? AND uid IN ('1234567890','777888')", (USER_ID,))
ws.db("UPDATE bloggers SET state='done', next_page=2 WHERE user_id=? AND uid='1234567890'", (USER_ID,))
r = ws.api_sync_all(USER, {})
check('有断点续全量入队', r['ok'] is True and r['total'] == 2)
modes = dict((t[1], t[2]) for t in ws.TASKQ)
check('断点博主续全量', modes.get('1234567890') == 'full' and modes.get('777888') == 'incr')
ws.TASKQ.clear()
# 无博主 / 无登录信息
ws.db("DELETE FROM bloggers WHERE user_id=?", (USER_ID,))
check('无博主拒绝', ws.api_sync_all(USER, {})['ok'] is False)
ws.db("INSERT INTO bloggers(user_id,uid,nickname,state,note,created_at) VALUES(?,'888888','B','done','', '2026-08-19 00:00:00')", (USER_ID,))
ws.ukv_set(USER_ID, 'cookie', '')
r = ws.api_sync_all(USER, {})
check('无登录信息拒绝', r['ok'] is False and '登录信息' in r['error'])

# 29. 定时拉取设置 + 归档删除
def kv_last_is_now():
    v = ws.ukv_get(USER_ID, 'sched_last')
    return bool(v) and abs(float(v) - time.time()) < 10
check('定时 低于30分钟拒绝', ws.api_schedule(USER, {'on': True, 'minutes': 10})['ok'] is False)
check('定时 超1440拒绝', ws.api_schedule(USER, {'on': True, 'minutes': 2000})['ok'] is False)
check('定时 非数字拒绝', ws.api_schedule(USER, {'on': True, 'minutes': 'abc'})['ok'] is False)
check('定时 开启成功', ws.api_schedule(USER, {'on': True, 'minutes': 60})['ok'] is True)
check('定时 配置落库', ws.sched_cfg(USER_ID) == (True, 60) and kv_last_is_now())
check('定时 state 带配置', ws.api_state(USER)['schedule'] == {'on': True, 'minutes': 60})
check('定时 关闭成功', ws.api_schedule(USER, {'on': False})['ok'] is True and ws.sched_cfg(USER_ID)[0] is False)
check('定时 越界值夹回', (ws.ukv_set(USER_ID, 'sched_minutes', '5'), ws.sched_cfg(USER_ID)[1] == 30)[1])

check('文档链接解析', ws.parse_yuque_doc_url('https://www.yuque.com/acc/book/slug1') == ('acc/book', 'slug1'))
check('文档链接带参数', ws.parse_yuque_doc_url('https://www.yuque.com/acc/book/slug1?a=1') == ('acc/book', 'slug1'))
check('文档链接非法', ws.parse_yuque_doc_url('https://www.yuque.com/acc/book') is None)

ws.db("UPDATE posts SET archived=1, arch_skip=0, arch_fail='', arch_state='', "
      "yuque_doc_url='https://www.yuque.com/acc/book/doc1', archived_at='2026-08-30' WHERE user_id=? AND id='a2'", (USER_ID,))
del_calls = []
ws.yuque_delete_doc = lambda url, token: del_calls.append((url, token))
r = ws.api_yuque_delete(USER, {'ids': ['a2']})
row = ws.db("SELECT archived, arch_skip, yuque_doc_url FROM posts WHERE user_id=? AND id='a2'", (USER_ID,)).fetchone()
check('归档删除成功重置待归档', r['ok'] and r['deleted'] == 1 and row['archived'] == 0 and row['yuque_doc_url'] == '')
check('归档删除调了远端', del_calls == [('https://www.yuque.com/acc/book/doc1', 'test-token')])
check('归档删除无文档拒绝', ws.api_yuque_delete(USER, {'ids': ['a2']})['ok'] is False)
ws.db("UPDATE posts SET archived=1, yuque_doc_url='https://www.yuque.com/acc/book/doc1' WHERE user_id=? AND id='a2'", (USER_ID,))
def _raise(url, token):
    raise ws.ApiError('语雀接口错误（500）')
ws.yuque_delete_doc = _raise
r = ws.api_yuque_delete(USER, {'ids': ['a2']})
row = ws.db("SELECT archived, yuque_doc_url FROM posts WHERE user_id=? AND id='a2'", (USER_ID,)).fetchone()
check('归档删除失败保留原状态', r['deleted'] == 0 and r['failed'] == 1 and row['archived'] == 1 and row['yuque_doc_url'])
check('归档删除失败带原因', '500' in r['error_sample'])
ws.db("UPDATE posts SET arch_state='syncing' WHERE user_id=? AND id='a2'", (USER_ID,))
ws.yuque_delete_doc = lambda url, token: None
check('归档删除瞬态跳过', ws.api_yuque_delete(USER, {'ids': ['a2']})['ok'] is False)
ws.db("UPDATE posts SET arch_state='' WHERE user_id=? AND id='a2'", (USER_ID,))
check('归档删除空ids拒绝', ws.api_yuque_delete(USER, {'ids': []})['ok'] is False)

# 30. 管理端状态过滤（deactivated_at 为 NOT NULL DEFAULT 0，必须用 =0/>0 而非 IS NULL）
ws.db("INSERT INTO users(username,pass_hash,pass_salt,role,disabled,deactivated_at,created_at) "
      "VALUES('u_normal','x','0','user',0,0,?)", (ws.now_str(),))
ws.db("INSERT INTO users(username,pass_hash,pass_salt,role,disabled,deactivated_at,created_at) "
      "VALUES('u_disabled','x','0','user',1,0,?)", (ws.now_str(),))
ws.db("INSERT INTO users(username,pass_hash,pass_salt,role,disabled,deactivated_at,created_at) "
      "VALUES('u_deact','x','0','user',0,?,?)", (int(time.time()), ws.now_str()))
def admin_usernames(status):
    return {u['username'] for u in ws.api_admin_users(USER, {'status': [status]})['users']}
check('管理 正常不含注销/禁用', 'u_normal' in admin_usernames('normal')
      and 'u_deact' not in admin_usernames('normal') and 'u_disabled' not in admin_usernames('normal'))
check('管理 禁用只含禁用', admin_usernames('disabled') == {'u_disabled'})
check('管理 注销只含注销', admin_usernames('deactivated') == {'u_deact'})

# 31. 账号与会话（spec §8.2/8.3：注册/登录/限流/注销/取消注销/会话/me）
pw = 'testpass123'
salt_u = ws.secrets.token_hex(16)
ws.db('INSERT INTO users(username,pass_hash,pass_salt,role,can_archive,created_at) '
      "VALUES('authtest',?,?,'user',0,?)", (ws._hash_password(pw, salt_u), salt_u, ws.now_str()))
auth_user = ws.db("SELECT * FROM users WHERE username='authtest'").fetchone()
inv = ws.kv_get('invite_code') or ''
check('注册 邀请码不对拒绝', ws.api_register({'username': 'newbie', 'password': pw, 'invite': 'bad'})['ok'] is False)
check('注册 短密码拒绝', ws.api_register({'username': 'newbie', 'password': 'short', 'invite': inv})['ok'] is False)
check('注册 用户名格式拒绝', ws.api_register({'username': 'a', 'password': pw, 'invite': inv})['ok'] is False)
r = ws.api_register({'username': 'newbie', 'password': pw, 'invite': inv})
check('注册 成功发会话', r['ok'] is True and bool(r['token']))
check('登录 错密码拒绝', ws.api_login({'username': 'authtest', 'password': 'wrongpass'})['ok'] is False)
r = ws.api_login({'username': 'authtest', 'password': pw})
check('登录 成功发会话', r['ok'] is True and bool(r['token']))
check('会话 有效token可取用户', ws.session_user(r['token'])['username'] == 'authtest')
check('会话 伪造token返回None', ws.session_user('deadbeef') is None)
ws.db("UPDATE users SET disabled=1 WHERE username='authtest'")
r = ws.api_login({'username': 'authtest', 'password': pw})
check('登录 禁用账号拒绝', r['ok'] is False and '停用' in r['error'])
ws.db("UPDATE users SET disabled=0 WHERE username='authtest'")
ws.db("UPDATE users SET deactivated_at=? WHERE username='authtest'", (int(time.time()) - 100,))
r = ws.api_login({'username': 'authtest', 'password': pw})
check('登录 注销宽限内提示反悔', r['ok'] is False and r.get('deactivated') and r.get('days_left') is not None)
r = ws.api_cancel_deactivate({'username': 'authtest', 'password': pw})
check('取消注销 发会话', r['ok'] is True and bool(r['token']))
check('取消注销 清标记', ws.db("SELECT deactivated_at FROM users WHERE username='authtest'").fetchone()['deactivated_at'] == 0)
check('取消注销 不在流程拒绝', ws.api_cancel_deactivate({'username': 'authtest', 'password': pw})['ok'] is False)
ws.db("UPDATE users SET deactivated_at=? WHERE username='authtest'", (int(time.time()) - 8 * 86400,))
r = ws.api_login({'username': 'authtest', 'password': pw})
check('登录 超7天注销视同不存在', r['ok'] is False and 'deactivated' not in r and '错误' in r['error'])
for _ in range(5):
    ws.api_login({'username': 'throttleme', 'password': 'bad'})
check('登录 连续失败限流', ws.api_login({'username': 'throttleme', 'password': 'bad'})['ok'] is False)
r = ws.api_me(auth_user)
check('me 带 cookie_set', r['user'].get('cookie_set') is False)
ws.ukv_set(auth_user['id'], 'cookie', 'SUB=abc')
check('me cookie_set 随配置', ws.api_me(auth_user)['user']['cookie_set'] is True)

print('---- RESULT: %s ----' % ('ALL PASS' if ok else 'HAS FAILURES'))
sys.exit(0 if ok else 1)
