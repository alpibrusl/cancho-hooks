#!/usr/bin/env python3
"""`src/cron.ls` against an independent implementation: Python's `datetime` for the calendar, sets for the fields, and a scan day
by day for the next and the last fire (docs/design.md section 32). The two do not share an algorithm or a line.

    python3 tests/cron_test.py build/cron_probe [cases]

Random expressions (valid ones and wrong ones: ranges that run backwards, numbers outside a field, steps of 0, a day that does
not exist) at random times between 1970 and 2096, and the edges (every month's last day, leap days, the turn of a year), asked of
`next_after`, `prev_upto` and the refusals. Standard cron semantics are written out in `reference()` below:

  * the day of month and the day of week are "either" unless one of the two fields begins with `*`, and then "both";
  * Sunday is 0 and 7; a step starts at the field's first value; a range does not wrap.
"""
import calendar
import datetime
import random
import re
import subprocess
import sys

PROBE = sys.argv[1] if len(sys.argv) > 1 else "build/cron_probe"
CASES = int(sys.argv[2]) if len(sys.argv) > 2 else 500
rng = random.Random(20240229)

RANGES = [(0, 59), (0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]  # second, minute, hour, day of month, month, day of week
ITEM = re.compile(r"^(\*|(\d{1,3})(?:-(\d{1,3}))?)(?:/(\d{1,3}))?$")


def reference_field(text, lo, hi, dow):
    """The set of values of one field, or None if the field is wrong. Also whether it begins with `*`."""
    values = set()
    for item in text.split(","):
        m = ITEM.match(item)
        if not m:
            return None, False
        base, a, b, step = m.groups()
        if base == "*":
            first, last = lo, (6 if dow else hi)
        elif b is not None:
            first, last = int(a), int(b)
            if not (lo <= first <= hi and lo <= last <= hi) or first > last:
                return None, False
        else:
            first = last = int(a)
            if not lo <= first <= hi or step is not None:
                return None, False
        n = 1
        if step is not None:
            n = int(step)
            if not 1 <= n <= hi - lo + 1:
                return None, False
        for v in range(first, last + 1, n):
            values.add(0 if dow and v == 7 else v)
    return values, text.startswith("*")


def reference(expr, seconds):
    """(sets, star_dom, star_dow), or None if the expression is wrong."""
    fields = expr.split()
    if len(fields) != (6 if seconds else 5) or len(expr) > 100:
        return None
    if not seconds:
        fields = ["0"] + fields
    sets, stars = [], []
    for i, (text, (lo, hi)) in enumerate(zip(fields, RANGES)):
        s, star = reference_field(text, lo, hi, i == 5)
        if s is None:
            return None
        sets.append(s)
        stars.append(star)
    return sets, stars[3], stars[5]


def day_ok(ref, day):
    sets, star_dom, star_dow = ref
    if day.month not in sets[4]:
        return False
    by_date = day.day in sets[3]
    by_week = (day.isoweekday() % 7) in sets[5]
    return (by_date and by_week) if (star_dom or star_dow) else (by_date or by_week)


def times_of(ref):
    sets = ref[0]
    return sorted(h * 3600 + m * 60 + s for h in sets[2] for m in sets[1] for s in sets[0])


EPOCH = datetime.date(1970, 1, 1)
HORIZON = 366 * 41


def ref_next(ref, t):
    times = times_of(ref)
    if not times:
        return -1
    day_no, sod = divmod(t + 1, 86400)
    for d in range(day_no, day_no + HORIZON):
        day = EPOCH + datetime.timedelta(days=d)
        if day.year > 9999:
            return -1
        if day_ok(ref, day):
            for x in times:
                if d > day_no or x >= sod:
                    return d * 86400 + x
    return -1


def ref_prev(ref, t):
    times = times_of(ref)
    if not times or t < 0:
        return -1
    day_no, sod = divmod(t, 86400)
    for d in range(day_no, max(day_no - HORIZON, -1), -1):
        day = EPOCH + datetime.timedelta(days=d)
        if day_ok(ref, day):
            for x in reversed(times):
                if d < day_no or x <= sod:
                    return d * 86400 + x
    return -1


def probe(*args):
    out = subprocess.run([PROBE, *map(str, args)], capture_output=True, timeout=30, text=True)
    return out.stdout.strip()


def random_field(lo, hi, dow):
    def number():
        return rng.choice([lo, hi, rng.randint(lo, hi), rng.randint(lo, hi)]) if rng.random() < 0.97 else rng.choice([lo - 1, hi + 1, 0, 99])

    def item():
        k = rng.random()
        if k < 0.25:
            return "*"
        if k < 0.40:
            return "*/" + str(rng.choice([1, 2, 3, 5, 7, 10, 15, rng.randint(0, hi + 2)]))
        if k < 0.65:
            return str(number())
        a, b = sorted([number(), number()]) if rng.random() < 0.95 else (number(), number())
        text = f"{a}-{b}"
        if rng.random() < 0.3:
            text += "/" + str(rng.choice([1, 2, 3, 5, rng.randint(0, 12)]))
        return text

    return ",".join(item() for _ in range(rng.choice([1, 1, 1, 2, 3])))


def random_expr(seconds):
    spans = RANGES if seconds else RANGES[1:]
    return " ".join(random_field(lo, hi, i == len(spans) - 1) for i, (lo, hi) in enumerate(spans))


def main():
    fails = checks = 0

    def check(what, got, want):
        nonlocal fails, checks
        checks += 1
        if str(got) != str(want):
            fails += 1
            if fails <= 15:
                print(f"FAIL {what}: got {got}, want {want}")

    edges = [
        "* * * * *", "0 0 29 2 *", "0 0 31 * *", "0 0 30 * *", "59 23 31 12 *", "0 0 1 1 *", "0 0 * * 0", "0 0 * * 7", "0 0 1 * 1",
        "0 0 */2 * 1", "*/15 * * * *", "0 0 29 2 */1", "0 0 29 2 1", "30 4 1,15 * 5", "0 0 31 2 *", "0 0 31 2 1", "0 12 * * 5-7",
        "0 0 29 2 0", "0 0 * 2 *", "*/7 */5 */3 */2 */2",
    ]
    cases = [(e, False) for e in edges] + [(random_expr(False), False) for _ in range(CASES)]
    cases += [(random_expr(True), True) for _ in range(CASES // 5)]
    times = [0, 68169599, 68169600, 951782399, 951782400, 1709164799, 1709164800, 1709251199, 1735689599, 1735689600, 4102444799, 4233686399, 3981311999,
             calendar.timegm((2100, 2, 28, 23, 59, 59)), calendar.timegm((2100, 3, 1, 0, 0, 0)), calendar.timegm((2096, 2, 29, 0, 0, 0))]
    edge_set = {(e, False) for e in edges}
    accepted = refused = never = 0
    for expr, seconds in cases:
        flag = 1 if seconds else 0
        ref = reference(expr, seconds)
        code = probe("check", flag, expr)
        if ref is None:
            check(f"refuse {expr!r}", code != "0" and code != "7", True)
            refused += 1
            continue
        if not times_of(ref) or ref_next(ref, 0) < 0:
            check(f"never {expr!r}", code, 7)
            never += 1
            continue
        check(f"accept {expr!r}", code, 0)
        accepted += 1
        # the edge expressions are asked at every edge time; the random ones at a few
        for t in (times if (expr, seconds) in edge_set else times[:3] + [rng.choice(times)]) + [rng.randint(0, 4_000_000_000) for _ in range(3)]:
            check(f"next {expr!r} after {t}", probe("next", flag, expr, t), ref_next(ref, t))
            check(f"prev {expr!r} up to {t}", probe("prev", flag, expr, t), ref_prev(ref, t))
    # the time as text
    for t in [0, 59, 86399, 86400, 951782400, 1709164800, 1709251199, 4102444800, 253402300799]:
        want = datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        check(f"iso {t}", probe("iso", t), want)
    check("iso of the year 10000", probe("iso", 253402300800), "none")
    # a whole table of days: the first second of every month from 1970 to 2100 against calendar.timegm
    for year in range(1970, 2101, 7):
        for month in range(1, 13):
            t = calendar.timegm((year, month, 1, 0, 0, 0))
            if t > 0:
                check(f"first of {year}-{month}", probe("next", 0, "0 0 1 * *", t - 1), t)
    print(f"{accepted} accepted, {refused} refused, {never} never fire; {checks} checks, {fails} failures")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
