"""Single deterministic state reducer shared by live collection and raw-tape replay."""
from collections import defaultdict, deque
from dataclasses import asdict
import hashlib
import json
import logging
import sqlite3
from pathlib import Path
import threading
import time
import uuid
from . import CALCULATION_VERSION, FEATURE_VERSION
from .config import Config
from .features import Dynamics, Episodes, valid
from .pricing import derive, discrepancy
from .semantics import array, infer_event, mapping_hash, number, probability, timestamp, validate_mapping
from .store import encode


def code_hash():
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob('*.py')):
        digest.update(path.name.encode()); digest.update(path.read_bytes())
    return digest.hexdigest()[:20]


class Engine:
    def __init__(self, store, config=None, persist=True):
        self.store, self.config, self.persist = store, config or Config(), persist
        self.session = uuid.uuid4().hex
        self.code_hash = code_hash()
        self.lock = threading.RLock()
        self.catalog, self.events, self.manual, self.books, self.spots = {}, {}, {}, {}, {}
        self.surfaces, self.histories, self.latest = {}, {}, {}
        self.health = {}
        self.dynamics, self.episodes = Dynamics(self.config), Episodes(self.config)
        self.last_raw_id = 0
        self.last_received_ms = 0
        self.delayed_packets = 0
        self.clock_errors = 0
        self.errors = deque(maxlen=100)
        self.observation_count = 0
        self.started_ms = time.time_ns() // 1_000_000
        self.restoring = False
        self.persistence_error = None

    def start_session(self):
        if self.store.path != ':memory:':
            destination = Path(self.store.path).parent / 'versions' / self.code_hash
            destination.mkdir(parents=True, exist_ok=True)
            for path in Path(__file__).parent.glob('*.py'):
                target = destination / path.name
                if not target.exists():
                    target.write_bytes(path.read_bytes())
        extra={}
        if getattr(self,'restore_migration',None):
            extra['checkpoint_migration']=self.restore_migration
        if getattr(self,'restore_migration',None)=='equity-context-1':
            corrected=[];cache={}
            for event in self.catalog.values():
                if event.get('asset') in (None,'BTC','ETH'):continue
                raw_id=event.get('catalog_raw_id')
                if raw_id and raw_id not in cache:
                    raw=self.store.raw_record(raw_id)
                    cache[raw_id]={str(m.get('id')):m for m in raw['payload'].get('markets',[])} if raw else {}
                original=cache.get(raw_id,{}).get(event['event_id'])
                if original:
                    fixed=infer_event(original)
                    fixed.update({k:event[k] for k in ('catalog_raw_id','catalog_received_ms') if k in event});corrected.append(fixed)
            extra=dict(checkpoint_migration=self.restore_migration,equity_catalog=corrected)
        self.ingest('basis', 'session', self.session, dict(config=asdict(self.config), parameter_hash=self.config.hash,
            code_hash=self.code_hash, calculation_version=CALCULATION_VERSION, feature_version=FEATURE_VERSION,**extra))

    def ingest(self, source, kind, subject, payload, received_ms=None, source_ms=None, monotonic_ns=None):
        with self.lock:
            if self.persistence_error:
                raise RuntimeError('Recorder persistence failed; restart from committed tape: '+self.persistence_error)
            try:
                with self.store.transaction():
                    record = self.store.append_raw(source, kind, subject, payload, self.session, received_ms, source_ms, monotonic_ns)
                    self.apply(record)
                    return record['id']
            except sqlite3.Error as error:
                self.persistence_error = str(error)
                raise

    def newer(self, previous, source_ms, received_ms):
        if source_ms is not None and source_ms > received_ms + 5000:
            self.clock_errors += 1
            return False
        if previous and source_ms is not None and previous.get('source_ms') is not None and source_ms < previous['source_ms']:
            self.delayed_packets += 1
            return False
        return True

    def apply(self, record):
        # Live and replay quarantine exactly the same malformed source inputs.
        try:
            self._apply(record)
        except (ValueError, TypeError, KeyError, OverflowError, IndexError, AttributeError) as error:
            self.errors.append(dict(raw_id=record['id'], error=str(error), kind=record['kind']))
            if self.persist:
                logging.exception('BASIS reducer quarantined raw_id=%s kind=%s', record['id'], record['kind'])

    def _apply(self, record):
        self.last_raw_id = record['id']
        # Clock regressions remain in raw; observation availability never travels backward.
        now = max(record['received_ms'], self.last_received_ms)
        self.last_received_ms = now
        kind, payload, subject = record['kind'], record['payload'], record['subject']
        affected = set()
        if kind == 'session':
            # Each session's thresholds and versions are part of replay, not today's settings.
            self.config = Config(**payload['config'])
            self.dynamics.config = self.episodes.config = self.config
            if payload.get('checkpoint_migration')=='equity-context-1':
                for event in payload.get('equity_catalog',[]):self.catalog[event['event_id']]=event
                equity_ids={k for k,e in self.catalog.items() if e.get('asset') not in (None,'BTC','ETH')}
                self.latest={k:v for k,v in self.latest.items() if k not in equity_ids}
                self.spots={k:v for k,v in self.spots.items() if k in ('BTC','ETH')}
                self.surfaces={k:v for k,v in self.surfaces.items() if k[0] in ('BTC','ETH')}
                self.histories={k:v for k,v in self.histories.items() if k[0] in ('BTC','ETH')}
                for collection in (self.dynamics.history,self.dynamics.previous,self.dynamics.salient):
                    for key in list(collection):
                        if key[0] in equity_ids:del collection[key]
                self.select_events(now)
            return
        if kind == 'health':
            previous = self.health.get(subject, {})
            self.health[subject] = dict(payload, timestamp=now, raw_id=record['id'], last_success_ms=now if payload.get('state') == 'OK' else previous.get('last_success_ms'))
            return
        if kind == 'catalog':
            for raw in payload.get('markets', []):
                event = infer_event(raw)
                if not event['event_id']:
                    continue
                event.update(catalog_raw_id=record['id'], catalog_received_ms=record['received_ms'])
                self.catalog[event['event_id']] = event
            self.select_events(now)
            affected = set(self.events)
        elif kind == 'mapping':
            event = validate_mapping(payload)
            event['mapping_raw_id'] = record['id']
            self.manual[event['event_id']] = event
            self.select_events(now)
            affected = {event['event_id']}
        elif kind == 'dismiss':
            key = str(payload['event_id'])
            self.manual[key] = dict(dismissed=True, mapping_raw_id=record['id'])
            if key in self.events:
                old = dict(self.latest.get(key, {}))
                if old:
                    old.update(source_state='DISMISSED', raw_id=record['id'], timestamp_wall=now)
                    for episode in self.episodes.update(old):
                        if self.persist:
                            self.store.append_episode(record['id'], episode)
                self.events.pop(key, None); self.latest.pop(key, None)
            return
        elif kind == 'pm':
            affected = self.update_pm(record)
        elif kind == 'spot':
            value = number(payload.get('price'))
            source_ms = timestamp(payload.get('timestamp')) or record.get('source_ms')
            if value is not None and value > 0 and self.newer(self.spots.get(subject), source_ms, now):
                self.spots[subject] = dict(price=value, source_ms=source_ms if source_ms is not None else now, timestamp_origin='source' if source_ms is not None else 'receipt', received_ms=now, raw_id=record['id'], venue=record['source'])
                affected = {k for k, e in self.events.items() if e['asset'] == subject}
        elif kind == 'options':
            self.update_options(record)
            self.select_events(now)
            affected = {k for k, e in self.events.items() if e['asset'] == subject}
        elif kind == 'history':
            self.update_history(record)
            affected = {k for k, e in self.events.items() if e['asset'] == subject}
        elif kind == 'timer':
            affected = set(self.events)
        for key in sorted(affected):
            if key in self.events:
                self.observe(self.events[key], record, now)
        if kind == 'timer' and payload.get('analyze') and not self.restoring:
            self.analyze(record, now)

    def save_checkpoint(self):
        if self.persistence_error:
            logging.error('Skipped checkpoint of uncommitted reducer state: %s',self.persistence_error)
            return
        with self.lock, self.store.transaction():
            state = {k: getattr(self,k) for k in ('catalog','events','manual','books','spots','latest','health',
                'last_raw_id','last_received_ms','delayed_packets','clock_errors','observation_count')}
            state.update(config=asdict(self.config), errors=list(self.errors), episodes=self.episodes.active,
                surfaces=[[list(k),v] for k,v in self.surfaces.items()], histories=[[list(k),v] for k,v in self.histories.items()],
                rolling=[[list(k),list(v)] for k,v in self.dynamics.history.items()],
                salient=[[list(k),list(v)] for k,v in self.dynamics.salient.items()],
                previous=[[list(k),v] for k,v in self.dynamics.previous.items()])
            self.store.append_checkpoint(self.last_raw_id, self.code_hash, state)

    def load_checkpoint(self, state):
        self.config = Config(**state['config'])
        self.dynamics, self.episodes = Dynamics(self.config), Episodes(self.config)
        for key in ('catalog','events','manual','books','spots','latest','health','last_raw_id',
                    'last_received_ms','delayed_packets','clock_errors','observation_count'):
            setattr(self,key,state[key])
        self.errors = deque(state['errors'],maxlen=100)
        self.surfaces = {tuple(k):v for k,v in state['surfaces']}
        self.histories = {tuple(k):v for k,v in state['histories']}
        self.episodes.active = state['episodes']
        for key, rows in state['rolling']:
            self.dynamics.history[tuple(key)].extend(rows)
        for key, rows in state['salient']:
            self.dynamics.salient[tuple(key)].extend(rows)
        self.dynamics.previous = {tuple(k):v for k,v in state['previous']}

    def select_events(self, now):
        candidates = [e for e in self.catalog.values() if e['event_type'] != 'unmapped' and e['active'] and e['expiry'] > now and not self.manual.get(e['event_id'], {}).get('dismissed')]
        def supported_expiry(event):
            known = [surface for (asset,expiry),surface in self.surfaces.items() if asset == event['asset']]
            return not known or any(0 <= surface['expiry']-event['expiry'] <=
                (self.config.yahoo_max_expiry_offset_hours if surface['venue']=='yahoo' else self.config.max_expiry_offset_hours)*3600000 for surface in known)
        candidates.sort(key=lambda e: (not supported_expiry(e), e['event_type'] == 'touch', -(e['volume_24h'] or 0), e['event_id']))
        selected, counts = {}, defaultdict(int)
        for event in candidates:
            if len(selected) >= self.config.max_events or counts[event['asset']] >= self.config.max_per_asset:
                continue
            selected[event['event_id']] = event
            counts[event['asset']] += 1
        for key, manual in self.manual.items():
            if not manual.get('dismissed'):
                merged = dict(self.catalog.get(key, {}), **manual)
                # Catalog probability and source provenance refresh; semantic overrides remain manual.
                catalog = self.catalog.get(key, {})
                for field in ('pm_indicative', 'catalog_raw_id', 'catalog_received_ms', 'volume_24h'):
                    if field in catalog:
                        merged[field] = catalog[field]
                selected[key] = merged
        # Keep expired active episodes until a timer closes them, rather than losing lifecycle state.
        for key in self.episodes.active:
            if key not in selected and key in self.events:
                selected[key] = self.events[key]
        self.events = selected
        self.latest = {k: v for k, v in self.latest.items() if k in selected}
        self.dynamics.prune({(e['event_id'], e.get('mapping_hash')) for e in selected.values()}, now)

    def update_pm(self, record):
        affected = set()
        messages = record['payload'] if isinstance(record['payload'], list) else [record['payload']]
        for message in messages:
            kind = message.get('event_type') or message.get('type')
            p = message.get('payload') or message
            source_ms = timestamp(p.get('timestamp')) or record.get('source_ms')
            if kind == 'price_change':
                changes = p.get('price_changes', p.get('priceChanges', []))
            elif kind in ('book', 'best_bid_ask'):
                changes = [p]
            else:
                continue
            for change in changes:
                token = str(change.get('asset_id') or change.get('tokenId') or change.get('token_id') or '')
                if not token or not self.newer(self.books.get(token), source_ms, record['received_ms']):
                    continue
                previous = self.books.get(token, {})
                book = dict(previous)
                if kind == 'book':
                    bids = [probability(x.get('price')) for x in change.get('bids', [])]
                    asks = [probability(x.get('price')) for x in change.get('asks', [])]
                    book.update(bid=max((x for x in bids if x is not None), default=None), ask=min((x for x in asks if x is not None), default=None),
                                depth=dict(bids=change.get('bids', []), asks=change.get('asks', [])), depth_raw_id=record['id'])
                else:
                    for side, snake, camel in (('bid', 'best_bid', 'bestBid'), ('ask', 'best_ask', 'bestAsk')):
                        if snake in change or camel in change:
                            book[side] = probability(change.get(snake, change.get(camel)))
                    # Full depth becomes unverified after incremental top-of-book changes.
                    book['depth'] = None
                book.update(raw_id=record['id'], source_ms=source_ms if source_ms is not None else record['received_ms'], received_ms=record['received_ms'],
                            timestamp_origin='source' if source_ms is not None else 'receipt', method='clob_mid')
                bid, ask = book.get('bid'), book.get('ask')
                book['crossed'] = bid is not None and ask is not None and bid > ask
                book['yes'] = (bid + ask) / 2 if bid is not None and ask is not None and not book['crossed'] else None
                self.books[token] = book
                affected.update(k for k, e in self.events.items() if e['yes_token'] == token)
        return affected

    def update_options(self, record):
        payload, asset = record['payload'], record['subject']
        source_ms = record.get('source_ms')
        previous = next((s for (a, _), s in self.surfaces.items() if a == asset), None)
        if not self.newer(previous, source_ms, record['received_ms']):
            return
        grouped = defaultdict(list)
        puts = defaultdict(list)
        if record['source'] == 'deribit':
            months = dict(zip('JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC'.split(), range(1, 13)))
            from datetime import datetime, timezone
            import re
            for raw in payload.get('result', []):
                match = re.fullmatch(r'([A-Z]+)-(\d{1,2})([A-Z]{3})(\d{2})-([\d.]+)-([CP])', str(raw.get('instrument_name', '')))
                if not match:
                    continue
                expiry = int(datetime(2000 + int(match[4]), months[match[3]], int(match[2]), 8, tzinfo=timezone.utc).timestamp() * 1000)
                forward = number(raw.get('underlying_price'))
                def price(name):
                    value = number(raw.get(name))
                    return value * forward if value is not None and forward is not None and forward > 0 else None
                grouped[expiry]  # Retain expiries that currently have only puts.
                target = grouped if match[6]=='C' else puts
                target[expiry].append(dict(instrument=raw['instrument_name'], strike=float(match[5]), option_type='call' if match[6]=='C' else 'put',
                    bid=price('bid_price'), ask=price('ask_price'), mark=price('mark_price'),
                    iv=number(raw.get('mark_iv')) / 100 if number(raw.get('mark_iv')) is not None else None,
                    forward=forward, source_ms=timestamp(raw.get('creation_timestamp')) or source_ms,
                    volume=number(raw.get('volume')), open_interest=number(raw.get('open_interest'))))
        else:
            for chain in payload.get('chains', []):
                grouped[chain['expiry']].extend(chain['calls'])
            if number(payload.get('spot')) is not None:
                self.spots[asset] = dict(price=payload['spot'], raw_id=record['id'], source_ms=payload.get('spot_source_ms') or record['received_ms'], received_ms=record['received_ms'], venue='yahoo', timestamp_origin='source' if payload.get('spot_source_ms') else 'retrieval_unknown_delay',context=payload.get('underlying_context',{}))
        for expiry, calls in grouped.items():
            self.surfaces[(asset, expiry)] = dict(venue=record['source'], expiry=expiry, calls=calls, puts=puts[expiry],
                source_ms=source_ms if source_ms is not None else record['received_ms'], received_ms=record['received_ms'], raw_id=record['id'],
                timestamp_origin='source' if source_ms is not None else 'retrieval_snapshot',
                forward=next((c.get('forward') for c in calls if c.get('forward')), None))
            if record['source']=='yahoo':
                self.surfaces[(asset,expiry)]['puts']=next((c.get('puts',[]) for c in payload.get('chains',[]) if c['expiry']==expiry),[])
                self.surfaces[(asset,expiry)]['context']=payload.get('underlying_context',{})
        for key in list(self.surfaces):
            if key[0] == asset and key[1] < record['received_ms'] - 86400000:
                del self.surfaces[key]

    def update_history(self, record):
        p = record['payload']; start = p['start_ms']; now = record['received_ms']
        candles = [c for c in p.get('candles', []) if start <= c[0] * 1000 <= now]
        if p.get('session_calendar'):
            lows=[number(c[1]) for c in candles];highs=[number(c[2]) for c in candles]
            good=bool(candles) and len(candles)==len(p.get('sessions',[])) and set(p.get('required_sessions',[]))<=set(p.get('sessions',[]))
            good=good and all(v is not None for v in lows+highs) and not p.get('corporate_action')
            self.histories[(record['subject'],start)]=dict(raw_id=record['id'],received_ms=now,low=min(lows) if good else None,
                high=max(highs) if good else None,error=not good,source='yahoo_regular_sessions',
                session_calendar=p['session_calendar'],calendar_version=p.get('calendar_version'),
                required_sessions=p.get('required_sessions',[]),sessions=p.get('sessions',[]),corporate_action=p.get('corporate_action'))
            return
        days = {int(c[0] // 86400) for c in candles}
        required = set(range(start // 86400000, now // 86400000))
        good = start % 86400000 == 0 and required <= days and candles
        lows = [number(c[1]) for c in candles]; highs = [number(c[2]) for c in candles]
        good = good and all(x is not None for x in lows + highs)
        self.histories[(record['subject'], start)] = dict(raw_id=record['id'], received_ms=now, low=min(lows) if good else None,
            high=max(highs) if good else None, error=not bool(good), source='coinbase_daily')

    def observe(self, event, record, now):
        book = self.books.get(event['yes_token'])
        pm = book
        # An invalid or crossed book is kept invalid. Only absent/stale books may use indicative catalog data.
        if not book or now - book['source_ms'] > self.config.pm_max_age_ms:
            if event.get('pm_indicative') is not None:
                pm = dict(yes=event['pm_indicative'], bid=None, ask=None, depth=None, method='gamma_indicative',
                    source_ms=event.get('catalog_received_ms', 0), received_ms=event.get('catalog_received_ms', 0),
                    raw_id=event.get('catalog_raw_id'), timestamp_origin='retrieval_unknown_delay')
        options = [surface for (asset, expiry), surface in self.surfaces.items() if asset == event['asset'] and expiry >= event['expiry']]
        surface = min(options, key=lambda s: s['expiry']) if options else None
        spot = self.spots.get(event['asset'])
        history = self.histories.get((event['asset'], event.get('window_start')))
        derived = derive(event, pm, surface, spot, history, now, self.config)
        row = {k: event.get(k) for k in ('event_id', 'event_text', 'asset', 'expiry', 'event_type', 'direction', 'strike_or_threshold', 'window_start', 'window_start_assumption', 'settlement_source', 'threshold_inclusive', 'mapping_origin', 'mapping_hash', 'mapping_reason', 'event_url')}
        row.update(derived)
        if event.get('asset') not in ('BTC','ETH'):
            row['underlying_context']=spot.get('context',{}) if spot else {}
        row.update(timestamp_wall=now, timestamp_received=record['received_ms'], timestamp_monotonic=record['monotonic_ns'],
            trigger_kind=record['kind'], trigger_source=record['source'],
            raw_id=record['id'], code_hash=self.code_hash, parameter_hash=self.config.hash, spot=spot.get('price') if spot else None,
            spot_timestamp=spot.get('source_ms') if spot else None, pm_yes=pm.get('yes') if pm else None,
            pm_bid=pm.get('bid') if pm else None, pm_ask=pm.get('ask') if pm else None,
            pm_depth=pm.get('depth') if pm else None, pm_timestamp=pm.get('source_ms') if pm else None,
            pm_source=pm.get('method') if pm else None, pm_timestamp_origin=pm.get('timestamp_origin') if pm else None,
            opt_timestamp=surface.get('source_ms') if surface else None, options_received_ms=surface.get('received_ms') if surface else None,
            opt_timestamp_origin=surface.get('timestamp_origin') if surface else None,
            input_refs={k: v for k, v in dict(catalog=event.get('catalog_raw_id'), mapping=event.get('mapping_raw_id'),
                pm=pm.get('raw_id') if pm else None, options=surface.get('raw_id') if surface else None,
                spot=spot.get('raw_id') if spot else None, history=history.get('raw_id') if history else None).items() if v is not None})
        row.update(discrepancy(row['pm_yes'], row['opt_yes']))
        if row['relative_gap'] is None and row['gap_pp'] is not None and (row['opt_yes'] or 0)>0:
            row['quality_flags'].append('RELATIVE_GAP_OVERFLOW')
        # Timers record state transitions and supply a regular causal analysis clock. All source updates remain raw.
        self.dynamics.update(row)
        self.latest[event['event_id']] = row
        self.observation_count += 1
        if self.persist:
            self.store.append_observation(record['id'], row)
        for episode in self.episodes.update(row):
            if self.persist:
                self.store.append_episode(record['id'], episode)

    def analyze(self, record, now):
        # Historical analyzers are available through their archived reducers.
        # Current collection records source timing and frames, not exotic outputs.
        return

    def snapshot(self):
        with self.lock:
            now = time.time_ns() // 1_000_000
            health = {}
            for source, entry in self.health.items():
                limit = self.config.catalog_seconds * 2000 if source == 'catalog' else self.config.history_seconds * 2000 if source.startswith('history:') else max(self.config.yahoo_seconds * 2000, 90000) if source.startswith('yahoo') else 90000
                health[source] = dict(entry, age_ms=max(0, now-entry['timestamp']))
                if entry['state'] == 'OK' and now-entry['timestamp'] > limit:
                    health[source]['state'] = 'STALE'
            return dict(timestamp=now, rows=list(self.latest.values()), catalog=list(self.catalog.values()),
                sources=health, spots=self.spots,
                stats=self.store.stats(), diagnostics=dict(delayed_packets=self.delayed_packets, clock_errors=self.clock_errors,
                quarantined=list(self.errors), uptime_seconds=(now-self.started_ms)/1000),
                versions=dict(code_hash=self.code_hash, calculation=CALCULATION_VERSION, feature=FEATURE_VERSION, config=self.config.hash))
