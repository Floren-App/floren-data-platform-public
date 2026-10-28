"""Read-only latency benchmark for a prepared PostgreSQL relation, through the real SDK.

Run with SDK/sql + psycopg installed, FDP_API, FDP_SESSION_TOKEN, and
FDP_BENCH_PG_URL for the direct baseline. The relation needs integer id/amount
columns and an index on id. Never writes data or records credentials in its report.
"""
import argparse
import concurrent.futures
import json
import math
import re
import threading
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', default='finance')
    parser.add_argument('--relation', default='reporting.bench')
    parser.add_argument('--iterations', type=int, default=30)
    parser.add_argument('--concurrency', type=int, default=4)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*', args.relation):
        parser.error('relation must be a simple schema.table identifier')
    import os
    import psycopg
    import fdp
    engine = fdp.connect(args.source, schema=args.relation.split('.')[0])
    direct = threading.local()
    native_connections = []
    native_lock = threading.Lock()
    cases = {
        'indexed_lookup': f'SELECT id,amount FROM {args.relation} WHERE id = ?',
        'selective_filter': f'SELECT id,amount FROM {args.relation} WHERE id BETWEEN ? AND ? ORDER BY id LIMIT 20',
        'small_aggregate': f'SELECT count(*),sum(amount) FROM {args.relation} WHERE id BETWEEN ? AND ?',
    }

    def call(mode, query, i):
        parameters = (1 + i % 100,) if query.count('?') == 1 else (1 + i % 100, 20 + i % 100)
        if mode == 'postgres_direct':
            if not getattr(direct, 'connection', None):
                direct.connection = psycopg.connect(os.environ['FDP_BENCH_PG_URL'], autocommit=True)
                with native_lock:
                    native_connections.append(direct.connection)
            return direct.connection.execute(query.replace('?', '%s'), parameters).fetchall()
        with engine.connect() as connection:
            return connection.exec_driver_sql(query, parameters).fetchall()

    report = {'engine': 'Trino 484', 'note': 'Local deployment measurement; no production latency guarantee.',
              'iterations': args.iterations, 'concurrency': args.concurrency, 'measurements': {}, 'plans': {}}
    try:
        for name, query in cases.items():
            with engine.connect() as connection:
                # Trino EXPLAIN does not bind placeholders in all statement forms.
                plan = connection.exec_driver_sql('EXPLAIN ' + query.replace('?', '1', 1).replace('?', '20')).fetchall()
                report['plans'][name] = '\n'.join(str(row[0]) for row in plan)
            for mode in ('postgres_direct', 'shared_trino'):
                for concurrency in (1, args.concurrency):
                    def sample(i):
                        # Warm every worker's connection before taking its first measurement.
                        if not getattr(direct, mode + '_warm', False):
                            call(mode, query, i)
                            setattr(direct, mode + '_warm', True)
                        started = time.perf_counter()
                        rows = call(mode, query, i)
                        assert rows, 'benchmark returned no rows'
                        return (time.perf_counter() - started) * 1000
                    started = time.perf_counter()
                    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
                        timings = sorted(pool.map(sample, range(args.iterations)))
                    elapsed = time.perf_counter() - started
                    report['measurements'][f'{name}/{mode}/concurrency_{concurrency}'] = {
                        'p50_ms': round(timings[len(timings)//2], 3),
                        'p95_ms': round(timings[max(0, math.ceil(len(timings)*.95)-1)], 3),
                        'requests_per_second_including_warmup': round(len(timings)/elapsed, 3)}
    finally:
        engine.dispose()
        for connection in native_connections:
            connection.close()
    with open(args.output, 'w') as stream:
        json.dump(report, stream, indent=2)
        stream.write('\n')


if __name__ == '__main__':
    main()
