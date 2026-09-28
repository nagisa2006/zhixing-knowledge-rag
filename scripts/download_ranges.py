"""Resume large release artifacts using verified HTTP byte ranges."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import requests


def main():
    p = argparse.ArgumentParser()
    p.add_argument('url'); p.add_argument('output'); p.add_argument('--sha256')
    args = p.parse_args()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    response = requests.get(args.url, headers={'Range': 'bytes=0-0'}, timeout=60)
    response.raise_for_status()
    size = int(response.headers['Content-Range'].split('/')[-1])
    block = 16 * 1024 * 1024
    parts = output.with_suffix(output.suffix + '.parts')
    parts.mkdir(exist_ok=True)

    def fetch(start):
        end = min(size, start + block) - 1
        target = parts / str(start)
        if target.exists() and target.stat().st_size == end-start+1:
            return
        r = requests.get(args.url, headers={'Range': f'bytes={start}-{end}'}, timeout=(20, 120))
        r.raise_for_status()
        if r.status_code != 206 or r.headers.get('Content-Range') != f'bytes {start}-{end}/{size}' or len(r.content) != end-start+1:
            raise ValueError('Download range mismatch')
        target.write_bytes(r.content)
        print(f'{output.name}: {start+len(r.content)}/{size}', flush=True)

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(fetch, range(0, size, block)))
    digest = hashlib.sha256()
    with output.open('wb') as stream:
        for start in range(0, size, block):
            data = (parts / str(start)).read_bytes()
            digest.update(data); stream.write(data)
    actual = digest.hexdigest()
    if args.sha256 and actual != args.sha256:
        raise ValueError(f'SHA256 mismatch: {actual}')
    print(f'COMPLETE {output.name} sha256={actual}', flush=True)


if __name__ == '__main__':
    main()
