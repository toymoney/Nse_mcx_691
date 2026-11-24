#!/usr/bin/env python3
"""Append-only MCX snapshots; no strategy filtering. Also export/analyse saved data."""
import argparse
import csv
import gzip
import json
import os
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict


class Recorder:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.handle = None
        self.path = None
        self.bucket = None
        self.rows = 0

    def write(self, row):
        stamp = datetime.fromisoformat(row['observed_at'])
        bucket = stamp.strftime('%Y-%m-%d/%H') + f'{stamp.minute//5*5:02}'
        if self.bucket != bucket:
            self.close()
            folder = self.root / stamp.strftime('%Y-%m-%d')
            folder.mkdir(exist_ok=True)
            # Exclusive creation: never overwrite a previous job/restart's data.
            suffix = datetime.now(timezone.utc).strftime('%H%M%S%f') + f'-{os.getpid()}'
            self.path = folder / (bucket.split('/')[-1] + '-' + suffix + '.jsonl.gz')
            self.handle = gzip.open(self.path, 'xt', encoding='utf-8')
            self.bucket = bucket
        self.handle.write(json.dumps(row, separators=(',', ':'), allow_nan=False) + '\n')
        self.handle.flush()
        self.rows += 1

    def close(self):
        if self.handle:
            self.handle.close()
        self.handle = None
        self.bucket = None


def records(path):
    p = Path(path)
    files = [p] if p.is_file() else sorted(p.rglob('*.jsonl.gz'))
    for file in files:
        with gzip.open(file, 'rt', encoding='utf-8') as handle:
            for line in handle:
                if line.strip():
                    yield json.loads(line)


def flatten(row):
    result = {k: v for k, v in row.items() if not isinstance(v, (list, dict))}
    for name in ('quality_flags','peer_keys'):
        if name in row: result[name]=json.dumps(row[name],separators=(',',':'))
    for name in ('broker_greeks', 'model_greeks'):
        for k, v in (row.get(name) or {}).items():
            if not isinstance(v, (dict, list)):
                result[name + '_' + k] = v
    return result


def export_csv(source, destination):
    # Stable superset of columns; raw responses remain in JSONL, never dropped there.
    fields = set()
    for row in records(source):
        if row.get('kind') == 'option': fields.update(flatten(row))
    with open(destination, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=sorted(fields))
        writer.writeheader()
        for row in records(source):
            if row.get('kind') == 'option': writer.writerow(flatten(row))


def analyse(source, destination):
    """Every computed positive-gap snapshot, not deduped alerts; polled observations only."""
    groups = defaultdict(list)
    for row in records(source):
        if row.get('kind') == 'option':
            groups[(row['observed_at'][:10], row['instrument_key'])].append(row)
    rows = []
    for values in groups.values():
        values.sort(key=lambda r: r['observed_at'])
        # Future quotes must describe trades AFTER the research snapshot.
        trades = sorted((r['last_trade_at'], r['ltp']) for r in values
                        if r.get('last_trade_at') and r.get('ltp') is not None
                        and r.get('ltp', 0) > 0
                        and r['last_trade_at'] <= r['observed_at'])
        suffix = [0.] * (len(trades) + 1)
        for i in range(len(trades)-1, -1, -1): suffix[i] = max(suffix[i+1], trades[i][1])
        from bisect import bisect_right
        times = [t[0] for t in trades]
        for r in values:
            if not r.get('model_target') or not r.get('gap_pct', 0) > 0: continue
            start = bisect_right(times, r['observed_at'])
            later = len(trades) - start
            out = flatten(r)
            out['outcome'] = ('TARGET_OBSERVED' if suffix[start] >= r['model_target']
                              else 'NO_TOUCH_OBSERVED' if later else 'NO_LATER_DATA')
            out['later_trade_observations'] = later
            rows.append(out)
    fields = sorted({k for r in rows for k in r})
    with open(destination, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields or ['outcome'])
        writer.writeheader(); writer.writerows(rows)
    counts = defaultdict(int)
    for row in rows: counts[row['outcome']] += 1
    return dict(counts)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=['record', 'export', 'analyse'])
    p.add_argument('--source', default='mcx-state/snapshots')
    p.add_argument('--output', default='mcx_research.csv')
    args = p.parse_args()
    if args.mode == 'record':
        from mcx_s3_bot import run
        run('record')
    elif args.mode == 'export': export_csv(args.source, args.output)
    else: print(json.dumps(analyse(args.source, args.output)))

if __name__ == '__main__': main()
