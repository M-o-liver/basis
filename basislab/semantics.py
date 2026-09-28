"""Conservative event parsing. Mappings retain wording, assumptions and review status."""
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

SYMBOLS = {
 'BTC': r'bitcoin|btc', 'ETH': r'ethereum|ether|eth', 'SPY': r'SPY', 'QQQ': r'QQQ',
 'DIA': r'DIA', 'IWM': r'IWM', 'TSLA': r'tesla|TSLA', 'NVDA': r'nvidia|NVDA',
 'AAPL': r'apple|AAPL', 'AMZN': r'amazon|AMZN', 'MSFT': r'microsoft|MSFT',
 'META': r'meta|facebook', 'GOOGL': r'google|alphabet|GOOGL', 'COIN': r'coinbase|COIN',
 'MSTR': r'microstrategy|MSTR',
}
MONTHS = 'january february march april may june july august september october november december'.split()


def number(value):
    if value is None or isinstance(value, bool) or str(value).strip() == '':
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def probability(value):
    result = number(value)
    return result if result is not None and 0 <= result <= 1 else None


def timestamp(value):
    if value is None:
        return None
    n = number(value)
    if n is not None:
        return int(n * 1000 if abs(n) < 100_000_000_000 else n)
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if dt.tzinfo is None:
            return None  # Never silently guess a timezone.
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError, OverflowError):
        return None


def array(value):
    if isinstance(value, list):
        return value
    try:
        result = json.loads(value)
        return result if isinstance(result, list) else []
    except (TypeError, ValueError):
        return []


def underlying(text):
    for symbol, pattern in SYMBOLS.items():
        if re.search(r'\b(?:' + pattern + r')\b', text, re.I):
            return symbol
    return None


def threshold(text):
    if re.search(r'\bbetween\b|\bversus\b|\bvs\.?\b', text, re.I):
        return None
    candidates = list(re.finditer(r'\$\s*(\d[\d,]*(?:\.\d+)?)\s*([kKmM])?', text))
    if not candidates:
        candidates = list(re.finditer(r'\b(?:above|below|over|under|reach|hit|touch|to)\s+(\d[\d,]*(?:\.\d+)?)\s*([kKmM])?', text, re.I))
    if len(candidates) != 1:
        return None
    match = candidates[0]
    value = float(match[1].replace(',', '')) * {'k': 1000, 'm': 1e6}.get((match[2] or '').lower(), 1)
    return value if value > 0 else None


def infer_event(raw):
    text = str(raw.get('question') or raw.get('title') or '')
    description = str(raw.get('description') or '')
    asset = underlying(text)
    outcomes = [str(x).lower() for x in array(raw.get('outcomes'))]
    tokens = array(raw.get('clobTokenIds'))
    prices = array(raw.get('outcomePrices'))
    end = timestamp(raw.get('endDate') or raw.get('eventEndDate'))
    basic = dict(event_id=str(raw.get('id') or raw.get('conditionId') or ''), event_text=text,
                 description=description, asset=asset, expiry=end, event_type='unmapped', direction=None,
                 strike_or_threshold=None, settlement_source=raw.get('resolutionSource') or None,
                 threshold_inclusive=None, mapping_review='UNVERIFIED', mapping_origin='automatic',
                 event_url='https://polymarket.com/event/' + str(raw.get('eventSlug') or raw.get('slug') or ''),
                 yes_token=None, pm_indicative=None, volume_24h=number(raw.get('volume24hr')),
                 active=raw.get('active') is not False and raw.get('closed') is not True,
                 window_start=None, window_start_assumption=None)
    if outcomes == ['yes', 'no'] or outcomes == ['no', 'yes']:
        i = outcomes.index('yes')
        basic['yes_token'] = str(tokens[i]) if len(tokens) == 2 else None
        basic['pm_indicative'] = probability(prices[i]) if len(prices) == 2 else None
    excluded = re.search(r'market.?cap|revenue|earnings|deliveries|valuation|subscriber|production', text + ' ' + description, re.I)
    strike = threshold(text)
    if not asset or strike is None or not end or not basic['yes_token'] or excluded:
        basic['mapping_reason'] = 'No supported binary price proposition; retained in catalog'
        return basic
    terminal = bool(re.search(r'\bclose|\bsettle|\bfinish|end.of.(?:the.)?(?:day|month|week)', text, re.I))
    touch = bool(re.search(r'\btouch|\bdip|\bhit|\breach|\bcross|\bdrop to|\bfall to', text, re.I)) and not terminal
    down = bool(re.search(r'\bdip|\bbelow|\bunder|\bdrop|\bfall|\blower|\blow\b', text, re.I))
    if not touch and not re.search(r'\babove|\bbelow|\bover|\bunder|\bgreater than|\bless than', text, re.I):
        basic['mapping_reason'] = 'Direction ambiguous'
        return basic
    basic.update(event_type='touch' if touch else 'terminal', direction='down' if down else 'up', strike_or_threshold=strike)
    if touch:
        month = re.search(r'\b(?:in|during)\s+(' + '|'.join(MONTHS) + r')\b', text, re.I)
        if month:
            dt = datetime.fromtimestamp(end / 1000, timezone.utc)
            m = MONTHS.index(month[1].lower()) + 1
            explicit = re.search(r'\b(20\d{2})\b', text)
            year = int(explicit[1]) if explicit else dt.year - int(m > dt.month)
            basic['window_start'] = int(datetime(year, m, 1, tzinfo=timezone.utc).timestamp() * 1000)
            basic['window_start_assumption'] = 'Calendar month in UTC inferred from title; verify venue/timezone'
            if asset not in ('BTC','ETH'):
                basic['window_start']=int(datetime(year,m,1,tzinfo=ZoneInfo('America/New_York')).timestamp()*1000)
                basic['window_start_assumption']='US equity calendar month in New York; regular-session history proxy, verify rules'
    for venue in ('Binance', 'Coinbase', 'Deribit', 'Nasdaq', 'NYSE'):
        if venue.lower() in description.lower():
            basic['settlement_source'] = venue
            break
    basic['mapping_reason'] = 'Title-derived proposition; inspect resolution wording and settlement source'
    basic['mapping_hash'] = mapping_hash(basic)
    return basic


def mapping_hash(event):
    fields = ('event_id', 'asset', 'expiry', 'event_type', 'direction', 'strike_or_threshold', 'window_start',
              'settlement_source', 'threshold_inclusive', 'mapping_review', 'description')
    return hashlib.sha256(json.dumps({k: event.get(k) for k in fields}, sort_keys=True).encode()).hexdigest()[:20]


def validate_mapping(value):
    event = dict(value)
    if not event.get('event_id') or not event.get('asset') or event.get('event_type') not in ('terminal', 'touch'):
        raise ValueError('Mapping requires event_id, asset and terminal/touch type')
    strike = number(event.get('strike_or_threshold'))
    if event.get('direction') not in ('up', 'down') or strike is None or strike <= 0:
        raise ValueError('Mapping requires up/down direction and a positive threshold')
    expiry = timestamp(event.get('expiry'))
    if expiry is None or expiry <= 0 or not event.get('yes_token'):
        raise ValueError('Mapping requires a UTC expiry timestamp and YES token')
    if event.get('mapping_review') not in ('UNVERIFIED', 'PROXY', 'VERIFIED', 'MISMATCH'):
        raise ValueError('Invalid mapping review state')
    start = timestamp(event.get('window_start')) if event.get('window_start') is not None else None
    if event.get('window_start') is not None and (start is None or start >= expiry):
        raise ValueError('Path window must have an explicit UTC timestamp before expiry')
    inclusive = event.get('threshold_inclusive')
    if inclusive is not None and not isinstance(inclusive, bool):
        raise ValueError('threshold_inclusive must be true, false or null')
    event.update(strike_or_threshold=strike, expiry=expiry, window_start=start, asset=str(event['asset']).strip().upper())
    event['mapping_origin'] = 'manual'
    event['mapping_hash'] = mapping_hash(event)
    return event
