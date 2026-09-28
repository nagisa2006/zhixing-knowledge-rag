"""Discover article/service URLs from an explicitly maintained official source list."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit
import requests
from lxml import html
from .ingest import official_url


def links(url):
    response = requests.get(url, timeout=(10,30))
    response.raise_for_status()
    tree = html.fromstring(response.content)
    found = []
    for anchor in tree.xpath('//a[@href]'):
        target = urljoin(response.url, anchor.get('href')).split('#')[0]
        title = ''.join(anchor.itertext()).strip()
        if not official_url(target):
            continue
        path = urlsplit(target).path
        if '/info/' in path or re.search(r'\.(pdf|docx|xlsx)$',path,re.I) or (
            title and any(t in title for t in ['指南','借阅','续借','开放时间','上网申请','远程访问','网接入','就诊时间'])
        ):
            found.append(target)
    return list(dict.fromkeys(found))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--sources',required=True)
    parser.add_argument('--output',required=True)
    args = parser.parse_args()
    sources = json.loads(Path(args.sources).read_text(encoding='utf8'))
    urls = list(sources['documents'])
    failures = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        tasks = [(url,pool.submit(links,url)) for url in sources['discoveryPages']]
        for url,task in tasks:
            try:
                urls.extend(task.result())
            except Exception as error:
                failures.append({'url':url,'error':str(error)})
    Path(args.output).write_text(json.dumps(list(dict.fromkeys(urls)),ensure_ascii=False,indent=2),encoding='utf8')
    Path(args.output+'.failures.json').write_text(json.dumps(failures,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({'urls':len(set(urls)),'failedSources':len(failures)}))


if __name__ == '__main__':
    main()
