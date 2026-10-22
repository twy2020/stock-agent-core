"""Independent public-price previews; never credentialed and never auto-applied.

Parser provenance: adapted from the local OCR project's src/model_pricing.py
(2026-10-04). Core owns this copy and has no import/runtime dependency on OCR.
Official page formats are undocumented and may change; unavailable/stale is
explicit. Fetch time is not an asserted price effective date.
"""
import copy
import json
import re
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx
from .providers import resolve_key


SOURCES = {'siliconflow': 'https://www.siliconflow.cn/pricing',
           'deepseek': 'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/'}
HOSTS = {'api.siliconflow.cn': 'siliconflow', 'api.deepseek.com': 'deepseek'}
PAGE_LIMIT = 3_000_000
_CACHE = {}
_LOCKS = {name: threading.Lock() for name in SOURCES}


def provider_for(base_url):
    try:
        return HOSTS.get(urlsplit(base_url).hostname)
    except (ValueError, TypeError):
        return None


def _amount(value, factor=1):
    if len(str(value)) > 80:
        raise ValueError('Invalid price')
    number = Decimal(str(value))
    if not number.is_finite() or (number and not -100 <= number.adjusted() <= 100):
        raise ValueError('Invalid price')
    number *= factor
    if not number.is_finite() or number < 0 or number > 1000000000 or (number and number.adjusted() < -12):
        raise ValueError('Invalid price')
    return format(number.normalize(), 'f')


def parse_siliconflow(html):
    chunks = []
    for match in re.finditer(r'self\.__next_f\.push\((\[.*?\])\)</script>', html, re.S):
        value = json.loads(match[1])
        if isinstance(value, list) and len(value) > 1 and isinstance(value[1], str):
            chunks.append(value[1])
    stream = ''.join(chunks)
    if '"pricingApiItems":' not in stream:
        raise ValueError('Pricing payload missing')
    tail = stream.split('"pricingApiItems":', 1)[1].lstrip()
    items = json.JSONDecoder().raw_decode(tail)[0]
    if not isinstance(items, list) or len(items) > 20000:
        raise ValueError('Unsupported pricing payload')
    kinds = {'input-tokens': 'input', 'output-tokens': 'output', 'cached-input-tokens': 'cached_input'}
    result = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        name, raw, unit = item.get('playgroundName'), item.get('realTimePriceCnyUnit'), item.get('unitZhCnName')
        if not isinstance(name, str) or not 1 <= len(name) <= 200 or raw is None or not isinstance(unit, str) or not 1 <= len(unit) <= 100:
            continue
        try:
            amount = _amount(raw, 1000 if unit == 'K tokens' else 1)
        except (ValueError, InvalidOperation):
            continue
        # Preserve every tier/time/scenario condition as published. A preview
        # with multiple rows must never be flattened to a single base rate.
        condition = str(item.get('coordinateDesc') or '')
        scenario = str(item.get('price_scenario') or '')
        if len(condition) > 4000 or len(scenario) > 1000:
            raise ValueError('Unsupported pricing condition')
        row = {'kind': kinds.get(item.get('componentCode'), str(item.get('componentZhCnName') or 'other')[:100]),
               'amount': amount, 'unit': 'million_tokens' if unit in ('K tokens', 'M tokens', '百万 tokens') else unit,
               'condition': condition, 'scenario': scenario}
        rows = result.setdefault(name, [])
        if row not in rows:
            rows.append(row)
        if len(rows) > 200:
            raise ValueError('Too many pricing conditions')
    return result


class _Tables(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables, self.rows, self.row, self.cell = [], None, None, None

    def handle_starttag(self, tag, attrs):
        if tag == 'table':
            self.rows = []
        elif tag == 'tr' and self.rows is not None:
            self.row = []
        elif tag in ('td', 'th') and self.row is not None:
            self.cell = [dict(attrs), '']
        elif tag == 'br' and self.cell is not None:
            self.cell[1] += ' '

    def handle_data(self, data):
        if self.cell is not None:
            self.cell[1] += data

    def handle_endtag(self, tag):
        if tag in ('td', 'th') and self.cell is not None:
            self.row.append(self.cell); self.cell = None
        elif tag == 'tr' and self.row is not None:
            self.rows.append(self.row); self.row = None
            if len(self.rows) > 500:
                raise ValueError('Too many pricing rows')
        elif tag == 'table' and self.rows is not None:
            self.tables.append(self.rows); self.rows = None
            if len(self.tables) > 100:
                raise ValueError('Too many pricing tables')


def parse_deepseek(html):
    parser = _Tables(); parser.feed(html); result = {}
    for table in parser.tables:
        grid = {}
        for row_number, cells in enumerate(table):
            column = 0
            for attrs, text in cells:
                while (row_number, column) in grid:
                    column += 1
                width, height = int(attrs.get('colspan', 1)), int(attrs.get('rowspan', 1))
                if not 1 <= width <= 50 or not 1 <= height <= 100 or column + width > 100:
                    raise ValueError('Unsupported pricing table')
                for y in range(row_number, row_number + height):
                    for x in range(column, column + width):
                        grid[y, x] = text.strip()
                column += width
        models = {column: re.search(r'deepseek-[\w-]+', text)[0] for (row, column), text in grid.items()
                  if row == 0 and re.search(r'deepseek-[\w-]+', text)}
        for row in range(1, len(table)):
            labels = ' '.join(dict.fromkeys(grid.get((row, column), '') for column in range(min(models, default=0)))).strip()
            if '百万' not in labels or 'token' not in labels.lower():
                continue
            kind = 'cached_input' if '缓存命中' in labels else 'input' if '输入' in labels else 'output' if '输出' in labels else None
            if not kind:
                continue
            for column, name in models.items():
                match = re.fullmatch(r'\s*([\d.]+)\s*元\s*', grid.get((row, column), ''))
                if match:
                    result.setdefault(name, []).append({'kind': kind, 'amount': _amount(match[1]), 'unit': 'million_tokens',
                                                       'condition': labels, 'scenario': ''})
    return result


def _fetch(url):
    approved_host = urlsplit(url).hostname
    for _ in range(3):
        with httpx.stream('GET', url, headers={'User-Agent': 'Agent-Core-Public-Pricing/1.0', 'Accept': 'text/html'},
                          timeout=httpx.Timeout(15, connect=5), follow_redirects=False, trust_env=False) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                target = urljoin(url, response.headers.get('location', ''))
                parsed = urlsplit(target)
                if not response.headers.get('location') or parsed.scheme != 'https' or parsed.hostname != approved_host or parsed.username or parsed.password or parsed.port not in (None, 443):
                    raise ValueError('Unsupported pricing redirect')
                url = target; continue
            if response.status_code != 200:
                raise ValueError('Pricing source unavailable')
            chunks = []; size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > PAGE_LIMIT:
                    raise ValueError('Pricing page too large')
                chunks.append(chunk)
            return b''.join(chunks).decode('utf-8')
    raise ValueError('Too many pricing redirects')


def catalog(base_url):
    provider = provider_for(base_url)
    if not provider:
        return {'status': 'unsupported', 'models': {}, 'message': 'No official public pricing adapter is configured for this provider.'}
    with _LOCKS[provider]:
        cached = _CACHE.get(provider)
        if cached and time.monotonic() < cached[0]:
            return copy.deepcopy(cached[1])
        try:
            html = _fetch(SOURCES[provider])
            models = (parse_siliconflow if provider == 'siliconflow' else parse_deepseek)(html)
            if not models:
                raise ValueError('No supported prices')
            result = {'status': 'available', 'provider': provider, 'currency': 'CNY', 'sourceUrl': SOURCES[provider],
                      'fetchedAt': datetime.now(timezone.utc).isoformat(), 'effectiveDate': None, 'models': models,
                      'message': 'Official reference prices. Preserve tiers, time windows and scenarios; provider bills are authoritative. Applying any rate requires an explicit configuration edit.'}
            ttl = 3600
        except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError, InvalidOperation, UnicodeError):
            previous = cached[1] if cached else {}
            result = {**previous, 'status': 'stale' if previous.get('models') else 'unavailable', 'provider': provider,
                      'sourceUrl': SOURCES[provider], 'effectiveDate': None, 'models': previous.get('models', {}),
                      'error': 'PUBLIC_PRICING_UNAVAILABLE', 'message': 'Official pricing refresh failed; any retained prices are stale and must be verified at the source.'}
            ttl = 60
        _CACHE[provider] = (time.monotonic() + ttl, result)
        return copy.deepcopy(result)


def discover_pricing(config, key_id):
    profile = resolve_key(config, key_id)
    result = catalog(profile['baseUrl'])
    return {**result, 'keyId': key_id,
            'profiles': [{'id': item['id'], 'model': item['model'], 'prices': result['models'].get(item['model'])}
                         for item in config.get('models', []) if item.get('keyId') == key_id]}
