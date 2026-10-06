#!/usr/bin/env python3
"""Draw docs/figures/soak.svg from docs/figures/soak-run.json: what a soak run did to the service, and what the service did about it.

    python3 scripts/figures/soak_figure.py [snapshot.json] [out.svg]

The snapshot is cut from a run's own logs (chaos.jsonl: the kills, the database faults, the bursts; samples.jsonl: how far behind the endpoints that must
keep up were, the largest of them, every two minutes; and the number of violations the checker had found). Nothing in the figure is drawn that the run did not record. The colours follow the page's light and dark
themes through `prefers-color-scheme` inside the SVG, so the file also works as an image.
"""
import json
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "docs", "figures", "soak-run.json")
OUT = sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, "docs", "figures", "soak.svg")
d = json.load(open(SRC))
span = float(d["span_s"])
W, H = 960, 330
L, R = 150, 24                   # left label column, right margin
X = lambda t: L + (W - L - R) * t / span
rows = {"kill": 44, "pg": 84, "burst": 124}
LAG_TOP, LAG_BOT = 170, 288      # the lag panel
hours = int(span // 3600)


def lag_y(v):
    return LAG_BOT - (LAG_BOT - LAG_TOP) * min(1.0, math.log10(max(v, 1)) / 4.0)


o = []
a = o.append
a(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" role="img" aria-labelledby="t d" font-family="system-ui,-apple-system,Segoe UI,Roboto,sans-serif">')
a(f'<title id="t">{round(span / 3600, 1)} hours of a soak run: {len(d["kills"])} kills, {len(d["pg"])} database faults, {len(d["bursts"])} bursts, {int(d.get("violations", -1))} violations</title>')
a('<desc id="d">A timeline. Each red tick is the service killed with kill -9, as a power cut. Amber bars are the database cut, frozen or black-holed. Blue bands are bursts of '
  'one and a half times the load. Below, in teal, how many events behind the endpoints that must keep up were: it rises in each fault and always returns to nearly zero.</desc>')
a('''<style>
 .bg{fill:#ffffff}.fg{fill:#1c1f24}.mu{fill:#5b6573}.ln{stroke:#d9dde3}.kill{stroke:#d1362f}.pg{fill:#d98a14}.bu{fill:#3b6fe0;opacity:.18}.lag{stroke:#0f9d8a;fill:none}.ar{fill:#0f9d8a;opacity:.14}.ok{fill:#0f7b5f}
 @media (prefers-color-scheme: dark){.bg{fill:#141920}.fg{fill:#e6e8eb}.mu{fill:#a6aeba}.ln{stroke:#2a3038}.kill{stroke:#ff6b63}.pg{fill:#f0a93a}.bu{fill:#8ab4ff;opacity:.2}.lag{stroke:#3fd4bf}.ar{fill:#3fd4bf;opacity:.16}.ok{fill:#5bd6aa}}
</style>''')
a(f'<rect class="bg" width="{W}" height="{H}"/>')
# grid and hour ticks
for h in range(0, hours + 1):
    x = X(h * 3600)
    a(f'<line class="ln" x1="{x:.1f}" y1="22" x2="{x:.1f}" y2="{LAG_BOT}" stroke-width="1"/>')
    a(f'<text class="mu" x="{x:.1f}" y="{LAG_BOT + 20}" font-size="12" text-anchor="middle">{h} h</text>')
# row labels
for key, label in (("kill", "service killed"), ("pg", "database faults"), ("burst", "bursts, 1.5× load")):
    a(f'<text class="fg" x="{L - 12}" y="{rows[key] + 5}" font-size="13" text-anchor="end">{label}</text>')
a(f'<text class="fg" x="{L - 12}" y="{LAG_TOP + 14}" font-size="13" text-anchor="end">events behind</text>')
a(f'<text class="mu" x="{L - 12}" y="{LAG_TOP + 31}" font-size="11" text-anchor="end">(log scale)</text>')
# kills
for t in d["kills"]:
    a(f'<line class="kill" x1="{X(t):.1f}" y1="{rows["kill"] - 12}" x2="{X(t):.1f}" y2="{rows["kill"] + 12}" stroke-width="1.4"/>')
# database faults
for t, dur, _ in d["pg"]:
    w = max(2.0, X(t + dur) - X(t))
    a(f'<rect class="pg" x="{X(t):.1f}" y="{rows["pg"] - 11}" width="{w:.1f}" height="22" rx="1.5"/>')
# bursts
for t, dur in d["bursts"]:
    a(f'<rect class="bu" x="{X(t):.1f}" y="{rows["burst"] - 14}" width="{max(3.0, X(t + dur) - X(t)):.1f}" height="28" rx="2"/>')
# lag
pts = [(X(t + 60), lag_y(v)) for t, v in d["lag120"]]
for k, label in ((0, "1"), (1, "10"), (2, "100"), (3, "1,000"), (4, "10,000")):
    y = LAG_BOT - (LAG_BOT - LAG_TOP) * k / 4
    a(f'<line class="ln" x1="{L}" y1="{y:.1f}" x2="{W - R}" y2="{y:.1f}" stroke-width="1" stroke-dasharray="2 4"/>')
    a(f'<text class="mu" x="{L + 4}" y="{y - 3:.1f}" font-size="10" text-anchor="start">{label}</text>')
area = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts) + f" L{pts[-1][0]:.1f},{LAG_BOT} L{pts[0][0]:.1f},{LAG_BOT} Z"
line = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
a(f'<path class="ar" d="{area}"/><path class="lag" d="{line}" stroke-width="1.8" stroke-linejoin="round"/>')
v = int(d.get("violations", -1))
note = f'{v} violations in {round(span / 3600, 1)} h' + (' so far' if d.get("in_progress") else '')
a(f'<text class="{"ok" if v == 0 else "kill"}" x="{W - R}" y="14" font-size="13" font-weight="600" text-anchor="end">{note}</text>')
a('</svg>')
open(OUT, "w").write("\n".join(o) + "\n")
print(f"{OUT}: {os.path.getsize(OUT)} bytes, {len(d['kills'])} kills, {len(d['pg'])} faults, {len(d['bursts'])} bursts, {len(pts)} lag points")
