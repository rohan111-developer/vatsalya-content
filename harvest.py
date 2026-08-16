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


# ---------------------------------------------------------------------------
# SAFETY DENY LIST
#
# Applied to title AND description, case-insensitively, across every
# category. A single match rejects the video outright — no scoring, no
# second chance.
#
# This exists because ranking by engagement optimises for popular, and in
# this niche a lot of what's popular is harmful. These are not edge cases;
# they are the genres that fill an unfiltered garbh sanskar shelf.
# ---------------------------------------------------------------------------

DENY = {
    # ---- ILLEGAL IN INDIA ---------------------------------------------
    # The PCPNDT Act 1994 prohibits communicating foetal sex. "Predict your
    # baby's gender" is a huge genre on YouTube India. Distributing it
    # through an app aimed at Indian pregnant women is not a risk worth
    # any amount of engagement.
    "legal": [
        "boy or girl", "gender predict", "gender reveal", "baby gender",
        "ladka ya ladki", "ladka hoga", "ladki hogi", "beta hoga",
        "लड़का या लड़की", "लड़का होगा", "लड़की होगी", "बेटा होगा",
        "गर्भ में लड़का", "गर्भ में लड़की", "लिंग परीक्षण",
        "boy symptoms", "girl symptoms", "male baby", "female baby",
    ],

    # ---- SON PREFERENCE -----------------------------------------------
    # Mantras and rituals promising a son. Popular, well-ranked, and
    # something no app for expecting mothers should carry.
    "son_preference": [
        "putra prapti", "putr prapti", "पुत्र प्राप्ति", "पुत्र प्राप्ति मंत्र",
        "son mantra", "mantra for son", "for baby boy", "baby boy mantra",
        "वंश", "kul deepak",
    ],

    # ---- COLOURISM ----------------------------------------------------
    # The "fair baby" genre — foods, mantras and rituals for lighter skin.
    # Enormously popular. Actively harmful.
    "colourism": [
        "gora baccha", "gora bacha", "fair baby", "fair complexion",
        "गोरा बच्चा", "गोरा होगा", "रंग गोरा", "fairness",
        "skin colour of baby", "baby colour", "white baby",
    ],

    # ---- MEDICAL AND OUTCOME CLAIMS ------------------------------------
    # We removed the medical category precisely to avoid this. Claims about
    # intelligence, guaranteed outcomes and cures don't get in by the back
    # door via a "story" or "mantra".
    "claims": [
        "guaranteed", "100% result", "100% working", "गारंटी",
        "increase iq", "iq badhaye", "intelligent baby", "genius baby",
        "smart baby banaye", "cure", "ilaj", "इलाज", "treatment",
        "medicine", "दवा", "instant result", "chamatkari", "चमत्कारी",
        "miracle",
    ],

    # ---- FEAR AND DISTRESS ---------------------------------------------
    # She may be watching at 2am, alone, already anxious. Nothing in this
    # app should raise her heart rate.
    "fear": [
        "miscarriage", "गर्भपात", "abortion", "danger", "खतरा",
        "warning", "चेतावनी", "mistake", "galti", "गलती",
        "never do", "bhulkar bhi", "भूलकर भी", "risk", "problem",
        "complication", "death", "मृत्यु", "shocking", "चौंकाने",
    ],

    # ---- SELLING -------------------------------------------------------
    # Course pitches, workshop ads, consultation funnels. She came for a
    # story, not a sales call.
    "selling": [
        "course", "workshop", "admission", "fees", "enroll", "enrol",
        "join now", "call now", "whatsapp", "book now", "consultation",
        "appointment", "discount", "offer price", "limited time",
    ],

    # ---- CLICKBAIT AND LOW EFFORT --------------------------------------
    "clickbait": [
        "must watch", "viral video", "trending", "shorts", "#shorts",
        "reaction", "vlog", "prank", "challenge", "unboxing",
        "part 2 link", "subscribe now",
    ],
}

# Flattened once at import rather than per video — this runs against
# thousands of titles.
DENY_FLAT = [(term.lower(), group)
             for group, terms in DENY.items()
             for term in terms]


def deny_reason(title, description):
    """Returns the deny group that matched, or None."""
    haystack = f"{title} {description}".lower()
    for term, group in DENY_FLAT:
        if term in haystack:
            return f"deny:{group}"
    return None


def looks_like_clickbait(title):
    """Structural signals, no keywords needed.

    Shouty titles and emoji walls correlate strongly with the kind of
    content we're already excluding by keyword — this catches the ones
    that phrase it differently.
    """
    letters = [c for c in title if c.isalpha() and c.isascii()]
    if len(letters) >= 12:
        caps = sum(1 for c in letters if c.isupper()) / len(letters)
        if caps > 0.6:
            return True
    if title.count("!") >= 3 or title.count("?") >= 3:
        return True
    # Emoji and symbol pile-ups
    exotic = sum(1 for c in title if ord(c) > 0x2100)
    if exotic >= 4:
        return True
    return False

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

    # Safety gates come before preference filters — an excluded video is
    # excluded regardless of how well it performs.
    raw_title = snip.get("title") or ""
    desc = (snip.get("description") or "")[:600]

    reason = deny_reason(raw_title, desc)
    if reason:
        return reason

    if looks_like_clickbait(raw_title):
        return "clickbait shape"

    # Engagement quality. A video with plenty of views but a very low like
    # rate is usually one people clicked and regretted. 0.5% is well below
    # normal — typical is 2–5%.
    likes = v.get("statistics", {}).get("likeCount")
    if likes is not None and views > 20000:
        if int(likes) / views < 0.005:
            return "poor like ratio"

    # Comments disabled on a channel that has them elsewhere is a weak
    # signal, but zero comments on a high-view video usually means the
    # uploader turned them off after trouble.
    comments = v.get("statistics", {}).get("commentCount")
    if comments is not None and views > 100000 and int(comments) == 0:
        return "comments disabled at scale"

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
