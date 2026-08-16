#!/usr/bin/env python3
"""
Harvest videos from allowlisted YouTube channels, rank them, publish JSON.

Quota economics, which drive the whole design:
    search.list        100 units per call, max 50 results   <- never used
    channels.list        1 unit  per call, up to 50 ids
    playlistItems.list   1 unit  per call, up to 50 videos
    videos.list          1 unit  per call, up to 50 ids

Harvesting via each channel's "uploads" playlist costs about 1 unit per 50
videos. A 5,000-video run is roughly 200 units against a 10,000/day budget.
Using search.list for the same job would cost 10,000 and return less.
"""

import hashlib
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

API = "https://www.googleapis.com/youtube/v3"
KEY = os.environ.get("YOUTUBE_API_KEY")
ROOT = Path(__file__).parent
OUT = ROOT / "data"

ISO_DURATION = re.compile(
    r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?"
)


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def api(endpoint, **params):
    params["key"] = KEY
    r = requests.get(f"{API}/{endpoint}", params=params, timeout=30)
    if r.status_code == 403:
        die(f"Quota exceeded or key rejected.\n{r.text[:400]}")
    r.raise_for_status()
    return r.json()


def parse_duration(iso):
    """PT1H2M10S -> seconds."""
    m = ISO_DURATION.match(iso or "")
    if not m:
        return 0
    d, h, mi, s = (int(x) if x else 0 for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def uploads_playlists(channel_ids):
    """Every channel has a hidden 'uploads' playlist containing all videos."""
    out = {}
    for i in range(0, len(channel_ids), 50):
        chunk = channel_ids[i:i + 50]
        data = api("channels", part="contentDetails", id=",".join(chunk),
                   maxResults=50)
        for item in data.get("items", []):
            out[item["id"]] = (
                item["contentDetails"]["relatedPlaylists"]["uploads"]
            )
    return out


def playlist_video_ids(playlist_id, cap):
    ids, token = [], None
    while len(ids) < cap:
        data = api("playlistItems", part="contentDetails",
                   playlistId=playlist_id, maxResults=50, pageToken=token)
        for item in data.get("items", []):
            vid = item["contentDetails"].get("videoId")
            if vid:
                ids.append(vid)
        token = data.get("nextPageToken")
        if not token:
            break
    return ids[:cap]


def video_details(video_ids):
    out = []
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i:i + 50]
        data = api("videos",
                   part="snippet,contentDetails,statistics,status",
                   id=",".join(chunk), maxResults=50)
        out.extend(data.get("items", []))
    return out


def passes(v, cfg, blocked):
    """Everything that would make a video useless or unplayable."""
    vid = v["id"]
    if vid in blocked:
        return False

    status = v.get("status", {})
    # The big one: uploaders can disable embedding, and such a video shows a
    # perfectly good thumbnail while refusing to play inside the app.
    if not status.get("embeddable", False):
        return False
    if status.get("privacyStatus") != "public":
        return False

    snip = v.get("snippet", {})
    if snip.get("liveBroadcastContent", "none") != "none":
        return False

    secs = parse_duration(v.get("contentDetails", {}).get("duration"))
    if not (cfg["min_duration_seconds"] <= secs <= cfg["max_duration_seconds"]):
        return False

    stats = v.get("statistics", {})
    views = int(stats.get("viewCount", 0) or 0)
    if views < cfg["min_views"]:
        return False

    title = (snip.get("title") or "").lower()
    inc = [k.lower() for k in cfg.get("include_any") or []]
    exc = [k.lower() for k in cfg.get("exclude_any") or []]
    if inc and not any(k in title for k in inc):
        return False
    if any(k in title for k in exc):
        return False

    return True


def score(videos):
    """Rank within a category by views, like rate and recency.

    Percentile-normalised rather than absolute, because a mantra channel and
    a story channel operate at completely different view scales and one
    shouldn't bury the other.

    likeCount is now hidden on a large share of videos. Missing likes score
    as the category median instead of zero — absence of data is not evidence
    of a bad video.
    """
    if not videos:
        return

    now = datetime.now(timezone.utc)

    for v in videos:
        v["_views"] = math.log10(v["views"] + 10)
        v["_age_days"] = max(
            1, (now - datetime.fromisoformat(
                v["publishedAt"].replace("Z", "+00:00"))).days
        )
        v["_recency"] = 1.0 / (1.0 + v["_age_days"] / 900.0)
        v["_like_rate"] = (
            (v["likes"] / v["views"]) if v["likes"] and v["views"] else None
        )

    rates = sorted(x["_like_rate"] for x in videos
                   if x["_like_rate"] is not None)
    median_rate = rates[len(rates) // 2] if rates else 0.02
    for v in videos:
        if v["_like_rate"] is None:
            v["_like_rate"] = median_rate

    def pct(key):
        ordered = sorted(videos, key=lambda x: x[key])
        n = max(1, len(ordered) - 1)
        for i, v in enumerate(ordered):
            v[key + "_p"] = i / n

    pct("_views")
    pct("_like_rate")
    pct("_recency")

    for v in videos:
        v["score"] = round(
            0.60 * v["_views_p"]
            + 0.25 * v["_like_rate_p"]
            + 0.15 * v["_recency_p"],
            6,
        )
        for k in list(v):
            if k.startswith("_"):
                del v[k]


def main():
    if not KEY:
        die("YOUTUBE_API_KEY is not set.")

    cfg = yaml.safe_load((ROOT / "channels.yaml").read_text(encoding="utf-8"))
    defaults = cfg.get("defaults", {})

    blocked = set()
    bl = ROOT / "blocklist.txt"
    if bl.exists():
        for line in bl.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                blocked.add(line)

    OUT.mkdir(exist_ok=True)
    index_categories = []

    for cat in cfg["categories"]:
        merged = {**defaults, **{k: v for k, v in cat.items()
                                 if k in defaults or k.startswith("include")
                                 or k.startswith("exclude")}}
        merged.setdefault("include_any", cat.get("include_any"))
        merged.setdefault("exclude_any", cat.get("exclude_any"))

        print(f"\n== {cat['id']} ==")
        uploads = uploads_playlists(cat["channels"])
        print(f"   {len(uploads)} channels resolved")

        all_ids = []
        for ch, pl in uploads.items():
            ids = playlist_video_ids(pl, merged["max_per_channel"])
            print(f"   {ch}: {len(ids)} uploads")
            all_ids.extend(ids)

        all_ids = list(dict.fromkeys(all_ids))  # dedupe, keep order
        print(f"   {len(all_ids)} unique videos, fetching details...")

        kept = []
        for v in video_details(all_ids):
            if not passes(v, merged, blocked):
                continue
            snip, stats = v["snippet"], v.get("statistics", {})
            kept.append({
                "id": v["id"],
                "title": snip["title"],
                "channel": snip.get("channelTitle", ""),
                "publishedAt": snip["publishedAt"],
                "seconds": parse_duration(v["contentDetails"]["duration"]),
                "views": int(stats.get("viewCount", 0) or 0),
                "likes": int(stats["likeCount"]) if stats.get("likeCount")
                         else None,
            })

        print(f"   {len(kept)} passed filtering")
        score(kept)
        kept.sort(key=lambda x: x["score"], reverse=True)

        payload = json.dumps(
            {"id": cat["id"], "videos": kept},
            ensure_ascii=False, separators=(",", ":"),
        )
        (OUT / f"{cat['id']}.json").write_text(payload, encoding="utf-8")

        index_categories.append({
            "id": cat["id"],
            "title": cat["title"],
            "blurb": cat.get("blurb", ""),
            "icon": cat.get("icon", "play_circle"),
            "accent": cat.get("accent", "sky"),
            "note": cat.get("note"),
            "count": len(kept),
            "file": f"{cat['id']}.json",
            # The app compares this to what it last synced and skips the
            # download entirely when nothing has changed.
            "checksum": hashlib.sha256(payload.encode()).hexdigest()[:16],
        })

    index = {
        "version": 2,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "categories": index_categories,
    }
    (OUT / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

    total = sum(c["count"] for c in index_categories)
    print(f"\nDone. {total} videos across {len(index_categories)} categories.")


if __name__ == "__main__":
    main()
