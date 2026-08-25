# -*- coding: utf-8 -*-
"""进程内冒烟测试：灌测试数据 → 直接调 API 函数验证查询/筛选/删除/转义。
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

t = int(time.time())
c = sqlite3.connect(DB)
c.execute("INSERT INTO posts(id,uid,bid,text,created_ts,reposts,comments,atts,media_json,retweeted_json,raw_json,fetched_at) "
          "VALUES('a1','1234567890','a1','第一条：介绍微博存档工具',%d,12,3,50,'{\"imgs\":[\"https://wx4.sinaimg.cn/large/x.jpg\"]}','', '{}','2026-08-19 00:00:00')" % (t - 86400))
c.execute("INSERT INTO posts(id,uid,bid,text,created_ts,reposts,comments,atts,media_json,retweeted_json,raw_json,fetched_at) "
          "VALUES('a2','1234567890','a2','转发测试',%d,1,0,2,'{}','{\"nickname\":\"原博主\",\"text\":\"被转的原文内容\",\"imgs\":[]}','{}','2026-08-19 00:00:00')" % (t - 3600))
c.execute("INSERT INTO posts(id,uid,bid,text,created_ts,reposts,comments,atts,media_json,retweeted_json,raw_json,fetched_at) "
          "VALUES('a3','999','a3','另一位的微博',%d,0,0,0,'{}','','{}','2026-08-19 00:00:00')" % (t - 100))
c.commit()
c.close()

ok = True
def check(name, cond):
    global ok
    print(('PASS ' if cond else 'FAIL ') + name)
    if not cond:
        ok = False

def q(params):
    return ws.api_posts(dict((k, [v]) for k, v in params.items()))

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
check('cookie 空值拒绝', ws.api_cookie({'value': '  '})['ok'] is False)
check('cookie 保存', ws.api_cookie({'value': 'SUB=abc'})['ok'] is True)

# 9. 删除博主级联（a3 属于 999，先删 999 → 剩 a1,a2；再删 1234567890 → 空）
check('删除不存在', ws.api_delete({'uid': '0'})['ok'] is False)
ws.db("INSERT INTO bloggers(uid,nickname,state,note,created_at) VALUES('999','X','done','', '2026-08-19 00:00:00')")
r = ws.api_delete({'uid': '999'})
check('删除999成功', r['ok'] is True and q({'page': '1'})['total'] == 2)
check('state移除', len(ws.blogger_rows()) == 0)

# 10. preview 非法 uid
check('非法uid', ws.api_preview({'input': 'hello'})['ok'] is False)

# 11. 断点续爬状态：暂停中的全量保留 next_page
ws.db("INSERT INTO bloggers(uid,nickname,state,next_page,note,created_at) "
      "VALUES('1234567890','A','paused',5,'已暂停','2026-08-19 00:00:00')")
check('有断点→续全量', ws.api_sync({'uid': '1234567890'})['ok'] is True)
check('入队后queued', ws.blogger_rows()[0]['state'] == 'queued')
ws.api_pause({'uid': '1234567890'})
check('出队后paused', ws.blogger_rows()[0]['state'] == 'paused')

# 12. 批量操作
r = q({'all_ids': '1'})
check('all_ids 返回全部', r['ok'] is True and r['total'] == 2 and len(r['ids']) == 2)
r = ws.api_batch_delete({'ids': ['a1']})
check('批量删除1条', r['ok'] is True and r['deleted'] == 1)
check('删除后剩1条', q({'page': '1'})['total'] == 1)
r = ws.api_batch_update({'ids': ['a2']})
check('批量更新入队', r['ok'] is True and r['queued'] == 1)
check('独立更新队列', len(ws.REFRESH_QUEUE) == 1 and ws.REFRESH['total'] == 1)
check('拉取队列不受影响', all(u != '__refresh__' for u, _ in ws.TASKQ))
r = ws.api_batch_delete({'ids': []})
check('空ids拒绝', r['ok'] is False)
r = ws.api_batch_update({'ids': []})
check('空ids更新拒绝', r['ok'] is False)
# 模拟上一批已完成，再开新批次应重置计数（只统计本次）
ws.REFRESH['total'] = 10
ws.REFRESH['done'] = 10
r = ws.api_batch_update({'ids': ['a2']})
check('新批次重置计数', r['ok'] is True and ws.REFRESH['total'] == 1 and ws.REFRESH['done'] == 0)
# 清理队列，避免影响后续
ws.TASKQ.clear()
ws.REFRESH_QUEUE.clear()
ws.REFRESH['total'] = 0
ws.REFRESH['done'] = 0

# 13. 重拉全量
r = ws.api_refull({'uid': '1234567890'})
check('重拉全量入队', r['ok'] is True)
b = ws.blogger_rows()[0]
check('next_page 重置为1', b['next_page'] == 1 and b['state'] == 'queued')
ws.api_pause({'uid': '1234567890'})
check('重拉全量可暂停', ws.blogger_rows()[0]['state'] == 'paused')
r = ws.api_refull({'uid': '0'})
check('重拉全量不存在拒绝', r['ok'] is False)
# 博主主页（无 homepage 时按 uid 推导）
check('主页链接推导', ws.blogger_rows()[0]['homepage'] == 'https://weibo.com/u/1234567890')

# 14. 已删除筛选
ws.db("UPDATE posts SET deleted=0")
ws.db("UPDATE posts SET deleted=1 WHERE id='a2'")
check('已删除筛选1条', q({'deleted': '1'})['total'] == 1)
check('正常筛选0条', q({'deleted': '0'})['total'] == 0)
check('全部仍1条', q({'page': '1'})['total'] == 1)
ws.db("UPDATE posts SET deleted=0")

# 15. 批量更新的已删除识别
check('已删除错误识别', ws._is_deleted_error(ws.ApiError('该微博不存在')) is True)
check('普通网络错误不误标', ws._is_deleted_error(ws.ApiError('网络异常')) is False)

# 16. 全量拉取"已见清单"标记逻辑（拉到底时，未见到的才标记）
ws.db("INSERT INTO posts(id,uid,bid,text,created_ts,deleted) VALUES('fakepost','1234567890','fp','x',100,0)")
ws.db("INSERT OR IGNORE INTO pull_seen(uid,id) VALUES('1234567890','a2')")
ws.db("UPDATE posts SET deleted=1 WHERE uid='1234567890' AND id NOT IN "
      "(SELECT id FROM pull_seen WHERE uid='1234567890')")
check('未见到的标记已删除', ws.db("SELECT deleted FROM posts WHERE id='fakepost'").fetchone()['deleted'] == 1)
check('见到的保持正常', ws.db("SELECT deleted FROM posts WHERE id='a2'").fetchone()['deleted'] == 0)
ws.db("DELETE FROM posts WHERE id='fakepost'")
ws.db("DELETE FROM pull_seen")

# 17. 取消拉取
ws.enqueue('1234567890', 'full')
check('入队成功', ws.blogger_rows()[0]['state'] == 'queued')
r = ws.api_cancel({'uid': '1234567890'})
check('排队中取消→done', r['ok'] is True and ws.blogger_rows()[0]['state'] == 'done')
ws.db("UPDATE bloggers SET state='fulling', next_page=3 WHERE uid='1234567890'")
ws.api_cancel({'uid': '1234567890'})
check('运行中取消已标记', '1234567890' in ws.CANCEL)
ws.CANCEL.discard('1234567890')
ev = ws.STOP.setdefault('1234567890', ws.threading.Event())
ev.clear()
ws.db("UPDATE bloggers SET state='paused', next_page=NULL WHERE uid='1234567890'")

# 18. 语雀归档：目录链接格式校验
check('目录格式 账号/库/目录', ws.validate_yuque_dir('https://www.yuque.com/aaa/bbb/ddd') == ('aaa', 'bbb', 'ddd'))
check('目录格式 多层路径', ws.validate_yuque_dir('https://www.yuque.com/aaa/bbb/1/2/ddd') == ('aaa', 'bbb', '1/2/ddd'))
check('目录格式 仅知识库', ws.validate_yuque_dir('https://www.yuque.com/aaa/bbb') == ('aaa', 'bbb', ''))
check('目录格式 非法协议', ws.validate_yuque_dir('http://yuque.com/aaa/bbb') is None)
check('目录格式 缺知识库', ws.validate_yuque_dir('https://www.yuque.com/aaa') is None)
check('目录格式 中文目录名', ws.validate_yuque_dir('https://www.yuque.com/shanhesss/study/其他') == ('shanhesss', 'study', '其他'))
check('目录格式 空格拒收', ws.validate_yuque_dir('https://www.yuque.com/aaa/bb b') is None)

# 19. 博主语雀目录设置
r = ws.api_blogger_yuque_dir({'uid': '1234567890', 'dir': 'https://www.yuque.com/aaa/bbb/ddd'})
check('设置目录成功', r['ok'] is True)
r = ws.api_blogger_yuque_dir({'uid': '1234567890', 'dir': 'not-a-url'})
check('非法目录拒绝', r['ok'] is False)
check('blogger_rows 带 yuque_dir', ws.blogger_rows()[0]['yuque_dir'] == 'https://www.yuque.com/aaa/bbb/ddd')

# 20. 归档预检与入队
check('归档空ids拒绝', ws.api_yuque_sync({'ids': []})['ok'] is False)
ws.db("INSERT INTO posts(id,uid,bid,text,created_ts,media_json,retweeted_json,raw_json,fetched_at) "
      "VALUES('a4','1234567890','a4','普通微博',%d,'{}','','{}','2026-08-19 00:00:00')" % (t - 50))
ws.api_blogger_yuque_dir({'uid': '1234567890', 'dir': ''})
r = ws.api_yuque_sync({'ids': ['a4']})
check('未配目录报博主', r['ok'] is False and '博主' in r['error'])
ws.api_blogger_yuque_dir({'uid': '1234567890', 'dir': 'https://www.yuque.com/aaa/bbb/ddd'})
r = ws.api_yuque_sync({'ids': ['a2']})
check('转发微博不可归档', r['ok'] is False)
r = ws.api_yuque_sync({'ids': ['a4']})
check('单条归档入队', r['ok'] is True and r['queued'] == 1)
check('归档队列1条', len(ws.SYNC_QUEUE) == 1)
ws.SYNC_QUEUE.clear()
ws.SYNC['total'] = 0
ws.SYNC['done'] = 0
ws.SYNC['msg'] = ''

# 21. 归档筛选 + 状态字段
ws.db("UPDATE posts SET archived=1, yuque_doc_url='https://www.yuque.com/aaa/bbb/doc', arch_fail='', arch_skip=0, arch_state='' WHERE id='a4'")
check('已归档筛选1条', q({'arch': 'done'})['total'] == 1)
check('待归档筛选1条(a2待归档)', q({'arch': 'pending'})['total'] == 1)
r = q({'page': '1'})
it = {i['id']: i for i in r['items']}
check('返回归档字段', it['a4']['archived'] == 1 and it['a4']['yuque_doc_url'].endswith('/doc'))
check('state 带归档进度', ws.api_state()['yuque_total'] == 0)

# 22. 已归档可再同步（瞬态标记 updating + 入队）
r = ws.api_yuque_sync({'ids': ['a4']})
check('已归档可再同步入队', r['ok'] is True and r['updated'] == 1 and r['created'] == 0)
check('瞬态标记 updating', ws.db("SELECT arch_state FROM posts WHERE id='a4'").fetchone()['arch_state'] == 'updating')
ws.SYNC_QUEUE.clear()
ws.SYNC['total'] = 0
ws.SYNC['done'] = 0
ws.SYNC['msg'] = ''
ws.db("UPDATE posts SET arch_state='' WHERE id='a4'")

# 23. 失败状态筛选 + 原因返回
ws.db("UPDATE posts SET arch_fail='目录不存在' WHERE id='a4'")
ws.db("UPDATE posts SET arch_fail='超时', arch_skip=0, archived=0 WHERE id='a2'")
check('更新失败筛选1条', q({'arch': 'update_fail'})['total'] == 1)
check('同步失败筛选1条', q({'arch': 'sync_fail'})['total'] == 1)
r = q({'page': '1'})
it = {i['id']: i for i in r['items']}
check('返回失败原因', it['a4']['arch_fail'] == '目录不存在' and it['a2']['arch_fail'] == '超时')
ws.db("UPDATE posts SET arch_fail='' WHERE id='a2'")
ws.db("UPDATE posts SET arch_fail='', archived=1 WHERE id='a4'")

# 24. 批量改为无需归档 / 改回待归档
r = ws.api_yuque_mark({'ids': ['a2', 'a4'], 'to': 'skip'})
check('批量改为无需归档', r['ok'] is True and r['updated'] == 2)
check('无需归档筛选2条', q({'arch': 'skip'})['total'] == 2)
r = ws.api_yuque_mark({'ids': ['a2'], 'to': 'pending'})
check('改回待归档', r['ok'] is True)
check('待归档筛选1条', q({'arch': 'pending'})['total'] == 1)
check('无需归档剩1条', q({'arch': 'skip'})['total'] == 1)
r = ws.api_yuque_mark({'ids': [], 'to': 'skip'})
check('mark 空ids拒绝', r['ok'] is False)

print('---- RESULT: %s ----' % ('ALL PASS' if ok else 'HAS FAILURES'))
sys.exit(0 if ok else 1)
