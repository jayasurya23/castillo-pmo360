"""Sync PMO 360 projects from the Monday Portfolio board.

Monday's Portfolio board is the source of truth for the Castillo project list
and the job number (e.g. "264-066", "2512-053"). This script can:

  * backfill `project_number` onto projects that already exist here, and
  * create the projects (and their clients) that exist in Monday but not here.

READ-ONLY against Monday — it refuses any query containing a mutation — and it
writes nothing to PMO 360 unless --apply is passed.

    python scripts/sync_projects_from_monday.py                      # dry run
    python scripts/sync_projects_from_monday.py --apply              # numbers only
    python scripts/sync_projects_from_monday.py --create-missing --apply
    python scripts/sync_projects_from_monday.py --set "Nesler=264-066" --apply

Environment:
    MONDAY_API_TOKEN            required
    MONDAY_PORTFOLIO_BOARD_ID   optional, defaults to the PMO workspace Portfolio
    MONDAY_API_VERSION          optional

Matching existing projects is by NAME. Exact (normalised) matches are applied by
default; close-but-not-exact matches are reported and only written with
--include-fuzzy, because assigning the wrong job number silently corrupts every
downstream join.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from difflib import SequenceMatcher

# Allow running as `python scripts/sync_projects_from_monday.py` from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import session_scope  # noqa: E402
from db.models import Client, Project  # noqa: E402

API_URL = "https://api.monday.com/v2"
DEFAULT_BOARD_ID = "18403099969"          # PMO workspace -> Portfolio
FUZZY_THRESHOLD = 0.85
UNASSIGNED_CLIENT = "(Unassigned)"

# Columns resolved by TITLE, never by id — board-specific ids change between
# boards and template versions.
COLUMN_TITLES = {
    "number": "Project ID",
    "client": "Client Name",
    "description": "Project Description",
}


# --------------------------------------------------------------- monday (read)
def _post(token: str, query: str, variables: dict | None = None) -> dict:
    import requests

    if re.search(r"\bmutation\b", query, re.I):
        raise RuntimeError("This script is read-only against Monday.")
    resp = requests.post(
        API_URL,
        json={"query": query, "variables": variables or {}},
        headers={
            "Authorization": token,
            "Content-Type": "application/json",
            "API-Version": os.getenv("MONDAY_API_VERSION", "2024-10"),
        },
        timeout=(10, 60),
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Monday HTTP {resp.status_code}: {resp.text[:300]}")
    payload = resp.json()
    if "errors" in payload:
        raise RuntimeError(f"Monday API error: {str(payload['errors'])[:300]}")
    return payload.get("data") or {}


def fetch_portfolio(token: str, board_id: str) -> list[dict]:
    """Return [{name, number, client, description}] for every Portfolio item."""
    meta = _post(token, """
        query($ids: [ID!]) {
          boards(ids: $ids) { name columns { id title } }
        }""", {"ids": [board_id]})
    boards = meta.get("boards") or []
    if not boards:
        raise RuntimeError(f"Board {board_id} not visible to this token.")

    by_title = {c["title"].strip().lower(): c["id"] for c in boards[0]["columns"]}
    ids: dict[str, str] = {}
    for key, title in COLUMN_TITLES.items():
        cid = by_title.get(title.lower())
        if cid:
            ids[key] = cid
        elif key == "number":
            raise RuntimeError(
                f"No column titled '{title}' on '{boards[0]['name']}'. "
                f"Columns are: {', '.join(c['title'] for c in boards[0]['columns'])}"
            )
    wanted = list(ids.values())
    rev = {v: k for k, v in ids.items()}

    items, cursor = [], None
    page = _post(token, """
        query($ids: [ID!], $cols: [String!]) {
          boards(ids: $ids) {
            items_page(limit: 100) {
              cursor
              items { name column_values(ids: $cols) { id text } }
            }
          }
        }""", {"ids": [board_id], "cols": wanted})
    ip = (page.get("boards") or [{}])[0].get("items_page") or {}
    items.extend(ip.get("items") or [])
    cursor = ip.get("cursor")
    while cursor:
        page = _post(token, """
            query($cursor: String!, $cols: [String!]) {
              next_items_page(cursor: $cursor, limit: 100) {
                cursor
                items { name column_values(ids: $cols) { id text } }
              }
            }""", {"cursor": cursor, "cols": wanted})
        ip = page.get("next_items_page") or {}
        items.extend(ip.get("items") or [])
        cursor = ip.get("cursor")

    out = []
    for it in items:
        row = {"name": (it.get("name") or "").strip(),
               "number": None, "client": None, "description": None}
        for cv in it.get("column_values") or []:
            key = rev.get(cv["id"])
            if key:
                row[key] = ((cv.get("text") or "").strip()) or None
        out.append(row)
    return out


# ------------------------------------------------------------------- matching
def norm(name: str) -> str:
    """Lowercase, punctuation to spaces, whitespace collapsed."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (name or "").lower())).strip()


def match(projects: list, portfolio: list[dict]) -> dict:
    by_norm: dict[str, list[dict]] = {}
    for row in portfolio:
        by_norm.setdefault(norm(row["name"]), []).append(row)

    exact, fuzzy, unmatched = [], [], []
    for p in projects:
        key = norm(p.name)
        hit = by_norm.get(key)
        if hit:
            exact.append((p, hit[0]))
            continue
        best, score = None, 0.0
        for row in portfolio:
            r = SequenceMatcher(None, key, norm(row["name"])).ratio()
            if r > score:
                best, score = row, r
        if best and score >= FUZZY_THRESHOLD:
            fuzzy.append((p, best, round(score, 2)))
        else:
            unmatched.append((p, best, round(score, 2) if best else 0.0))

    claimed = {norm(m["name"]) for _, m in exact} | {norm(m["name"]) for _, m, _ in fuzzy}
    orphans = [r for r in portfolio if norm(r["name"]) not in claimed]
    return {"exact": exact, "fuzzy": fuzzy, "unmatched": unmatched, "orphans": orphans}


# ------------------------------------------------------------------- reporting
def rule(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


def report(res: dict) -> None:
    rule(f"ALREADY IN PMO 360 — exact name match ({len(res['exact'])})")
    for p, m in res["exact"]:
        cur = f"  (currently {p.project_number})" if p.project_number else ""
        print(f"  {p.name[:44]:<44} -> {m['number'] or '(blank in Monday)'}{cur}")
    if not res["exact"]:
        print("  none")

    if res["fuzzy"]:
        rule(f"CLOSE MATCHES — review before applying ({len(res['fuzzy'])})")
        for p, m, score in res["fuzzy"]:
            print(f"  [{score}] {p.name[:36]:<36} -> {m['name'][:26]:<26} {m['number'] or '(blank)'}")

    if res["unmatched"]:
        rule(f"IN PMO 360, NOT IN MONDAY ({len(res['unmatched'])})")
        for p, best, score in res["unmatched"]:
            near = f"  closest: {best['name']} ({score})" if best else ""
            print(f"  {p.name[:44]:<44}{near}")

    seen: dict[str, list[str]] = {}
    for p, m in res["exact"]:
        if m["number"]:
            seen.setdefault(m["number"], []).append(p.name)
    dupes = {k: v for k, v in seen.items() if len(v) > 1}
    if dupes:
        rule(f"DUPLICATE JOB NUMBERS ({len(dupes)})")
        for num, names in dupes.items():
            print(f"  {num} -> {', '.join(names)}")
        print("  Resolve these before relying on project_number as a join key.")


# ------------------------------------------------------------------ creation
def create_missing(session, orphans: list[dict], *, apply: bool,
                   include_unnumbered: bool) -> tuple[int, int, list[str]]:
    """Create Client (find-or-create) + Project for each Monday-only row."""
    skipped = [r["name"] for r in orphans if not r["number"]]
    todo = orphans if include_unnumbered else [r for r in orphans if r["number"]]
    if not todo:
        return 0, 0, skipped

    existing = {c.name.strip().lower(): c for c in session.query(Client).all()}
    new_clients: dict[str, Client] = {}
    created_projects = 0

    rule(f"{'CREATING' if apply else 'WOULD CREATE'} PROJECTS ({len(todo)})")
    for row in sorted(todo, key=lambda r: (r["client"] or "", r["name"])):
        cname = (row["client"] or UNASSIGNED_CLIENT).strip()
        key = cname.lower()
        client = existing.get(key) or new_clients.get(key)
        is_new_client = client is None
        if is_new_client:
            client = Client(name=cname)
            new_clients[key] = client
            if apply:
                session.add(client)
                session.flush()          # assign client.id

        flag = "  +client" if is_new_client else ""
        print(f"  {row['name'][:38]:<38} {row['number'] or '(no number)':<12} "
              f"{cname[:24]:<24}{flag}")

        if apply:
            session.add(Project(
                client_id=client.id,
                name=row["name"],
                project_number=row["number"],
                scope=row["description"],
            ))
        created_projects += 1

    # Monday's Client Name is free text, so the same company arrives spelled
    # several ways ("Priority Power" vs "Priority Power Management"). Creating
    # both splits that company's projects across two clients, which is painful
    # to unpick later — so warn while it is still cheap to fix.
    names = sorted({c.name for c in new_clients.values()} | set(existing))
    near = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            na, nb = norm(a), norm(b)
            # A plain ratio misses the commonest case: one name being a
            # whole-word prefix of the other. "Priority Power" vs "Priority
            # Power Management" scores only 0.72, so test containment too.
            contained = na.startswith(nb + " ") or nb.startswith(na + " ")
            if contained or SequenceMatcher(None, na, nb).ratio() >= 0.80:
                near.append((a, b))
    if near:
        rule(f"POSSIBLE DUPLICATE CLIENTS ({len(near)})")
        for a, b in near:
            print(f"  {a!r}  ~  {b!r}")
        print("  Same company spelled two ways? Fix in Monday, or merge here afterwards.")

    return created_projects, len(new_clients), skipped


# ------------------------------------------------------------------------ main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write to PMO 360 (default: dry run)")
    ap.add_argument("--create-missing", action="store_true",
                    help="create projects/clients that exist in Monday but not here")
    ap.add_argument("--include-unnumbered", action="store_true",
                    help="also create Monday rows that have no job number")
    ap.add_argument("--include-fuzzy", action="store_true", help="also write close matches")
    ap.add_argument("--overwrite", action="store_true",
                    help="replace a project_number that is already set")
    ap.add_argument("--set", action="append", default=[], metavar="NAME=NUMBER",
                    help="set one project by name, bypassing Monday (repeatable)")
    ap.add_argument("--board", default=os.getenv("MONDAY_PORTFOLIO_BOARD_ID", DEFAULT_BOARD_ID))
    args = ap.parse_args()

    # --- manual overrides, no Monday call needed -------------------------
    if args.set:
        pairs = []
        for spec in args.set:
            if "=" not in spec:
                print(f"ERROR: --set expects NAME=NUMBER, got {spec!r}")
                return 2
            n, v = spec.split("=", 1)
            pairs.append((n.strip(), v.strip()))
        with session_scope() as s:
            for name, number in pairs:
                hits = [p for p in s.query(Project).all() if norm(p.name) == norm(name)]
                if not hits:
                    print(f"  no project named {name!r}")
                    continue
                for p in hits:
                    print(f"  {p.name} -> {number}" + ("" if args.apply else "   (dry run)"))
                    if args.apply:
                        p.project_number = number
        print("\nDone." if args.apply else "\nDry run — nothing written. Re-run with --apply.")
        return 0

    token = os.getenv("MONDAY_API_TOKEN")
    if not token:
        print("ERROR: MONDAY_API_TOKEN is not set.")
        return 2

    print(f"Reading Monday board {args.board} (read-only)...")
    try:
        portfolio = fetch_portfolio(token, args.board)
    except Exception as e:  # noqa: BLE001 — surface the real reason and stop
        print(f"ERROR: {type(e).__name__}: {e}")
        return 1
    with_num = sum(1 for r in portfolio if r["number"])
    print(f"  {len(portfolio)} portfolio items, {with_num} with a job number")

    with session_scope() as s:
        projects = s.query(Project).order_by(Project.name).all()
        print(f"  {len(projects)} projects currently in PMO 360")

        res = match(projects, portfolio)
        report(res)

        # 1. backfill numbers onto existing projects
        pending = [(p, m) for p, m in res["exact"] if m["number"]]
        if args.include_fuzzy:
            pending += [(p, m) for p, m, _ in res["fuzzy"] if m["number"]]
        writes = [(p, m) for p, m in pending if args.overwrite or not p.project_number]
        already = len(pending) - len(writes)
        if args.apply:
            for p, m in writes:
                p.project_number = m["number"]

        # 2. create the ones Monday has and we don't
        made_p = made_c = 0
        skipped_unnumbered: list[str] = []
        if args.create_missing:
            made_p, made_c, skipped_unnumbered = create_missing(
                s, res["orphans"], apply=args.apply,
                include_unnumbered=args.include_unnumbered)
        elif res["orphans"]:
            rule(f"IN MONDAY, NOT IN PMO 360 ({len(res['orphans'])})")
            for r in sorted(res["orphans"], key=lambda x: x["name"]):
                print(f"  {r['name'][:38]:<38} {r['number'] or '(no number)':<12} {r['client'] or ''}")
            print("\n  Re-run with --create-missing to create these.")

        rule("RESULT")
        verb = "Wrote" if args.apply else "Would write"
        print(f"  {verb} {len(writes)} project number(s) onto existing projects"
              + (f"; {already} already set (use --overwrite)" if already else ""))
        if args.create_missing:
            print(f"  {'Created' if args.apply else 'Would create'} "
                  f"{made_p} project(s) and {made_c} client(s)")
            if skipped_unnumbered:
                print(f"  Skipped {len(skipped_unnumbered)} Monday row(s) with no job "
                      f"number: {', '.join(skipped_unnumbered)}")
                print("    (use --include-unnumbered to create them anyway)")
        if not args.apply:
            print("\n  DRY RUN — nothing written. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
