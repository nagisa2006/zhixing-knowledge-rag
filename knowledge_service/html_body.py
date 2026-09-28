"""Xidian CMS body adapters and explicit (never guessed) publication dates."""
import json
import re
from urllib.parse import urljoin, urlsplit
from lxml import html
import trafilatura
from .attachments import ocr_images


def parse_html(data,url):
    tree=html.fromstring(trafilatura.utils.decode_file(data))
    tree.make_links_absolute(url)
    title_nodes=tree.xpath('//meta[@name="pageTitle"]/@content | //title/text()')
    title=next((str(x).strip() for x in title_nodes if str(x).strip()),'')
    if not title:
        raise ValueError('原文标题缺失')
    raw_text=' '.join(tree.xpath('//text()[not(ancestor::script) and not(ancestor::style)]'))
    metadata=tree.xpath('//meta[@property="article:published_time" or @name="PubDate" or @name="publishdate"]/@content')
    match=re.search(r'(?:发布时间|发布日期|发布时间：|时间|日期)\s*[:：]\s*(20\d{2})[-年./](\d{1,2})[-月./](\d{1,2})',raw_text)
    published=f'{int(match[1]):04d}-{int(match[2]):02d}-{int(match[3]):02d}' if match else None
    if metadata and re.match(r'20\d{2}-\d{2}-\d{2}',metadata[0]):
        published=metadata[0][:10]
    image_data=re.search(rb'var\s+vsb_pdf_image_data\s*=\s*(\[[^;]+\])',data)
    pages=None
    if image_data:
        urls=[urljoin(url,path) for path in json.loads(image_data[1])]
        from .ingest import official_url
        if not all(official_url(target) for target in urls):
            raise ValueError('图像正文来源不在官方范围内')
        pages=ocr_images(urls)
        text='\n\n'.join(body for _,body in pages)
    else:
        nodes=tree.xpath('//*[@id="vsb_content" or @id="vsb_content_2" or contains(concat(" ",normalize-space(@class)," ")," v_news_content ")]')
        # Network-center service pages put procedures/materials beside the CMS introduction.
        if urlsplit(url).hostname=='xxzx.xidian.edu.cn' and tree.xpath('//div[contains(concat(" ",normalize-space(@class)," ")," list01 ")]'):
            nodes=tree.xpath('//div[contains(concat(" ",normalize-space(@class)," ")," list01 ")]')
        if nodes:
            node=nodes[0]
            for unwanted in node.xpath('.//script|.//style'):
                unwanted.drop_tree()
            # CMS boundaries identify the article; keep its paragraphs, tables and actual links.
            for anchor in node.xpath('.//a[@href]'):
                target=anchor.get('href')
                if target.startswith(('http://','https://')):
                    anchor.text=(anchor.text or '')+' ('+target+')'
            for row in node.xpath('.//tr'):
                row.text=' | '.join(' '.join(cell.itertext()).strip() for cell in row.xpath('./th|./td'))
                for child in list(row):
                    row.remove(child)
                row.tail='\n'
            for element in node.xpath('.//p|.//div|.//br|.//li|.//h1|.//h2|.//h3'):
                element.tail='\n'+(element.tail or '')
            text=node.text_content().strip()
            image_urls=node.xpath('.//img/@src')
            if len(text)<80 and image_urls:
                from .ingest import official_url
                if not all(official_url(target) for target in image_urls):
                    raise ValueError('图像正文来源不在官方范围内')
                pages=ocr_images(list(dict.fromkeys(image_urls)))
                text='\n\n'.join(body for _,body in pages)
        else:
            for footer in tree.xpath('//footer|//*[contains(concat(" ",normalize-space(@class)," ")," part3_lxwm ")]'):
                footer.drop_tree()
            extracted=trafilatura.bare_extraction(html.tostring(tree),url=url,
                include_tables=True,include_links=True,include_comments=False,with_metadata=False,favor_precision=True)
            text=extracted.text.strip() if extracted and extracted.text else ''
    text='\n'.join(line.strip() for line in text.splitlines() if line.strip())
    if len(text)<80:
        raise ValueError('正文不足，需检查图片、附件或访问权限')
    return title,text,published,pages
