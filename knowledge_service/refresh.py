"""Refresh configured official pages through the running knowledge service."""
import argparse
import json
import os
from pathlib import Path
import requests


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--urls',required=True);parser.add_argument('--report',required=True)
    args=parser.parse_args();report={'published':[],'failed':[]}
    for url in json.loads(Path(args.urls).read_text(encoding='utf8')):
        try:
            response=requests.post(os.environ['KNOWLEDGE_URL']+'/ingest',json={'url':url},
                headers={'Authorization':'Bearer '+os.environ['KNOWLEDGE_API_KEY']},timeout=180)
            response.raise_for_status();report['published'].append(response.json()['document']['id'])
        except Exception as error:
            report['failed'].append({'url':url,'error':str(error)})
    Path(args.report).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({k:len(v) for k,v in report.items()}))


if __name__=='__main__':
    main()
