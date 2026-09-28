"""Local operations CLI: secrets stay in the private configuration file."""
import argparse
import json
from pathlib import Path
import requests


def main():
    p=argparse.ArgumentParser();p.add_argument('path');p.add_argument('--body');p.add_argument('--output')
    p.add_argument('--config',default='artifacts/ai/local/knowledge-env.json');p.add_argument('--timeout',type=int,default=30)
    args=p.parse_args();config=json.loads(Path(args.config).read_text(encoding='utf8'))
    base=config.get('KNOWLEDGE_URL') or 'http://'+config['KNOWLEDGE_HOST']+':'+config['KNOWLEDGE_PORT']
    body=json.loads(args.body) if args.body is not None else None
    response=requests.request('POST' if body is not None else 'GET',base+args.path,
        headers={'Authorization':'Bearer '+config['KNOWLEDGE_API_KEY']},json=body,timeout=args.timeout)
    response.raise_for_status()
    if args.output:
        Path(args.output).write_text(response.text,encoding='utf8')
    print(response.text)


if __name__=='__main__':
    main()
