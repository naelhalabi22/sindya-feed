#!/usr/bin/env python3
"""Convert Sindya Cashcow Google Merchant RSS/XML to OpenAI stable-schema Ads CSV.

Usage:
    python sindya_feed_converter.py --input GoogleMerchantFeed.txt --output sindya_products.csv
    python sindya_feed_converter.py --url https://www.sindya.co.il/crowlers/GoogleMerchantFeed --output sindya_products.csv

Uses the original store's item IDs and source fields; does not invent missing data.
Requires Python 3.10+; no third-party dependencies.
"""
import argparse
import csv
from decimal import Decimal, InvalidOperation
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

SOURCE_URL = 'https://www.sindya.co.il/crowlers/GoogleMerchantFeed'
NS = '{http://base.google.com/ns/1.0}'
HEADERS = [
    'item_id','title','description','url','brand','image_url','price',
    'availability','seller_name','target_countries','is_eligible_search',
    'is_eligible_checkout','is_ads_eligible','mpn','condition',
    'product_category','sale_price','additional_image_urls','seller_url',
]
REQUIRED = ('item_id','title','description','url','brand','image_url','price',
            'availability','seller_name','target_countries')
VALID_AVAILABILITY = {'in_stock','out_of_stock','pre_order','backorder','unknown'}
MAX_SOURCE_BYTES = 100_000_000

class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
    def handle_data(self, data):
        self.parts.append(data)
    def handle_starttag(self, tag, attrs):
        if tag in ('p','br','li','div'):
            self.parts.append(' ')

def plain_text(value):
    parser = TextExtractor()
    safe = re.sub(r'&(?!#(?:[0-9]+|[xX][0-9a-fA-F]+);|[A-Za-z][A-Za-z0-9]+;)', '&amp;', value or '')
    parser.feed(safe)
    return re.sub(r'\s+', ' ', unescape(''.join(parser.parts))).strip()

def text(node, name):
    el = node.find(NS + name if name.startswith('g:') else name[2:] if name.startswith('./') else name)
    return '' if el is None or el.text is None else el.text.strip()

def gtext(node, name):
    el = node.find(NS + name)
    return '' if el is None or el.text is None else el.text.strip()

def http_url(value):
    try:
        parts = urlsplit(value)
        return parts.scheme in ('http','https') and bool(parts.netloc) and not(parts.username or parts.password)
    except ValueError:
        return False

def money(raw):
    pieces = raw.strip().split()
    if len(pieces)!=2 or not re.fullmatch('[A-Z]{3}', pieces[1]):
        raise ValueError('invalid amount/currency')
    try:
        amt = Decimal(pieces[0].replace(',',''))
    except InvalidOperation as exc:
        raise ValueError('invalid amount') from exc
    if not amt.is_finite() or amt <= 0:
        raise ValueError('price must be positive')
    return amt, pieces[1], f'{amt:.2f} {pieces[1]}'

def convert(source, output, rejected):
    from collections import Counter
    reasons=Counter()
    total=accepted=0
    ids=set()
    with open(output,'w',encoding='utf-8',newline='') as out, open(rejected,'w',encoding='utf-8',newline='') as rej:
        writer=csv.DictWriter(out,fieldnames=HEADERS)
        writer.writeheader()
        bad=csv.writer(rej)
        bad.writerow(['item_id','reason'])
        for _, elem in ET.iterparse(source, events=('end',)):
            if elem.tag != 'item':
                continue
            total += 1
            item_id=gtext(elem,'id')
            row={
                'item_id':item_id,
                'title':re.sub(r'\s+', ' ', unescape(text(elem,'title'))).strip()[:150],
                'description':plain_text(text(elem,'description'))[:5000],
                'url':text(elem,'link'),
                # This is the brand VALUE Cashcow emits. Verify genuine product brands in Cashcow.
                'brand':gtext(elem,'brand')[:70],
                'image_url':gtext(elem,'image_link'),
                'availability':gtext(elem,'availability').lower().replace(' ','_'),
                'seller_name':'סינדיה',
                'target_countries':'IL',
                'is_eligible_search':'false',
                'is_eligible_checkout':'false',
                'is_ads_eligible':'true',
                'mpn':gtext(elem,'mpn')[:70],
                'condition':gtext(elem,'condition'),
                'product_category':gtext(elem,'product_type').replace(';',' > '),
                'seller_url':'https://www.sindya.co.il',
            }
            images=[]
            for image in elem.findall(NS+'additional_image_link'):
                url=(image.text or '').strip()
                if url and http_url(url):
                    images.append(url)
            row['additional_image_urls']=','.join(dict.fromkeys(images))
            try:
                amount, currency, formatted=money(gtext(elem,'price'))
                row['price']=formatted
                sale=gtext(elem,'sale_price')
                if sale:
                    sale_amount, sale_currency, sale_formatted=money(sale)
                    if sale_currency==currency and sale_amount<amount:
                        row['sale_price']=sale_formatted
                    else:
                        reasons['invalid_sale_price_dropped']+=1
            except ValueError:
                row['price']=''
            if not row['availability'] in VALID_AVAILABILITY:
                row['availability']=''
            missing=[k for k in REQUIRED if not row.get(k)]
            if item_id in ids:
                missing.append('duplicate_item_id')
            if row['url'] and not http_url(row['url']):
                missing.append('invalid_product_url')
            if row['image_url'] and not http_url(row['image_url']):
                missing.append('invalid_image_url')
            if missing:
                for issue in missing:
                    reasons[issue]+=1
                bad.writerow([item_id,'; '.join(missing)])
            else:
                writer.writerow({col:row.get(col,'') for col in HEADERS})
                ids.add(item_id)
                accepted+=1
            elem.clear()
    print(f'total={total} accepted={accepted} rejected={total-accepted}')
    for reason, count in reasons.most_common():
        print(f'{reason}={count}')
    return accepted

def download(url, path):
    if not http_url(url) or not url.lower().startswith('https://'):
        raise ValueError('Source URL must use HTTPS and contain no credentials')
    req=Request(url,headers={'User-Agent':'Mozilla/5.0','Accept':'application/xml, text/xml, */*'})
    with urlopen(req,timeout=90) as response, open(path,'wb') as out:
        size=0
        while True:
            chunk=response.read(1024*1024)
            if not chunk:break
            size+=len(chunk)
            if size>MAX_SOURCE_BYTES:
                raise ValueError('Source XML exceeds 100 MB safety limit')
            out.write(chunk)
    if not Path(path).stat().st_size:
        raise ValueError('Source returned an empty response')

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    group=ap.add_mutually_exclusive_group(required=True)
    group.add_argument('--input',help='Local Cashcow XML/RSS file (extension may be .txt)')
    group.add_argument('--url',help='Public HTTPS Cashcow XML/RSS feed',default=None)
    ap.add_argument('--output',required=True,help='Destination OpenAI CSV')
    ap.add_argument('--rejected',help='CSV audit of rejected items (default next to output)')
    args=ap.parse_args()
    target=Path(args.output)
    target.parent.mkdir(parents=True,exist_ok=True)
    rejected=Path(args.rejected) if args.rejected else target.with_name(target.stem+'_rejected.csv')
    if args.input:
        convert(args.input,target,rejected)
    else:
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'source.xml'
            download(args.url,path)
            convert(path,target,rejected)

if __name__=='__main__':
    try:
        main()
    except (ET.ParseError,ValueError, OSError) as error:
        print(f'ERROR: {error}',file=sys.stderr)
        sys.exit(1)
