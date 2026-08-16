#!/usr/bin/env python3
"""
discover.py — find CHANNELS worth allowlisting, not videos.

Put this next to harvest.py in vatsalya-content/ and run it whenever you
want to grow the library.

    set YOUTUBE_API_KEY=your-key
    python discover.py stories

It searches YouTube for videos matching your queries, then works backwards
to the channels publishing them. A channel that appears across several
different queries and has hundreds of uploads is exactly what you want in
channels.yaml — one line there brings its entire back catalogue in.

Why not just search for videos and use those directly?
    search.list costs 100 units and returns at most 50 results per query,
    with no statistics attached. Harvesting a channel's uploads playlist
    costs 1 unit per 50 videos AND returns view counts, duration and
    embeddability. So: search once to find the channel, then harvest it
    cheaply forever.

Quota: 100 units per query. The default query sets are ~20 queries, about
2,000 units against a 10,000/day budget. Run all three categories in a day
and you'll still have room to spare.
"""

import os
import sys
import time
from collections import defaultdict

import requests

API = "https://www.googleapis.com/youtube/v3"
KEY = os.environ.get("YOUTUBE_API_KEY")

# ---------------------------------------------------------------------------
# Queries. This is the part worth editing — and the part an LLM is actually
# useful for. Ask one for "30 ways Hindi/Marathi/Gujarati YouTubers title
# garbh sanskar story videos" and paste the good ones in here. The API then
# turns those guesses into real, verifiable channels.
# ---------------------------------------------------------------------------

QUERIES = {
    "stories": [
        "garbh sanskar story",
        "garbh sanskar katha",
        "गर्भ संस्कार कहानी",
        "गर्भ संस्कार कथा",
        "pregnancy story for baby hindi",
        "ramayan katha for pregnancy",
        "mahabharat katha garbh sanskar",
        "panchatantra kahani for baby",
        "जातक कथा बच्चों के लिए",
        "prenatal storytelling hindi",
        "garbh samvad story",
        "बाल कहानी संस्कार",
    ],
    "mantras": [
        "garbh sanskar mantra",
        "pregnancy mantra hindi",
        "गर्भ संस्कार मंत्र",
        "santan gopal mantra",
        "garbh raksha mantra",
        "vishnu sahasranamam for pregnancy",
        "gayatri mantra for pregnant women",
        "vedic mantra for baby in womb",
        "shloka for pregnancy",
        "गर्भवती महिला के लिए मंत्र",
        "devotional chanting pregnancy",
        "om chanting for baby",
    ],
    "meditation": [
        "pregnancy meditation hindi",
        "garbh sanskar meditation",
        "womb meditation guided",
        "गर्भावस्था ध्यान",
        "prenatal relaxation music",
        "sleep music for pregnant women",
        "bonding meditation with baby",
        "calm music for pregnancy hindi",
        "yoga nidra pregnancy",
        "गर्भ संवाद ध्यान",
    ],
}


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def api(endpoint, _attempt=1, **params):
    """One call, with exponential backoff on rate limiting.

    429 is throttling (too fast), not quota exhaustion — that comes back
    as 403. Backing off and retrying is the correct response; giving up
    loses the whole run.
    """
    params["key"] = KEY
    r = requests.get(f"{API}/{endpoint}", params=params, timeout=30)

    if r.status_code == 403:
        die(f"Quota exceeded or key rejected.\n{r.text[:400]}")

    if r.status_code == 429 and _attempt <= 5:
        wait = 2 ** _attempt
        print(f"      rate limited — waiting {wait}s")
        time.sleep(wait)
        return api(endpoint, _attempt=_attempt + 1, **params)

    if not r.ok:
        # Never let requests' own exception surface — it embeds the full
        # URL, API key and all, straight into your terminal and any log
        # or bug report you paste it into.
        raise RuntimeError(f"{r.status_code} on {endpoint} — {r.reason}")
    return r.json()


def channels_from_query(q):
    """search.list -> the channels behind the top results.

    regionCode/relevanceLanguage bias results toward Indian uploaders,
    which matters a lot for this content — the same English query
    otherwise surfaces mostly US wellness channels.
    """
    data = api(
        "search",
        part="snippet",
        q=q,
        type="video",
        maxResults=50,
        regionCode="IN",
        relevanceLanguage="hi",
        order="relevance",
    )
    out = {}
    for item in data.get("items", []):
        s = item["snippet"]
        out[s["channelId"]] = s.get("channelTitle", "")
    return out


def channel_stats(channel_ids):
    """Uploads count and subscribers, 1 unit per 50 channels."""
    stats = {}
    ids = list(channel_ids)
    for i in range(0, len(ids), 50):
        data = api(
            "channels",
            part="snippet,statistics",
            id=",".join(ids[i:i + 50]),
            maxResults=50,
        )
        for item in data.get("items", []):
            st = item.get("statistics", {})
            stats[item["id"]] = {
                "title": item["snippet"]["title"],
                "country": item["snippet"].get("country", "??"),
                "videos": int(st.get("videoCount", 0) or 0),
                "subs": int(st.get("subscriberCount", 0) or 0),
                "hidden_subs": st.get("hiddenSubscriberCount", False),
            }
    return stats


# Channel names that disqualify regardless of stats. These aren't bad
# channels — they're the wrong channels. A kids' cartoon studio or an IVF
# clinic can rank well for "garbh sanskar katha" off one video, and with
# include_any empty their whole catalogue floods the shelf.
CHANNEL_DENY = [
    "cartoon", "toons", "kids", "rhymes", "nursery",
    "ivf", "clinic", "hospital", "fertility", "gynec",
    "dr ", "dr.", "doctor",          # we deliberately have no medical shelf
    "news", "tv", "movies", "comedy", "vlog", "podcast",
]


# Channels are auto-approved on these thresholds. Deliberately strict:
# a channel that clears all four is a real, sustained publisher, and every
# individual video still faces the deny list in harvest.py.
AUTO_APPROVE = {
    "min_videos": 80,      # a real catalogue, not a hobby account
    "min_subs": 20000,     # an audience that stuck around
    "min_hits": 3,          # found by 3+ different queries = genuine hub
    "max_videos": 3000,        
}


def append_to_yaml(category, rows, path="channels.yaml"):
    """Writes approved channel IDs into channels.yaml under the category.

    Idempotent — re-running never duplicates a channel.
    """
    import re as _re

    text = open(path, encoding="utf-8").read()

    approved = [
        r for r in rows
        if not any(bad in r["title"].lower() for bad in CHANNEL_DENY)
        and r["videos"] >= AUTO_APPROVE["min_videos"]
        and r["videos"] <= AUTO_APPROVE["max_videos"]
        and r["subs"] >= AUTO_APPROVE["min_subs"]
        and r["hits"] >= AUTO_APPROVE["min_hits"]
    ]
    new = [r for r in approved if r["id"] not in text]

    if not new:
        print(f"  nothing new to add for '{category}'")
        return 0

    # Find "- id: <category>" then its "channels:" line, and insert after.
    pattern = _re.compile(
        rf"(^\s*- id:\s*{_re.escape(category)}\s*$.*?^(\s*)channels:\s*$)",
        _re.MULTILINE | _re.DOTALL,
    )
    m = pattern.search(text)
    if not m:
        print(f"  !! could not find 'channels:' under '{category}' — "
              f"add these by hand")
        for r in new:
            print(f"      - {r['id']}   # {r['title'][:45]}")
        return 0

    indent = m.group(2) + "  "
    block = "".join(
        f"\n{indent}- {r['id']}   # {r['title'][:45]} ({r['videos']} videos)"
        for r in new
    )
    text = text[:m.end(1)] + block + text[m.end(1):]
    open(path, "w", encoding="utf-8").write(text)

    print(f"  added {len(new)} channels to '{category}' in {path}")
    for r in new:
        print(f"      {r['title'][:45]}  ({r['videos']} videos, "
              f"{r['subs']:,} subs)")
    return len(new)


def main():
    if not KEY:
        die("YOUTUBE_API_KEY is not set.")

    which = sys.argv[1] if len(sys.argv) > 1 else None
    if which and which not in QUERIES:
        die(f"Unknown category '{which}'. Options: {', '.join(QUERIES)}")

    categories = [which] if which else list(QUERIES)

    for cat in categories:
        queries = QUERIES[cat]
        print(f"\n{'=' * 78}\n{cat.upper()}  —  {len(queries)} queries, "
              f"~{len(queries) * 100} quota units\n{'=' * 78}")

        # How many different queries surfaced each channel. A channel found
        # by one query might be a fluke; found by six, it's a genuine hub
        # for this topic.
        hits = defaultdict(int)
        names = {}

        for q in queries:
            # One throttled query shouldn't cost you the whole run — the
            # other eleven results are still worth having.
            try:
                found = channels_from_query(q)
            except RuntimeError as e:
                print(f"  {q:<45} skipped ({e})")
                time.sleep(5)
                continue
            for cid, title in found.items():
                hits[cid] += 1
                names[cid] = title
            print(f"  {q:<45} {len(found):3d} channels")
            time.sleep(5)   # be a good citizen; search is expensive for them

        stats = channel_stats(hits.keys())

        rows = []
        for cid, n in hits.items():
            s = stats.get(cid, {})
            rows.append({
                "id": cid,
                "title": s.get("title", names.get(cid, "?")),
                "hits": n,
                "videos": s.get("videos", 0),
                "subs": s.get("subs", 0),
                "country": s.get("country", "??"),
            })

        # Rank by how many queries found it, then by catalogue size —
        # a big library is what turns one yaml line into hundreds of videos.
        rows.sort(key=lambda r: (-r["hits"], -r["videos"]))

        print(f"\n  {len(rows)} distinct channels found\n")
        print(f"  {'hits':>4}  {'uploads':>7}  {'subs':>9}  {'cc':2}  "
              f"{'channel':<38}  id")
        print(f"  {'-' * 4}  {'-' * 7}  {'-' * 9}  --  {'-' * 38}  {'-' * 24}")

        for r in rows[:40]:
            print(f"  {r['hits']:>4}  {r['videos']:>7}  {r['subs']:>9,}  "
                  f"{r['country']:2}  {r['title'][:38]:<38}  {r['id']}")

        # Ready to paste. Filtered to channels with a real catalogue —
        # a 12-video channel isn't worth an allowlist slot.
        added = append_to_yaml(cat, rows)
        pool = sum(r["videos"] for r in rows
                   if r["videos"] >= AUTO_APPROVE["min_videos"]
                   and r["videos"] <= AUTO_APPROVE["max_videos"]
                   and r["subs"] >= AUTO_APPROVE["min_subs"]
                   and r["hits"] >= AUTO_APPROVE["min_hits"])
        print(f"\n  Potential pool: ~{pool:,} videos before filtering.")

    print("\nChannels appended to channels.yaml. Run harvest.py next.\n"
          "Every video still passes the deny list in harvest.py before it\n"
          "reaches the app. Skim the top 20 of each shelf after harvesting —\n"
          "that's ten minutes and it's the only review that stays yours.\n")


if __name__ == "__main__":
    main()
