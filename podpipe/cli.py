"""Command line interface.

    podpipe add https://example.com/feed.xml
    podpipe sync
    podpipe run --limit 3
    podpipe show 12
    podpipe search "interest rates"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import CONFIG
from .pipeline import Processor, remerge, sync_feeds
from .store import Store


def _fmt_time(seconds: float) -> str:
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def cmd_add(args, store: Store) -> int:
    feed_id = store.add_feed(args.url)
    print(f"feed {feed_id}: {args.url}")
    print("run `podpipe sync` to pull in the episode list")
    return 0


def cmd_feeds(args, store: Store) -> int:
    rows = store.feeds()
    if not rows:
        print("no feeds yet — add one with `podpipe add <rss-url>`")
    for f in rows:
        print(f"{f['id']:>4}  {f['title'] or '(untitled)'}\n      {f['url']}")
    return 0


def cmd_sync(args, store: Store) -> int:
    print("syncing feeds…")
    total = sync_feeds(store)
    print(f"{total} new episode(s)")
    return 0


def cmd_run(args, store: Store) -> int:
    pending = store.pending(limit=args.limit, retry_errors=args.retry)
    if not pending:
        print("nothing pending")
        return 0
    print(f"processing {len(pending)} episode(s)")
    proc = Processor(CONFIG)
    for episode in pending:
        proc.process(store, episode, force=args.force)
    return 0


def cmd_status(args, store: Store) -> int:
    stats = store.stats()
    if not stats:
        print("no episodes")
        return 0
    for status, n in sorted(stats.items()):
        print(f"{status:>12}  {n}")
    return 0


def cmd_show(args, store: Store) -> int:
    ep = store.episode(args.episode_id)
    if ep is None:
        print(f"no episode {args.episode_id}", file=sys.stderr)
        return 1

    turns = store.turns(args.episode_id)
    lines = [f"# {ep['title'] or ep['guid']}", ""]
    for t in turns:
        stamp = _fmt_time(t["start"])
        lines.append(f"**{t['speaker']}** ({stamp})")
        lines.append(t["text"])
        lines.append("")

    text = "\n".join(lines)
    if args.out:
        Path(args.out).write_text(text)
        print(f"wrote {args.out} ({len(turns)} turns)")
    else:
        print(text)
    return 0


def cmd_search(args, store: Store) -> int:
    rows = store.search(args.query, limit=args.limit)
    if not rows:
        print("no matches")
        return 0
    for r in rows:
        print(f"[{r['episode_id']}] {r['episode_title'] or ''} @ {_fmt_time(r['start'])}")
        print(f"    {r['speaker']}: {r['snip']}")
    return 0


def cmd_remerge(args, store: Store) -> int:
    n = remerge(store, CONFIG, args.episode_id)
    print(f"re-merged episode {args.episode_id}: {n} turns")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="podpipe", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("add", help="add an RSS feed")
    a.add_argument("url")
    a.set_defaults(func=cmd_add)

    f = sub.add_parser("feeds", help="list feeds")
    f.set_defaults(func=cmd_feeds)

    s = sub.add_parser("sync", help="poll feeds for new episodes")
    s.set_defaults(func=cmd_sync)

    r = sub.add_parser("run", help="download, transcribe and diarize pending episodes")
    r.add_argument("--limit", type=int, default=None)
    r.add_argument("--force", action="store_true", help="ignore cached model output")
    r.add_argument("--retry", action="store_true", help="also retry episodes that failed")
    r.set_defaults(func=cmd_run)

    st = sub.add_parser("status", help="episode counts by state")
    st.set_defaults(func=cmd_status)

    sh = sub.add_parser("show", help="print a transcript as markdown")
    sh.add_argument("episode_id", type=int)
    sh.add_argument("--out", help="write to a file instead of stdout")
    sh.set_defaults(func=cmd_show)

    se = sub.add_parser("search", help="full-text search across transcripts")
    se.add_argument("query")
    se.add_argument("--limit", type=int, default=20)
    se.set_defaults(func=cmd_search)

    rm = sub.add_parser("remerge", help="redo speaker merge from cached model output")
    rm.add_argument("episode_id", type=int)
    rm.set_defaults(func=cmd_remerge)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    CONFIG.ensure_dirs()
    store = Store(CONFIG.db_path)
    try:
        return args.func(args, store)
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
