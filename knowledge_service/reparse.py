"""Rebuild canonical HTML documents from captured raw originals after parser changes."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
from .html_body import parse_html
from .ingest import make_document
from .store import KnowledgeStore, raw_object_path


def main():
    p=argparse.ArgumentParser();p.add_argument('--db',required=True);p.add_argument('--report',required=True)
    p.add_argument('--workers',type=int,default=2)
    p.add_argument('--failed-report');args=p.parse_args()
    store=KnowledgeStore(args.db)
    docs=[d for d in store.documents() if d.get('rawObjectKey') and not d['sourceUrl'].split('?')[0].lower().endswith(('.pdf','.docx','.xlsx'))]
    if args.failed_report:
        ids={row['id'] for row in json.loads(Path(args.failed_report).read_text(encoding='utf8'))['failed']}
        docs=[d for d in docs if d['id'] in ids]
    def parse(old):
        print(json.dumps({'parsing':old['id'],'title':old['title']},ensure_ascii=False),flush=True)
        # A missing capture must name itself: the failure path below disables the
        # document, so a wrong KNOWLEDGE_RAW_DIR would otherwise retire the
        # corpus one entry at a time with no indication of why.
        raw=raw_object_path(old['rawObjectKey'])
        if raw is None:
            raise FileNotFoundError(f"原始资料不在 KNOWLEDGE_RAW_DIR 中：{old['rawObjectKey']}")
        title,text,date,pages=parse_html(raw.read_bytes(),old['sourceUrl'])
        doc,chunks=make_document(title,text,old['sourceUrl'],published_at=date,pages=pages,
            doc_id=old['id'],source_id=old['sourceId'],authority=old['authority'])
        doc['rawObjectKey']=old['rawObjectKey'];doc['parserVersion']='xiaodian-body-2';doc['chunkCount']=len(chunks)
        store.put_document(doc,chunks)
        print(json.dumps({'parsed':doc['id'],'chunks':len(chunks)},ensure_ascii=False),flush=True)
        return {'id':doc['id'],'title':title,'chunks':len(chunks),'date':date}
    report={'published':[],'failed':[]}
    with ThreadPoolExecutor(args.workers) as pool:
        tasks={pool.submit(parse,doc):doc for doc in docs}
        for task in as_completed(tasks):
            doc=tasks[task]
            try:
                report['published'].append(task.result())
            except Exception as error:
                store.set_status(doc['id'],'disabled')
                report['failed'].append({'id':doc['id'],'url':doc['sourceUrl'],'error':str(error)})
            done=sum(map(len,report.values()))
            if done%25==0:
                print(json.dumps({'done':done,**{key:len(value) for key,value in report.items()}}),flush=True)
    Path(args.report).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({key:len(value) for key,value in report.items()}))


if __name__=='__main__':
    main()
