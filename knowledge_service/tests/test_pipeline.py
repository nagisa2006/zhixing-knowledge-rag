import hashlib
from pathlib import Path
import sys
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from knowledge_service.html_body import parse_html
from knowledge_service.ingest import make_document
from knowledge_service.store import KnowledgeStore


def test_cms_body_preserves_table_and_real_date_without_footer():
    body='请申请人核对下列材料与适用范围。'*8
    page=f'<html><title>2024级学生2026年申请通知</title><span>发布时间：2026年09月04日</span><div id="vsb_content"><p>{body}</p><table><tr><td>对象</td><td>截止</td></tr><tr><td>本科生</td><td>9月18日17:00</td></tr></table><a href="/apply">申请入口</a></div><footer>页脚联系电话999</footer></html>'
    _,text,date,_=parse_html(page.encode(),'https://jwc.xidian.edu.cn/info/test.htm')
    assert date=='2026-09-04'
    assert '本科生 | 9月18日17:00' in text
    assert 'https://jwc.xidian.edu.cn/apply' in text
    assert '999' not in text


def test_missing_date_is_not_guessed_from_title():
    _,_,date,_=parse_html(('<title>2024级申请通知</title><div id="vsb_content">'+ '申请材料须完整提交。'*15+'</div>').encode(),'https://xidian.edu.cn/2024/info.htm')
    assert date is None


def test_empty_article_does_not_publish_navigation():
    with pytest.raises(ValueError,match='正文不足'):
        parse_html(('<title>研究生申请通知</title><div id="vsb_content"></div><footer>'+'学校联系电话 导航链接 '*20+'</footer>').encode(),'https://gr.xidian.edu.cn/info/test.htm')


def test_revisions_and_disabling_remove_old_evidence(tmp_path):
    store=KnowledgeStore(str(tmp_path/'knowledge.db'))
    doc,chunks=make_document('办理说明','申请表和成绩单。'*100,'https://xidian.edu.cn/notice.htm')
    store.put_document(doc,chunks)
    old_ids=[c['id'] for c in chunks]
    updated,new_chunks=make_document('办理说明','只需申请表。'*100,doc['sourceUrl'])
    store.put_document(updated,new_chunks)
    assert store.get_chunks(old_ids)==[]
    assert store.get_chunks([new_chunks[0]['id']])
    store.set_status(doc['id'],'disabled')
    assert store.get_chunks([new_chunks[0]['id']])==[]
    assert store.neighbors(doc['id'],0)==[]


def test_policy_replacement_archives_previous_in_same_transaction(tmp_path):
    store=KnowledgeStore(str(tmp_path/'knowledge.db'))
    old,old_chunks=make_document('旧通知','9月10日前申请。','https://xidian.edu.cn/old.htm')
    store.put_document(old,old_chunks)
    new,new_chunks=make_document('新通知','9月20日前申请，本通知替代旧通知。','https://xidian.edu.cn/new.htm')
    new['supersedesId']=old['id'];store.put_document(new,new_chunks)
    assert store.get_document(old['id'])['status']=='archived'
    assert store.get_chunks([old_chunks[0]['id']])==[]
    assert store.get_chunks([new_chunks[0]['id']])


# --- 低算力主机：重排输入量限制（方案 §4.1）--------------------------------
#
# 这几条守的是一个出错时不会报错、只会悄悄降低召回质量的性质：切片必须在
# 可见性与适用范围过滤之后，且必须保持 RRF 顺序。

def _row(chunk_id, content_hash, **extra):
    return {"id": chunk_id, "contentHash": content_hash, **extra}


def test_narrowing_cuts_after_filters_not_before():
    """排在前面但已失效的资料不得占用重排名额。"""
    from knowledge_service.engine import narrow_candidates

    rows = [
        _row("expired", "h1", effectiveTo="2020-01-01"),   # RRF 第 1，但已过期
        _row("northonly", "h2", campuses=["北校区"]),        # RRF 第 2，但校区不符
        _row("good-a", "h3"),
        _row("good-b", "h4"),
    ]
    filtered, sliced = narrow_candidates(rows, today="2026-09-10", campus="南校区", limit=2)

    assert [r["id"] for r in filtered] == ["good-a", "good-b"]
    # 若切片发生在过滤之前，这里会是 expired/northonly，重排拿到两条无效资料
    assert [r["id"] for r in sliced] == ["good-a", "good-b"]


def test_narrowing_preserves_rrf_order():
    from knowledge_service.engine import narrow_candidates

    rows = [_row(f"c{i}", f"h{i}") for i in range(20)]
    _, sliced = narrow_candidates(rows, today="2026-09-10", limit=12)
    assert [r["id"] for r in sliced] == [f"c{i}" for i in range(12)]


def test_narrowing_dedups_identical_content_keeping_the_better_rank():
    from knowledge_service.engine import narrow_candidates

    rows = [_row("first", "same"), _row("duplicate", "same"), _row("other", "unique")]
    filtered, _ = narrow_candidates(rows, today="2026-09-10", limit=12)
    assert [r["id"] for r in filtered] == ["first", "other"]


def test_narrowing_respects_education_level_and_effective_from():
    from knowledge_service.engine import narrow_candidates

    rows = [
        _row("future", "h1", effectiveFrom="2099-01-01"),
        _row("graduate", "h2", educationLevels=["研究生"]),
        _row("applies", "h3", educationLevels=["本科生"], effectiveFrom="2026-01-01"),
    ]
    filtered, _ = narrow_candidates(rows, today="2026-09-10", level="本科生", limit=12)
    assert [r["id"] for r in filtered] == ["applies"]


def test_get_chunks_returns_rows_in_requested_order(tmp_path):
    """narrow_candidates 依赖这个顺序保证，破了就会取错前 N 条。"""
    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    doc, chunks = make_document("排序说明", "第一段内容与办理材料说明。" * 200, "https://xidian.edu.cn/order.htm")
    store.put_document(doc, chunks)
    ids = [c["id"] for c in chunks]
    assert len(ids) >= 2, "需要至少两个片段才能验证顺序"

    reversed_ids = list(reversed(ids))
    assert [r["id"] for r in store.get_chunks(reversed_ids)] == reversed_ids
