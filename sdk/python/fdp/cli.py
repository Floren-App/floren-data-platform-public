"""`fdp` command for workspace terminals.

    fdp programs                                   # list programs in fdp.json
    fdp start sales-report --warehouse large start_date=2026-01-01 region=North
    fdp run models/train.py --pool large            # ad-hoc file
    fdp logs <run-id> -f
"""

from __future__ import annotations

import argparse
import json
import time

import fdp
from fdp import _client


def main() -> None:
    p = argparse.ArgumentParser(prog="fdp")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sources", help="list data sources you can use")
    sub.add_parser("runs", help="list your recent runs")
    sub.add_parser("programs", help="list programs defined in fdp.json")

    sp = sub.add_parser("start", help="run a program from fdp.json")
    sp.add_argument("program")
    sp.add_argument("values", nargs="*", metavar="name=value", help="argument values")
    sp.add_argument("--warehouse", "-w", default="", help="override the program's warehouse")
    sp.add_argument("--detach", "-d", action="store_true", help="don't stream the console")

    for name, kind in (("run", "script"), ("notebook", "notebook"), ("app", "app")):
        sp = sub.add_parser(name, help=f"start an ad-hoc {kind} on a warehouse")
        sp.add_argument("entrypoint")
        sp.add_argument("--pool", "--warehouse", dest="pool", default="", help="default: the platform's default warehouse")
        sp.add_argument("--follow", "-f", action="store_true", help="stream the console until it finishes")
        sp.set_defaults(kind=kind)
    lp = sub.add_parser("logs", help="show the console of a run")
    lp.add_argument("run_id")
    lp.add_argument("--follow", "-f", action="store_true")
    sp = sub.add_parser("stop", help="stop a run or app")
    sp.add_argument("run_id")
    dp = sub.add_parser("dbt-profile", help="print (or --write) a dbt profiles.yml for a data source")
    dp.add_argument("--source", help="data source to build in (default: $FDP_DBT_SOURCE)")
    dp.add_argument("--schema", help="schema (default: your dev schema dbt_<user>)")
    dp.add_argument("--target", help="target name (default: dev)")
    dp.add_argument("--threads", type=int)
    dp.add_argument("--write", action="store_true", help="write ~/.dbt/profiles.yml instead of printing")
    args = p.parse_args()

    if args.cmd == "dbt-profile":
        from fdp import dbt

        dbt.main(args.source, args.schema, args.target, args.threads, args.write)
        return

    if args.cmd == "sources":
        for s in fdp.sources():
            print(f"{s['name']:<24} {s['kind']:<10} {s['description']}")
    elif args.cmd == "runs":
        for r in fdp.runs():
            what = r.get("program") or r["entrypoint"]
            print(f"{r['id']}  {r['status']:<10} {r['pool_name']:<8} {r['kind']:<8} {what}")
    elif args.cmd == "programs":
        cfg = fdp.programs()
        for err in cfg.get("errors", []):
            print(f"! {err}")
        for name, prog in ((cfg.get("config") or {}).get("programs") or {}).items():
            params = " ".join(f"{a['name']}=<{a['type']}>" for a in prog.get("args", []))
            print(f"{name:<24} {prog['kind']:<8} {prog.get('warehouse') or '-':<8} {params}")
    elif args.cmd == "start":
        values = dict(v.split("=", 1) for v in args.values)
        r = fdp.run_program(args.program, warehouse=args.warehouse, **values)
        print(f"started {args.program} as run {r['id']} on warehouse {r['pool_name']}")
        if not args.detach:
            _follow(r["id"])
    elif args.cmd in ("run", "notebook", "app"):
        r = fdp.run(args.entrypoint, pool=args.pool, kind=args.kind)
        print(f"started {r['kind']} {r['id']} on warehouse {r['pool_name']}")
        if args.follow:
            _follow(r["id"])
    elif args.cmd == "logs":
        if args.follow:
            _follow(args.run_id)
        else:
            with _client() as c:
                print(c.get(f"/v1/runs/{args.run_id}/logs").text, end="")
    elif args.cmd == "stop":
        with _client() as c:
            print(json.dumps(c.post(f"/v1/runs/{args.run_id}/stop").json(), indent=2, default=str))


def _follow(run_id: str) -> None:
    seen = 0
    with _client() as c:
        while True:
            logs = c.get(f"/v1/runs/{run_id}/logs", params={"tail": 20000}).text
            print(logs[seen:], end="", flush=True)
            seen = len(logs)
            status = c.get(f"/v1/runs/{run_id}").json()["status"]
            if status not in ("pending", "running"):
                print(f"\n[{status}]")
                return
            time.sleep(2)


if __name__ == "__main__":
    main()
