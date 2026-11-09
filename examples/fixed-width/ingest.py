"""Append physical lines to a governed Trino/Iceberg table; never strip field padding."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

COLUMNS = ("load_id", "definition_name", "source_file", "line_number", "record_length", "ingested_at", "raw_line")


def rows(path, definition_name, encoding="utf-8", load_id=None):
    load_id = load_id or uuid4().hex
    ingested_at = datetime.now(timezone.utc).isoformat()
    # newline="" recognizes CRLF/LF/CR while leaving the original terminators intact.
    with Path(path).open(encoding=encoding, newline="") as handle:
        for number, physical_line in enumerate(handle, 1):
            if physical_line.endswith("\r\n"):
                raw_line = physical_line[:-2]
            elif physical_line.endswith(("\n", "\r")):
                raw_line = physical_line[:-1]
            else:
                raw_line = physical_line
            yield (load_id, definition_name, Path(path).name, number, len(raw_line), ingested_at, raw_line)


def quote_ident(value):
    return '"' + value.replace('"', '""') + '"'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path, nargs="?", default=Path(__file__).parent / "data/customers.txt")
    parser.add_argument("--source", default="lake", help="Registered Limoon SQL source")
    parser.add_argument("--catalog", default="lake", help="Trino catalog name; may differ from the source name")
    parser.add_argument("--schema", default="fixed_width", help="Existing, registered schema")
    parser.add_argument("--definition-name", required=True, help="The layout these lines follow, e.g. customers_v1")
    parser.add_argument("--encoding", default="utf-8")
    parser.add_argument("--load-id", help="Name this load, e.g. after an orchestrator run; a load already in the table is not appended again")
    parser.add_argument("--dry-run", action="store_true", help="Print raw rows as JSON without accessing Limoon")
    args = parser.parse_args()
    records = rows(args.file, args.definition_name, args.encoding, args.load_id)
    if args.dry_run:
        for row in records:
            print(json.dumps(dict(zip(COLUMNS, row)), ensure_ascii=False))
        return

    import fdp
    table = '.'.join(quote_ident(x) for x in (args.catalog, args.schema, "raw_lines"))
    fdp.query(args.source, f"""create table if not exists {table} (
        load_id varchar, definition_name varchar, source_file varchar,
        line_number bigint, record_length integer, ingested_at varchar, raw_line varchar
    )""")
    # A retried task reuses its load ID: skip a load that completed rather than append it twice, and stop on one
    # that was interrupted between batches, since appending again would duplicate its first lines.
    if args.load_id:
        records = list(records)
        loaded = fdp.query(args.source, f"select count(*) from {table} where load_id = ?", [args.load_id])["rows"][0][0]
        if loaded == len(records):
            print(f"Load {args.load_id} is already in {table}; nothing appended")
            return
        if loaded:
            raise SystemExit(f"Load {args.load_id} is incomplete in {table} ({loaded} of {len(records)} lines); "
                             "remove its rows before retrying")
    count, batch = 0, []
    def insert(items):
        placeholders = ', '.join(['(' + ', '.join(['?'] * len(COLUMNS)) + ')'] * len(items))
        fdp.query(args.source, f"insert into {table} ({', '.join(COLUMNS)}) values {placeholders}",
                  [value for row in items for value in row])
    for row in records:
        batch.append(row)
        if len(batch) == 100:
            insert(batch); count += len(batch); batch = []
    if batch:
        insert(batch); count += len(batch)
    print(f"Appended {count} raw lines to {table}")


if __name__ == "__main__":
    main()
