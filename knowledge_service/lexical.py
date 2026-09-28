"""Chinese BM25 with real term frequencies; RRF joins independent rank lists."""
from collections import Counter, defaultdict
import math
import re

import jieba


STOP_WORDS = set("的 了 是 在 我 你 他 她 它 吗 呢 吧 啊 和 与 及 或 怎么 如何 什么 哪些 哪里 请 请问 帮 帮我 一下 这个 那个 可以 需要 有 没有 进行 相关 以及 为 对 将 还 就 都 很 想 要 能 该 西电 西安电子科技大学".split())
ALIASES = {"南校区": "长安校区", "北校区": "雁塔校区", "寝室": "宿舍", "连不上": "无法连接", "断网": "网络故障"}
for word in ["长安校区", "雁塔校区", "南校区", "北校区", "国家励志奖学金", "国家奖学金", "国家助学金", "补退选", "转专业", "校园网", "一网通办"]:
    jieba.add_word(word)


def tokens(text: str) -> list[str]:
    for original, canonical in ALIASES.items():
        text = text.replace(original, canonical)
    return [w for w in jieba.lcut(text.lower()) if w not in STOP_WORDS and re.search(r"[\w\u4e00-\u9fff]", w)]


class BM25:
    def __init__(self, rows: list[dict]):
        self.rows = {r["id"]: r for r in rows}
        self.postings = defaultdict(dict)
        self.lengths = {}
        for row in rows:
            terms = tokens(row["title"] + " " + row["title"] + " " + row["content"])
            self.lengths[row["id"]] = len(terms)
            for term, count in Counter(terms).items():
                self.postings[term][row["id"]] = count
        self.average_length = sum(self.lengths.values()) / max(1,len(rows))

    def search(self, query: str, limit=30, allowed=None):
        scores = defaultdict(float)
        n = len(self.rows)
        for term in set(tokens(query)):
            posting = self.postings.get(term, {})
            idf = math.log(1 + (n-len(posting)+0.5)/(len(posting)+0.5))
            for key, tf in posting.items():
                if allowed is not None and key not in allowed:
                    continue
                scores[key] += idf * tf * 2.2 / (tf+1.2*(0.25+0.75*self.lengths[key]/max(1,self.average_length)))
        return sorted(scores.items(), key=lambda item:item[1], reverse=True)[:limit]


def reciprocal_rank_fusion(rankings: list[list[tuple[str,float]]], limit=60):
    scores = defaultdict(float)
    for rows in rankings:
        for rank, (key, _) in enumerate(rows, 1):
            scores[key] += 1 / (60+rank)
    return sorted(scores.items(), key=lambda item:item[1], reverse=True)[:limit]
