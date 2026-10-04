"""Command line interface.

    ripperr add https://example.com/feed.xml
    ripperr sync
    ripperr run --limit 3
    ripperr show 12
    ripperr search "interest rates"
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .api import Ripperr


def _fmt_time(seconds: float) -> str:
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def _ref(text: str) -> int | str:
    """Digits mean a local episode id; anything else is a guid."""
    return int(text) if text.isdigit() else text


def cmd_add(args, rip: Ripperr) -> int:
    feed = rip.add_feed(args.url)
    print(f"feed {feed.id}: {feed.url}")
    if args.backfill:
        print(f"{rip.backfill(feed.id, args.backfill)} episode(s) backfilled")
    print("run `ripperr sync` to pull in the episode list")
    return 0


def cmd_feeds(args, rip: Ripperr) -> int:
    feeds = rip.feeds()
    if not feeds:
        print("no feeds yet — add one with `ripperr add <rss-url>`")
    for f in feeds:
        print(f"{f.id:>4}  {f.title or '(untitled)'}\n      {f.url}")
    return 0


def cmd_sync(args, rip: Ripperr) -> int:
    print("syncing feeds…")
    print(f"{rip.sync()} new episode(s)")
    return 0


def cmd_run(args, rip: Ripperr) -> int:
    if not rip.process(limit=args.limit, force=args.force, retry_errors=args.retry):
        print("nothing pending")
    return 0


def cmd_status(args, rip: Ripperr) -> int:
    stats = rip.stats()
    if not stats:
        print("no episodes")
        return 0
    for status, n in sorted(stats.items()):
        print(f"{status:>12}  {n}")
    return 0


def cmd_show(args, rip: Ripperr) -> int:
    tr = rip.transcript(_ref(args.episode))
    if tr is None:
        print(f"no episode {args.episode}", file=sys.stderr)
        return 1

    lines = [f"# {tr.episode.title or tr.episode.guid}", ""]
    speaker_names = {mapping.speaker: mapping.name for mapping in tr.speaker_names}
    for t in tr.turns:
        lines.append(f"**{speaker_names.get(t.speaker, t.speaker)}** ({_fmt_time(t.start)})")
        lines.append(t.text)
        lines.append("")

    text = "\n".join(lines)
    if args.out:
        Path(args.out).write_text(text)
        print(f"wrote {args.out} ({len(tr.turns)} turns)")
    else:
        print(text)
    return 0


def cmd_search(args, rip: Ripperr) -> int:
    hits = rip.search(args.query, limit=args.limit)
    if not hits:
        print("no matches")
        return 0
    for h in hits:
        print(f"[{h.episode_id}] {h.episode_title or ''} @ {_fmt_time(h.start)}")
        print(f"    {h.speaker}: {h.snippet}")
    return 0


def cmd_remerge(args, rip: Ripperr) -> int:
    ep = rip.remerge(_ref(args.episode))
    print(f"re-merged episode {ep.id}: revision {ep.revision}")
    return 0


def cmd_calibrate(args, rip: Ripperr) -> int:
    from .calibration import calibrate_identity

    feed = rip.feed(args.feed) if args.feed is not None else next(
        (feed for feed in rip.feeds() if "on the couch" in (feed.title or "").casefold()),
        None,
    )
    if feed is None:
        print("no matching feed; pass --feed <id>", file=sys.stderr)
        return 1

    episodes = rip.episodes(status="done")
    episodes = [episode for episode in episodes if episode.feed_id == feed.id]
    names = {episode.guid: rip.speaker_names(episode.guid) for episode in episodes}
    embeddings = {episode.guid: rip.speaker_embeddings(episode.guid) for episode in episodes}
    try:
        result = calibrate_identity(episodes, names, embeddings, args.identity)
    except ValueError as exc:
        print(f"calibration unavailable: {exc}", file=sys.stderr)
        return 1

    print(f"identity: {result.identity}")
    print(f"episodes: {result.episode_count}")
    print(f"labeled samples: {result.sample_count} ({result.positive_count} positive, {result.negative_count} other)")
    print(f"raw margin accuracy: {result.raw_accuracy:.1%}")
    print(f"leave-one-episode-out accuracy: {result.leave_one_out_accuracy:.1%}")
    print(f"leave-one-episode-out Brier score: {result.leave_one_out_brier:.3f}")
    print(f"probability model: sigmoid({result.intercept:.3f} + {result.slope:.3f} × identity margin)")
    return 0


def _feed_for_identity(args, rip: Ripperr):
    feed = rip.feed(args.feed) if args.feed is not None else next(
        (feed for feed in rip.feeds() if "on the couch" in (feed.title or "").casefold()),
        None,
    )
    if feed is None:
        print("no matching feed; pass --feed <id>", file=sys.stderr)
    return feed


def cmd_profiles(args, rip: Ripperr) -> int:
    feed = _feed_for_identity(args, rip)
    if feed is None:
        return 1
    profiles = rip.rebuild_speaker_profiles(feed.id)
    print(f"feed {feed.id}: {feed.title or feed.url}")
    if not profiles:
        print("no manually labeled voice samples")
        return 0
    for profile in profiles:
        print(f"{profile.name}: {profile.sample_count} sample(s), {len(profile.embedding)} dimensions")
    return 0


def cmd_identify(args, rip: Ripperr) -> int:
    feed = _feed_for_identity(args, rip)
    if feed is None:
        return 1
    if args.llm and not rip.cfg.deepinfra_token:
        print("set RIPPERR_DEEPINFRA_TOKEN (or DEEPINFRA_TOKEN) to use --llm", file=sys.stderr)
        return 1
    if not rip.speaker_profiles(feed.id):
        rip.rebuild_speaker_profiles(feed.id)
    episodes = [episode for episode in rip.episodes(status="done") if episode.feed_id == feed.id]
    for episode in sorted(episodes, key=lambda item: (item.published or "", item.id), reverse=True):
        print(f"\n{episode.title or episode.guid}")
        for match in rip.speaker_matches(episode.guid):
            print(f"  {match.speaker} -> {match.name} ({match.score:.3f}, {match.sample_count} samples)")
        for hint in rip.guest_hints(episode.guid, use_llm=args.llm):
            print(f"  guest hint -> {hint.name} [{hint.source}, {hint.confidence:.2f}]: {hint.evidence}")
    return 0


def cmd_serve(args, rip: Ripperr) -> int:
    from .server import serve

    serve(rip.cfg, args.host, args.port, args.token, args.emit_current)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ripperr", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("add", help="add an RSS feed")
    a.add_argument("url")
    a.add_argument("--backfill", type=int, metavar="N",
                   help="also record the feed's N newest episodes, not just the latest")
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
    sh.add_argument("episode", help="episode id or guid")
    sh.add_argument("--out", help="write to a file instead of stdout")
    sh.set_defaults(func=cmd_show)

    se = sub.add_parser("search", help="full-text search across transcripts")
    se.add_argument("query")
    se.add_argument("--limit", type=int, default=20)
    se.set_defaults(func=cmd_search)

    rm = sub.add_parser("remerge", help="redo speaker merge from cached model output")
    rm.add_argument("episode", help="episode id or guid")
    rm.set_defaults(func=cmd_remerge)

    cal = sub.add_parser("calibrate", help="fit a provisional speaker identity probability")
    cal.add_argument("--feed", type=int, help="feed id; defaults to the On The Couch feed")
    cal.add_argument("--identity", default="Sigmund Bloom")
    cal.set_defaults(func=cmd_calibrate)

    profiles = sub.add_parser("profiles", help="rebuild show-level speaker voice profiles")
    profiles.add_argument("--feed", type=int, help="feed id; defaults to the On The Couch feed")
    profiles.set_defaults(func=cmd_profiles)

    ident = sub.add_parser("identify", help="show voice matches and guest-name hints")
    ident.add_argument("--feed", type=int, help="feed id; defaults to the On The Couch feed")
    ident.add_argument("--llm", action="store_true", help="also ask DeepInfra for guest-name hints")
    ident.set_defaults(func=cmd_identify)

    sv = sub.add_parser("serve", help="serve the transcript change feed over HTTP")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8765)
    sv.add_argument(
        "--token",
        default=os.environ.get("RIPPERR_API_TOKEN"),
        help="optional bearer token (or set RIPPERR_API_TOKEN)",
    )
    sv.add_argument("--emit-current", action="store_true",
                    help="enqueue current completed episodes for bootstrap")
    sv.set_defaults(func=cmd_serve)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        rip = Ripperr()
    except ValueError as exc:
        print(f"ripperr: {exc}", file=sys.stderr)
        return 2
    with rip:
        return args.func(args, rip)


if __name__ == "__main__":
    raise SystemExit(main())
