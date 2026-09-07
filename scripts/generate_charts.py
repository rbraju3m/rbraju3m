#!/usr/bin/env python3
"""Render every profile chart directly from GitHub's public data.

Design rules for this file:

* No third-party render service. github-readme-stats.vercel.app (503) and
  github-profile-trophy.vercel.app (402 DEPLOYMENT_DISABLED) are both dead, and
  relying on them is what left the README full of broken images.
* No personal access token. Everything here works unauthenticated; GITHUB_TOKEN
  is used only to raise the REST rate limit when running in Actions.
* One question per chart. Two cards that answer the same question are a
  duplicate even when they are drawn differently, so e.g. "top languages" and
  "languages by bytes" are a single card here, not two.

Usage: python3 scripts/generate_charts.py <login> <out_dir>
"""

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import Counter, OrderedDict, defaultdict
from datetime import datetime, timedelta, timezone

API = "https://api.github.com"
TZ = timezone(timedelta(hours=6))  # Asia/Dhaka

# ------------------------------------------------------------------- theme

BG = "#0f0d1a"
PANEL = "#171430"
BORDER = "#2b2740"
TITLE = "#fe428e"
TEXT = "#eaf6ff"
MUTED = "#8b88a8"
PINK = "#fe428e"
CYAN = "#33d6f0"
VIOLET = "#7f5af0"
HEAT = ["#1c1930", "#4b2a52", "#7d2f68", "#c03a7e", "#fe428e"]
SERIES = [PINK, CYAN, "#f8d847", VIOLET, "#2cb67d", "#ff8906",
          "#e53170", "#4cc9f0", "#b8c1ec", "#a786df"]

FONT = "'Segoe UI', Ubuntu, Sans-Serif"
# weight, size, fill - inline attributes, because several SVG renderers (IDE
# markdown previews in particular) ignore an in-document CSS <style> block and
# fall back to a default font size that destroys every layout.
FACES = {
    "h1": (700, 34, TEXT),
    "h2": (400, 15, MUTED),
    "big": (700, 26, TEXT),
    "t": (600, 18, TITLE),
    "v": (600, 13, TEXT),
    "l": (400, 12, MUTED),
    "xs": (600, 11, TEXT),
}

WIDE = 900
HALF = 438

# GitHub's REST languages endpoint returns no colours, so keep a map of the
# ones that actually show up and fall back to the SERIES palette.
LANG_COLORS = {
    "PHP": "#8993be", "JavaScript": "#f1e05a", "TypeScript": "#3178c6",
    "HTML": "#e34c26", "CSS": "#563d7c", "SCSS": "#c6538c", "Less": "#2b7489",
    "Blade": "#f7523f", "Python": "#3572A5", "Shell": "#89e051",
    "Java": "#b07219", "C#": "#178600", "Ruby": "#701516", "Go": "#00ADD8",
    "Dart": "#00B4AB", "Vue": "#41b883", "CoffeeScript": "#244776",
    "Dockerfile": "#384d54", "Makefile": "#427819", "Twig": "#c1d026",
    "Hack": "#878787", "ApacheConf": "#d12127", "Batchfile": "#C1F12E",
    "Procfile": "#a0a0a0", "SQL": "#e38c00", "C": "#555555", "C++": "#f34b7d",
}


def avatar_data_uri(url, size=200):
    """Fetch the avatar and inline it as a data URI.

    It has to be embedded: GitHub serves README images through camo, and an
    <image> inside an SVG pointing at an external host is blocked, so a linked
    avatar renders as an empty circle.
    """
    if not url:
        return None
    joiner = "&" if "?" in url else "?"
    try:
        req = urllib.request.Request(url + "%ss=%d" % (joiner, size),
                                     headers={"User-Agent": "profile-charts"})
        with urllib.request.urlopen(req, timeout=45) as resp:
            kind = resp.headers.get("Content-Type", "image/jpeg").split(";")[0]
            raw = resp.read()
    except Exception as exc:                      # never fatal - hero degrades
        print("warning: avatar unavailable (%s)" % exc, file=sys.stderr)
        return None
    if not kind.startswith("image/"):
        kind = "image/jpeg"
    return "data:%s;base64,%s" % (kind, base64.b64encode(raw).decode("ascii"))


def lang_color(name, fallback=0):
    return LANG_COLORS.get(name) or SERIES[fallback % len(SERIES)]


# ---------------------------------------------------------------- transport

def _open(url, headers=None):
    hdrs = {"User-Agent": "profile-charts", "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        hdrs["Authorization"] = "Bearer " + token
    hdrs.update(headers or {})
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", "replace")


class ApiError(RuntimeError):
    """A fetch failed, so the data is incomplete."""


def rest(path):
    """Fetch JSON, or raise.

    Deliberately strict. An earlier version returned None on error, which meant
    a rate-limited language lookup silently became "this repo has no
    languages" - the charts still rendered, just wrong, and overwrote good
    ones. Failing here aborts the run before anything is written.
    """
    try:
        return json.loads(_open(API + path))
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429):
            raise ApiError("rate limited on %s (HTTP %s)" % (path, exc.code))
        raise ApiError("HTTP %s on %s" % (exc.code, path))
    except urllib.error.URLError as exc:
        raise ApiError("network error on %s: %s" % (path, exc))


# ------------------------------------------------------------------ fetchers

def fetch_user(login):
    return rest("/users/" + login) or {}


def fetch_calendar(login):
    """Contribution calendar scraped from the public profile page.

    Deliberately not GraphQL: contributionsCollection queried with a
    repo-scoped GITHUB_TOKEN returns *public* contributions only. This account
    shows 2.8k contributions but only ~266 are public, so GraphQL would render
    a near-empty calendar. The profile page honours the account's "include
    private contributions" setting, which is what a profile should show.
    """
    html = _open("https://github.com/users/%s/contributions" % login,
                 headers={"Accept": "text/html"})
    ids = {cid: day for day, cid in re.findall(
        r'data-date="(\d{4}-\d{2}-\d{2})"\s+id="(contribution-day-component-\d+-\d+)"',
        html)}
    counts = {}
    for cid, label in re.findall(
            r'<tool-tip[^>]*for="(contribution-day-component-\d+-\d+)"[^>]*>([^<]*)</tool-tip>',
            html):
        match = re.match(r"(No|[\d,]+) contribution", label)
        if cid in ids and match:
            raw = match.group(1)
            counts[ids[cid]] = 0 if raw == "No" else int(raw.replace(",", ""))
    if not counts:
        raise RuntimeError("could not parse the contribution calendar for " + login)

    weeks, week = [], []
    for day in sorted(counts):
        weekday = (datetime.strptime(day, "%Y-%m-%d").weekday() + 1) % 7  # Sun=0
        if weekday == 0 and week:
            weeks.append(week)
            week = []
        week.append({"date": day, "count": counts[day], "weekday": weekday})
    if week:
        weeks.append(week)
    return {"total": sum(counts.values()), "weeks": weeks, "days": counts}


def fetch_repos(login):
    repos = []
    for page in range(1, 4):
        batch = rest("/users/%s/repos?per_page=100&type=owner&page=%d" % (login, page))
        if not batch:
            break
        for repo in batch:
            if repo.get("fork"):
                continue
            langs = rest("/repos/%s/languages" % repo["full_name"]) or {}
            repos.append({
                "name": repo["name"],
                "created": repo.get("created_at", "")[:10],
                "pushed": repo.get("pushed_at", "")[:10],
                "stars": repo.get("stargazers_count", 0),
                "forks": repo.get("forks_count", 0),
                "primary": repo.get("language"),
                "langs": langs,
                "bytes": sum(langs.values()),
            })
    return repos


def fetch_events(login, pages=3):
    events = []
    for page in range(1, pages + 1):
        batch = rest("/users/%s/events/public?per_page=100&page=%d" % (login, page))
        if not batch:
            break
        events.extend(batch)
    return events


# ------------------------------------------------------------------ analysis

def streaks(days):
    """Current streak, longest streak and active-day count from the calendar."""
    ordered = sorted(days)
    longest = run = 0
    for day in ordered:
        run = run + 1 if days[day] > 0 else 0
        longest = max(longest, run)

    # A zero on today only means the day is not over yet, so start from
    # yesterday when today is still empty.
    current = 0
    for day in reversed(ordered):
        if days[day] > 0:
            current += 1
        elif current or day != ordered[-1]:
            break
    active = sum(1 for value in days.values() if value > 0)
    return current, longest, active


# -------------------------------------------------------------------- render

def esc(value):
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def human(n):
    n = float(n)
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(n) >= limit:
            return ("%.1f" % (n / limit)).rstrip("0").rstrip(".") + suffix
    return "%d" % n


def txt(x, y, content, kind="l", anchor=None):
    weight, size, fill = FACES[kind]
    align = ' text-anchor="%s"' % anchor if anchor else ""
    return ('<text x="%s" y="%s" font-family="%s" font-size="%d" font-weight="%d" '
            'fill="%s"%s>%s</text>' % (x, y, FONT, size, weight, fill, align, content))


def svg(width, height, body, defs="", label=""):
    return ('<svg width="%d" height="%d" viewBox="0 0 %d %d" '
            'xmlns="http://www.w3.org/2000/svg" '
            'xmlns:xlink="http://www.w3.org/1999/xlink" '
            'role="img" aria-label="%s">\n'
            '%s\n%s\n</svg>\n'
            % (width, height, width, height, esc(label),
               "<defs>%s</defs>" % defs if defs else "", body))


def card(width, height, title, body, defs="", subtitle=None):
    parts = ['<rect x="0.5" y="0.5" width="%d" height="%d" rx="12" fill="%s" '
             'stroke="%s"/>' % (width - 1, height - 1, BG, BORDER),
             '<rect x="24" y="22" width="4" height="16" rx="2" fill="%s"/>' % PINK,
             txt(38, 35, esc(title), "t")]
    if subtitle:
        parts.append(txt(38, 55, esc(subtitle), "l"))
    parts.append(body)
    return svg(width, height, "\n".join(parts), defs, title)


def write(out_dir, name, content):
    path = os.path.join(out_dir, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
    print("wrote %s (%d bytes)" % (path, len(content)))


def bar_gradient(gid, colour):
    return ('<linearGradient id="%s" x1="0" y1="0" x2="1" y2="0">'
            '<stop offset="0" stop-color="%s" stop-opacity="0.55"/>'
            '<stop offset="1" stop-color="%s"/></linearGradient>'
            % (gid, colour, colour))


# --------------------------------------------------------------------- charts
# Each chart answers one question no other chart answers.

def chart_hero(user, repos, out_dir):
    """Branding banner: avatar, name, role, stack. Carries no statistics -
    those live in the summary card, and repeating them here is a duplicate."""
    height = 220
    name = re.sub(r"\s+\.", ".", user.get("name") or user.get("login") or "")
    role = user.get("bio") or "Full Stack Software Engineer"

    tally = Counter()
    for repo in repos:
        for lang, size in repo["langs"].items():
            tally[lang] += size
    chips = [lang for lang, _ in tally.most_common(6)]

    avatar = avatar_data_uri(user.get("avatar_url"))
    ax, ay, ar = 116, 110, 62          # avatar centre and radius
    text_x = ax + ar + 42

    defs = ('<linearGradient id="sweep" x1="0" y1="0" x2="1" y2="0">'
            '<stop offset="0" stop-color="%s">'
            '<animate attributeName="stop-color" values="%s;%s;%s;%s" '
            'dur="14s" repeatCount="indefinite"/></stop>'
            '<stop offset="1" stop-color="%s">'
            '<animate attributeName="stop-color" values="%s;%s;%s;%s" '
            'dur="14s" repeatCount="indefinite"/></stop>'
            '</linearGradient>'
            '<clipPath id="avclip"><circle cx="%d" cy="%d" r="%d"/></clipPath>'
            % (PINK, PINK, VIOLET, CYAN, PINK, CYAN, CYAN, PINK, VIOLET, CYAN,
               ax, ay, ar))

    parts = ['<rect x="0.5" y="0.5" width="%d" height="%d" rx="12" fill="%s" '
             'stroke="%s"/>' % (WIDE - 1, height - 1, BG, BORDER),
             '<rect x="1" y="1" width="%d" height="6" fill="url(#sweep)"/>' % (WIDE - 2)]

    if avatar:
        parts.append('<image x="%d" y="%d" width="%d" height="%d" '
                     'clip-path="url(#avclip)" preserveAspectRatio="xMidYMid slice" '
                     'href="%s" xlink:href="%s"/>'
                     % (ax - ar, ay - ar, ar * 2, ar * 2, avatar, avatar))
    else:
        # No avatar available - fall back to initials so the layout still reads.
        initials = "".join(word[0] for word in name.split()[:2]).upper() or "?"
        parts.append('<circle cx="%d" cy="%d" r="%d" fill="%s"/>' % (ax, ay, ar, PANEL))
        parts.append('<text x="%d" y="%d" font-family="%s" font-size="42" '
                     'font-weight="700" fill="%s" text-anchor="middle">%s</text>'
                     % (ax, ay + 15, FONT, PINK, esc(initials)))

    parts.append('<circle cx="%d" cy="%d" r="%d" fill="none" stroke="url(#sweep)" '
                 'stroke-width="3"/>' % (ax, ay, ar + 5))

    parts.append(txt(text_x, 96, esc(name), "h1"))
    parts.append(txt(text_x, 124, esc(role), "h2"))

    x = text_x
    for i, chip in enumerate(chips):
        width = 16 + len(chip) * 7.6
        colour = lang_color(chip, i)
        parts.append('<rect x="%.1f" y="148" width="%.1f" height="28" rx="14" '
                     'fill="%s" opacity="0.16"/>' % (x, width, colour))
        parts.append('<rect x="%.1f" y="148" width="%.1f" height="28" rx="14" '
                     'fill="none" stroke="%s" opacity="0.5"/>' % (x, width, colour))
        parts.append('<text x="%.1f" y="166" font-family="%s" font-size="12" '
                     'font-weight="600" fill="%s" text-anchor="middle">%s</text>'
                     % (x + width / 2, FONT, colour, esc(chip)))
        x += width + 10

    write(out_dir, "hero.svg", svg(WIDE, height, "\n".join(parts), defs, name))


def chart_summary(user, cal, repos, out_dir):
    """Headline numbers. The only card that states totals."""
    height = 150
    current, longest, active = streaks(cal["days"])
    tiles = [
        ("Contributions", human(cal["total"]), "past year"),
        ("Current streak", "%d" % current, "days"),
        ("Longest streak", "%d" % longest, "days"),
        ("Active days", "%d" % active, "of %d" % len(cal["days"])),
        ("Repositories", "%d" % len(repos), "public, owned"),
        ("Followers", human(user.get("followers", 0)), "on GitHub"),
    ]

    parts = ['<rect x="0.5" y="0.5" width="%d" height="%d" rx="12" fill="%s" '
             'stroke="%s"/>' % (WIDE - 1, height - 1, BG, BORDER)]
    slot = WIDE / len(tiles)
    for i, (label, value, note) in enumerate(tiles):
        cx = slot * i + slot / 2
        if i:
            parts.append('<line x1="%.1f" y1="34" x2="%.1f" y2="%d" stroke="%s" '
                         'opacity="0.7"/>' % (slot * i, slot * i, height - 34, BORDER))
        parts.append(txt("%.1f" % cx, 62, esc(label.upper()), "l", "middle"))
        parts.append('<text x="%.1f" y="100" font-family="%s" font-size="30" '
                     'font-weight="700" fill="%s" text-anchor="middle">%s</text>'
                     % (cx, FONT, PINK if i < 3 else TEXT, esc(value)))
        parts.append(txt("%.1f" % cx, 122, esc(note), "l", "middle"))

    write(out_dir, "summary.svg", svg(WIDE, height, "\n".join(parts), "", "Profile summary"))


def chart_heatmap(cal, out_dir):
    """Question: which individual days did I work?"""
    weeks = cal["weeks"]
    cell, gap = 13, 3
    step = cell + gap
    left, top = 46, 78
    width = max(WIDE, left + len(weeks) * step + 26)
    height = top + 7 * step + 56
    peak = max((d["count"] for w in weeks for d in w), default=0)

    def level(count):
        if count <= 0 or peak <= 0:
            return 0
        return min(4, 1 + int((count - 1) * 4 / peak))

    parts, seen = [], set()
    for wi, week in enumerate(weeks):
        for day in week:
            when = datetime.strptime(day["date"], "%Y-%m-%d")
            x = left + wi * step
            y = top + day["weekday"] * step
            parts.append('<rect x="%d" y="%d" width="%d" height="%d" rx="3" fill="%s">'
                         '<title>%s: %d contributions</title></rect>'
                         % (x, y, cell, cell, HEAT[level(day["count"])],
                            day["date"], day["count"]))
            if when.day <= 7 and when.month not in seen:
                seen.add(when.month)
                parts.append(txt(x, top - 10, when.strftime("%b"), "l"))

    for row, label in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        parts.append(txt(38, top + row * step + 11, label, "l", "end"))

    ly = top + 7 * step + 28
    lx = width - 26 - 5 * step - 74
    parts.append(txt(lx, ly + 11, "Less", "l"))
    for i, colour in enumerate(HEAT):
        parts.append('<rect x="%d" y="%d" width="%d" height="%d" rx="3" fill="%s"/>'
                     % (lx + 34 + i * step, ly, cell, cell, colour))
    parts.append(txt(lx + 34 + 5 * step + 6, ly + 11, "More", "l"))
    parts.append(txt(38, ly + 11, "Busiest day: %d contributions" % peak, "v"))

    write(out_dir, "contribution-heatmap.svg",
          card(width, height, "Contribution Calendar",
               "\n".join(parts),
               subtitle="%s contributions in the last year" % human(cal["total"])))


def chart_trend(cal, out_dir):
    """Question: how has my output changed week to week?"""
    totals = [sum(d["count"] for d in w) for w in cal["weeks"]]
    if not totals:
        return
    height = 300
    left, right, top, bottom = 58, 26, 92, 48
    plot_w, plot_h = WIDE - left - right, height - top - bottom
    peak = max(totals) or 1
    span = max(len(totals) - 1, 1)

    px = lambda i: left + i * plot_w / span
    py = lambda v: top + plot_h - v * plot_h / peak

    defs = ('<linearGradient id="area" x1="0" y1="0" x2="0" y2="1">'
            '<stop offset="0" stop-color="%s" stop-opacity="0.5"/>'
            '<stop offset="1" stop-color="%s" stop-opacity="0.02"/></linearGradient>'
            % (PINK, PINK))

    parts = []
    for i in range(5):
        value = peak * (4 - i) / 4
        y = py(value)
        parts.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" '
                     'stroke-dasharray="3 5" opacity="0.4"/>'
                     % (left, y, WIDE - right, y, BORDER))
        parts.append(txt(left - 12, y + 4, human(round(value)), "l", "end"))

    points = " ".join("%.1f,%.1f" % (px(i), py(v)) for i, v in enumerate(totals))
    parts.append('<polygon fill="url(#area)" points="%.1f,%.1f %s %.1f,%.1f"/>'
                 % (left, top + plot_h, points, px(span), top + plot_h))
    parts.append('<polyline fill="none" stroke="%s" stroke-width="2.5" '
                 'stroke-linejoin="round" stroke-linecap="round" points="%s"/>'
                 % (PINK, points))

    last = None
    for i, week in enumerate(cal["weeks"]):
        when = datetime.strptime(week[0]["date"], "%Y-%m-%d")
        if when.month != last:
            last = when.month
            parts.append(txt("%.1f" % px(i), height - 20, when.strftime("%b"), "l", "middle"))

    best = max(range(len(totals)), key=lambda i: totals[i])
    parts.append('<circle cx="%.1f" cy="%.1f" r="5" fill="%s" stroke="%s" '
                 'stroke-width="2"/>' % (px(best), py(totals[best]), CYAN, BG))

    write(out_dir, "contribution-trend.svg",
          card(WIDE, height, "Weekly Momentum", "\n".join(parts), defs,
               subtitle="Peak week %d &#183; average %d per week"
                        % (totals[best], round(sum(totals) / len(totals)))))


def chart_consistency(cal, out_dir):
    """Question: how intense is a typical working day?

    Distribution, not a time series - the heatmap and trend already cover
    'when'. Quiet days are reported as a caption rather than a bar, because a
    zero bucket dwarfs everything else and hides the shape.
    """
    buckets = OrderedDict([("1-2", 0), ("3-5", 0), ("6-10", 0),
                           ("11-20", 0), ("21+", 0)])
    quiet = 0
    for count in cal["days"].values():
        if count <= 0:
            quiet += 1
        elif count <= 2:
            buckets["1-2"] += 1
        elif count <= 5:
            buckets["3-5"] += 1
        elif count <= 10:
            buckets["6-10"] += 1
        elif count <= 20:
            buckets["11-20"] += 1
        else:
            buckets["21+"] += 1
    if not any(buckets.values()):
        return

    height = 300
    left, top, plot_h = 40, 96, 128
    slot = (HALF - left * 2) / len(buckets)
    bar_w = min(slot - 14, 40)
    peak = max(buckets.values())
    active = sum(buckets.values())

    defs = bar_gradient("cbar", CYAN)
    parts = []
    for i, (label, count) in enumerate(buckets.items()):
        x = left + i * slot + (slot - bar_w) / 2
        bar_h = max(count / peak * plot_h, 3)
        y = top + plot_h - bar_h
        parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="5" '
                     'fill="url(#cbar)"><title>%s contributions: %d days</title></rect>'
                     % (x, y, bar_w, bar_h, label, count))
        parts.append(txt("%.1f" % (x + bar_w / 2), y - 8, "%d" % count, "xs", "middle"))
        parts.append(txt("%.1f" % (x + bar_w / 2), top + plot_h + 22, label, "l", "middle"))
    parts.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="%s"/>'
                 % (left, top + plot_h + 3, HALF - left, top + plot_h + 3, BORDER))
    parts.append(txt(38, height - 26,
                     "%d active days &#183; %d quiet &#183; %d%% of the year active"
                     % (active, quiet, round(active * 100.0 / max(len(cal["days"]), 1))),
                     "l"))

    write(out_dir, "consistency.svg",
          card(HALF, height, "Daily Intensity", "\n".join(parts), defs,
               subtitle="Days grouped by contributions made"))


def chart_languages(repos, out_dir):
    """Question: what do I actually write, and across how many projects?

    Bytes and repo count share one card on purpose: as two cards they would be
    two answers to the same question.
    """
    sizes, projects = Counter(), Counter()
    for repo in repos:
        for lang, size in repo["langs"].items():
            sizes[lang] += size
            projects[lang] += 1
    if not sizes:
        return

    top = sizes.most_common(9)
    total = sum(sizes.values())
    top_y, row_h = 92, 32
    height = top_y + len(top) * row_h + 30
    bar_x, bar_w = 136, 146

    defs = "".join(bar_gradient("lg%d" % i, lang_color(name, i))
                   for i, (name, _) in enumerate(top))
    parts = []
    for i, (name, size) in enumerate(top):
        y = top_y + i * row_h
        share = size / total
        colour = lang_color(name, i)
        label = name if len(name) <= 13 else name[:12] + "…"
        parts.append(txt(38, y + 12, esc(label), "v"))
        parts.append('<rect x="%d" y="%d" width="%d" height="11" rx="5.5" fill="%s" '
                     'opacity="0.14"/>' % (bar_x, y + 2, bar_w, colour))
        parts.append('<rect x="%d" y="%d" width="%.1f" height="11" rx="5.5" '
                     'fill="url(#lg%d)"><title>%s: %sB across %d repos (%.1f%%)</title>'
                     '</rect>' % (bar_x, y + 2, max(share * bar_w, 4), i,
                                  esc(name), human(size), projects[name], share * 100))
        parts.append(txt(bar_x + bar_w + 10, y + 12, "%.1f%%" % (share * 100), "xs"))
        parts.append(txt(HALF - 28, y + 12, "%d repo%s" % (projects[name],
                     "" if projects[name] == 1 else "s"), "l", "end"))

    write(out_dir, "languages.svg",
          card(HALF, height, "Language Footprint", "\n".join(parts), defs,
               subtitle="Share of %sB written, and projects using it" % human(total)))


def chart_activity_mix(events, out_dir):
    """Question: what kinds of work do I do (not how much)?"""
    label_for = {
        "PushEvent": "Commits", "PullRequestEvent": "Pull requests",
        "IssuesEvent": "Issues", "IssueCommentEvent": "Comments",
        "CreateEvent": "Created", "DeleteEvent": "Deleted",
        "WatchEvent": "Stars given", "ForkEvent": "Forks",
        "PullRequestReviewEvent": "Reviews",
        "PullRequestReviewCommentEvent": "Review comments",
        "ReleaseEvent": "Releases", "PublicEvent": "Made public",
        "MemberEvent": "Collaborators", "GollumEvent": "Wiki",
    }
    tally = Counter()
    for event in events:
        tally[label_for.get(event.get("type"), "Other")] += 1
    if not tally:
        return

    ranked = tally.most_common(6)
    total = sum(tally.values())
    top_y, row_h = 96, 30
    height = top_y + len(ranked) * row_h + 30
    bar_x, bar_w = 168, 200

    defs = "".join(bar_gradient("am%d" % i, SERIES[i % len(SERIES)])
                   for i in range(len(ranked)))
    parts = []
    for i, (label, count) in enumerate(ranked):
        y = top_y + i * row_h
        share = count / total
        parts.append('<rect x="38" y="%d" width="11" height="11" rx="3" fill="%s"/>'
                     % (y + 2, SERIES[i % len(SERIES)]))
        parts.append(txt(58, y + 12, esc(label), "v"))
        parts.append('<rect x="%d" y="%d" width="%d" height="11" rx="5.5" fill="%s" '
                     'opacity="0.14"/>' % (bar_x, y + 2, bar_w, SERIES[i % len(SERIES)]))
        parts.append('<rect x="%d" y="%d" width="%.1f" height="11" rx="5.5" '
                     'fill="url(#am%d)"><title>%s: %d events (%.0f%%)</title></rect>'
                     % (bar_x, y + 2, max(share * bar_w, 4), i, esc(label),
                        count, share * 100))
        parts.append(txt(HALF - 38, y + 12, "%d" % count, "xs", "end"))

    write(out_dir, "activity-mix.svg",
          card(HALF, height, "Activity Mix", "\n".join(parts), defs,
               subtitle="%d public events over the last ~90 days" % total))


def chart_active_repos(events, out_dir):
    """Question: which projects am I working on right now?"""
    tally = Counter()
    for event in events:
        repo = (event.get("repo") or {}).get("name", "")
        if repo:
            tally[repo.split("/")[-1]] += 1
    if not tally:
        return

    ranked = tally.most_common(9)
    top_y, row_h = 96, 30
    height = top_y + len(ranked) * row_h + 30
    bar_x, bar_w = 168, 200
    peak = ranked[0][1]

    defs = bar_gradient("ar", VIOLET)
    parts = []
    for i, (name, count) in enumerate(ranked):
        y = top_y + i * row_h
        label = name if len(name) <= 17 else name[:16] + "…"
        parts.append(txt(38, y + 12, esc(label), "v"))
        parts.append('<rect x="%d" y="%d" width="%d" height="11" rx="5.5" fill="%s" '
                     'opacity="0.14"/>' % (bar_x, y + 2, bar_w, VIOLET))
        parts.append('<rect x="%d" y="%d" width="%.1f" height="11" rx="5.5" '
                     'fill="url(#ar)"><title>%s: %d events</title></rect>'
                     % (bar_x, y + 2, max(count / peak * bar_w, 4), esc(name), count))
        parts.append(txt(HALF - 38, y + 12, "%d" % count, "xs", "end"))

    write(out_dir, "active-repos.svg",
          card(HALF, height, "Where I'm Working", "\n".join(parts), defs,
               subtitle="Public events per repository, last ~90 days"))


def chart_timeline(repos, out_dir):
    """Question: how has my stack shifted over the years?

    Repos created per year, stacked by primary language - the language axis is
    a colour dimension here, not a second 'top languages' chart.
    """
    dated = [r for r in repos if r["created"]]
    if not dated:
        return
    years = OrderedDict()
    for repo in sorted(dated, key=lambda r: r["created"]):
        years.setdefault(repo["created"][:4], Counter())[repo["primary"] or "Other"] += 1

    height = 320
    left, top, plot_h = 58, 100, 140
    slot = (WIDE - left - 40) / len(years)
    bar_w = min(slot - 22, 56)
    peak = max(sum(c.values()) for c in years.values())

    totals = Counter()
    for counts in years.values():
        totals.update(counts)
    order = [name for name, _ in totals.most_common()]

    # Assign colours so no two legend entries collide: several GitHub language
    # colours sit close to the SERIES fallbacks (JavaScript's yellow and
    # SERIES[2] are almost the same), which made the legend unreadable.
    palette, used = {}, set()
    for name in order:
        colour = MUTED if name == "Other" else LANG_COLORS.get(name)
        if not colour or colour in used:
            colour = next((c for c in SERIES if c not in used), MUTED)
        used.add(colour)
        palette[name] = colour

    parts = []
    for i in range(4):
        value = peak * (3 - i) / 3
        y = top + plot_h - value * plot_h / peak
        parts.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" '
                     'stroke-dasharray="3 5" opacity="0.35"/>'
                     % (left, y, WIDE - 40, y, BORDER))
        parts.append(txt(left - 12, y + 4, "%d" % round(value), "l", "end"))

    for i, (year, counts) in enumerate(years.items()):
        x = left + i * slot + (slot - bar_w) / 2
        y = top + plot_h
        for name, count in counts.most_common():
            seg = count / peak * plot_h
            y -= seg
            parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="3" '
                         'fill="%s"><title>%s &#183; %s: %d repo(s)</title></rect>'
                         % (x, y, bar_w, seg, palette.get(name, MUTED),
                            year, esc(name), count))
        parts.append(txt("%.1f" % (x + bar_w / 2), y - 8,
                         "%d" % sum(counts.values()), "xs", "middle"))
        parts.append(txt("%.1f" % (x + bar_w / 2), top + plot_h + 22, year, "l", "middle"))

    lx = 38
    for name in order[:7]:
        parts.append('<rect x="%.1f" y="%d" width="10" height="10" rx="3" fill="%s"/>'
                     % (lx, height - 32, palette[name]))
        parts.append(txt(lx + 16, height - 23, esc(name), "l"))
        lx += 26 + len(name) * 7

    write(out_dir, "repo-timeline.svg",
          card(WIDE, height, "Portfolio Timeline", "\n".join(parts),
               subtitle="Repositories created each year, coloured by primary language"))


def chart_repo_matrix(repos, events, out_dir):
    """Question: how do age, size and current activity relate per project?

    Three dimensions at once - a scatter, not another ranked bar chart.
    """
    sized = [r for r in repos if r["bytes"] > 0 and r["created"]]
    if len(sized) < 2:
        return

    recent = Counter()
    for event in events:
        name = (event.get("repo") or {}).get("name", "")
        if name:
            recent[name.split("/")[-1]] += 1

    height = 380
    left, right, top, bottom = 62, 34, 96, 78
    plot_w, plot_h = WIDE - left - right, height - top - bottom

    stamps = [datetime.strptime(r["created"], "%Y-%m-%d") for r in sized]
    first, last = min(stamps), max(stamps)
    span = max((last - first).days, 1)
    import math
    lo = math.log10(min(r["bytes"] for r in sized) + 1)
    hi = math.log10(max(r["bytes"] for r in sized) + 1)
    rng = max(hi - lo, 0.001)
    busiest = max(recent.values()) if recent else 1

    parts = []
    for i in range(4):
        y = top + plot_h * i / 3
        value = 10 ** (hi - rng * i / 3)
        parts.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" '
                     'stroke-dasharray="3 5" opacity="0.3"/>'
                     % (left, y, WIDE - right, y, BORDER))
        parts.append(txt(left - 12, y + 4, human(value) + "B", "l", "end"))

    for repo in sorted(sized, key=lambda r: -r["bytes"]):
        when = datetime.strptime(repo["created"], "%Y-%m-%d")
        x = left + (when - first).days / span * plot_w
        y = top + plot_h - (math.log10(repo["bytes"] + 1) - lo) / rng * plot_h
        hits = recent.get(repo["name"], 0)
        radius = 8 + (hits / busiest) ** 0.5 * 16 if hits else 7
        colour = lang_color(repo["primary"], 0) if repo["primary"] else MUTED
        # <title> lives beside the circle rather than inside it: the tooltip
        # behaves identically, and some renderers drop a <circle> that has
        # element children.
        parts.append('<g><title>%s &#183; %s &#183; %sB &#183; created %s &#183; '
                     '%d recent events</title>'
                     '<circle cx="%.1f" cy="%.1f" r="%.1f" fill="%s" '
                     'fill-opacity="%s" stroke="%s" stroke-width="1.5"/></g>'
                     % (esc(repo["name"]), esc(repo["primary"] or "Other"),
                        human(repo["bytes"]), repo["created"], hits,
                        x, y, radius, colour, "0.5" if hits else "0.3", colour))

    for i in range(4):
        when = first + timedelta(days=span * i / 3)
        parts.append(txt("%.1f" % (left + plot_w * i / 3), top + plot_h + 26,
                         when.strftime("%b %Y"), "l", "middle"))

    parts.append(txt(38, height - 22,
                     "Vertical: code size (log) &#183; horizontal: created "
                     "&#183; bubble: recent activity", "l"))

    write(out_dir, "repo-matrix.svg",
          card(WIDE, height, "Project Matrix", "\n".join(parts),
               subtitle="%d repositories by age, code volume and current activity"
                        % len(sized)))


def chart_punchcard(events, out_dir):
    """Question: at what hours do I actually work?

    Two-hour blocks: ~300 events over 168 hourly cells is too sparse to read.
    """
    grid = defaultdict(int)
    for event in events:
        stamp = event.get("created_at")
        if not stamp:
            continue
        when = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
        when = when.replace(tzinfo=timezone.utc).astimezone(TZ)
        weight = len((event.get("payload") or {}).get("commits") or [1])
        grid[(when.weekday(), when.hour // 2)] += weight
    if not grid:
        return

    cell, gap = 56, 7
    step = cell + gap
    left, top = 62, 116
    width = left + 12 * step + 30
    height = top + 7 * step + 44
    peak = max(grid.values())

    def level(count):
        return 0 if count <= 0 else min(4, 1 + int((count - 1) * 4 / peak))

    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    full = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
            "Saturday", "Sunday"]
    parts = []
    for block in range(12):
        parts.append(txt(left + block * step + cell // 2, top - 12,
                         "%02d" % (block * 2), "l", "middle"))
    for day in range(7):
        parts.append(txt(left - 12, top + day * step + cell // 2 + 4,
                         days[day], "l", "end"))
        for block in range(12):
            count = grid.get((day, block), 0)
            parts.append('<rect x="%d" y="%d" width="%d" height="%d" rx="8" fill="%s">'
                         '<title>%s %02d:00-%02d:00 &#183; %d events</title></rect>'
                         % (left + block * step, top + day * step, cell, cell,
                            HEAT[level(count)], days[day], block * 2,
                            block * 2 + 2, count))
            if count:
                parts.append(txt(left + block * step + cell // 2,
                                 top + day * step + cell // 2 + 5,
                                 "%d" % count, "xs", "middle"))

    best = max(grid, key=lambda k: grid[k])
    write(out_dir, "punch-card.svg",
          card(width, height, "When I Code", "\n".join(parts),
               subtitle="Peak: %s, %02d:00-%02d:00 (Asia/Dhaka) &#183; 2-hour blocks"
                        % (full[best[0]], best[1] * 2, best[1] * 2 + 2)))


# ----------------------------------------------------------------------- main

def main():
    if len(sys.argv) < 3:
        print("usage: generate_charts.py <login> <out_dir>", file=sys.stderr)
        return 2
    login, out_dir = sys.argv[1], sys.argv[2]
    os.makedirs(out_dir, exist_ok=True)

    # Fetch everything first: a chart is only written once all of its inputs
    # are known good, so a mid-run failure can never leave a mix of fresh and
    # stale cards behind.
    try:
        user = fetch_user(login)
        cal = fetch_calendar(login)
        repos = fetch_repos(login)
        events = fetch_events(login)
    except ApiError as exc:
        print("error: %s" % exc, file=sys.stderr)
        print("error: aborting without writing any chart", file=sys.stderr)
        return 1

    if not repos:
        print("error: no repositories returned for %s" % login, file=sys.stderr)
        return 1

    before = set(os.listdir(out_dir))
    chart_hero(user, repos, out_dir)
    chart_summary(user, cal, repos, out_dir)
    chart_heatmap(cal, out_dir)
    chart_trend(cal, out_dir)
    chart_consistency(cal, out_dir)
    chart_languages(repos, out_dir)
    chart_activity_mix(events, out_dir)
    chart_active_repos(events, out_dir)
    chart_timeline(repos, out_dir)
    chart_repo_matrix(repos, events, out_dir)
    chart_punchcard(events, out_dir)

    stale = sorted(name for name in before
                   if name.endswith(".svg") and name not in set(os.listdir(out_dir)))
    if stale:
        print("warning: charts no longer generated: %s" % ", ".join(stale),
              file=sys.stderr)
    print("%d charts in %s" % (
        len([n for n in os.listdir(out_dir) if n.endswith(".svg")]), out_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
