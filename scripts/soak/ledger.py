"""The checker of the soak (docs/soak.md section 3): it reads the poster's ledger and the receivers' ledger, which the service has no hand in, and the service's own
claim (each endpoint's cursor), and says which invariants broke. It keeps state only for a window of the newest events, so that a day's run uses bounded memory.

The checker is a class with no I/O and no clock of its own: every time is an argument. That is what lets `selftest_ledger.py` feed it synthetic ledgers, mutate them,
and require that each mutation is caught under its own tag.

Tags (the letters are the invariants of docs/soak.md):

  A_missing         an acknowledged event, at an endpoint that wants it, is below the endpoint's cursor and was never delivered
  A_missing_final   the same, found at the end (an event that died at a `sick` endpoint and was not replayed)
  A_unseen_event    an event id the service holds that no ledger knows
  B_repeat          a second delivery that nothing explains
  B_restart_repeat  the same, when the service was restarted after the first delivery (and within 15 minutes before this one)
  C_filter          a delivery of a type the endpoint does not subscribe to
  C_old_event       a delivery of an event older than the endpoint
  C_foreign_type    a delivery of a type the harness never posted
  D_signature       a delivery whose signature no valid secret of the endpoint makes
  D_timestamp       a delivery whose timestamp is more than five minutes off
  D_body            a delivery whose body does not parse or whose checksum is wrong
  D_overlap         a delivery with one signature while a rotation overlaps
  E_cursor_back     a cursor that went backwards, or did not get back after a restart
  E_cursor_ahead    a cursor beyond the newest event
  F_dead_letter     a dead letter at an endpoint that must deliver everything
  F_never           a delivery of an event that must never be delivered to the endpoint (a 410's)
  I_cron_twice      two events for one scheduled second
  I_cron_gap        a gap in a schedule that no absence of the service explains
  P_phantom         a delivery of an event the poster never posted
  P_id_mismatch     one id for two different events (or two ids for one keyed event)
  U_unknown_ep      a record of an endpoint that is not in the spec
  A_expired_unexplained  more losses excused as expired (below) than the service says it expired
  END_lag           an endpoint that had not caught up at the end

**Expiry.** With a hard maximum age (`max-age-days`, design.md 47.2; the soak sets `--max-age-ms`) the service drops a segment however much something pins it, moving every endpoint's
cursor past what it had not delivered and counting those events (`events_expired`). A loss at an endpoint is excused as an expiry **only if the event was older than the maximum age when
the cursor was found past it** (by the time the poster was told it was accepted, less a second or two of slack), and the number so excused is held against what the service says it
expired (`finish`). An event younger than that which is passed undelivered is A_missing, as it always was; so is any event when the run has no maximum age.
"""
import bisect
from collections import Counter, defaultdict, deque

from common import CRON_TYPE, F_BODY, F_EFF, F_RISK, F_SIG, F_TS, F_TWO, UNKNOWN_TYPE, type_name, wanted

MUST_DELIVER = ("oracle", "healthy", "healthy2", "filter", "slow", "flapping", "http5xx", "https", "rate", "churn")


class Violations:
    def __init__(self, sink=None, keep=20):
        self.count = Counter()
        self.examples = defaultdict(list)
        self.keep = keep
        self.sink = sink     # a function(tag, detail) called for each (a JSONL writer)

    def add(self, tag, **detail):
        self.count[tag] += 1
        if len(self.examples[tag]) < self.keep:
            self.examples[tag].append(detail)
        if self.sink:
            self.sink(tag, detail)

    def total(self):
        return sum(self.count.values())


class EpState:
    def __init__(self, idx, label, cls, types, c0=0, created=0.0, params=None):
        self.idx, self.label, self.cls, self.types = idx, label, cls, list(types)
        self.c0, self.created = c0, created
        self.params = params or {}
        self.settled = c0
        self.unknown = set()
        self.deferred = set()        # `sick`: events that failed at least once and so may be dead: judged again at the end
        self.failed_once = set()
        self.windows = []            # (from, to): the times a `sick` or `dead` endpoint was made to fail: what is acknowledged in them may die
        self.overlaps = []           # (from, to): two signatures are due
        self.retired = None
        self.hist = deque()          # (t, cursor, inc)
        self.ref = {}                # inc -> the cursor the first sample of that incarnation must reach
        self.ref_lim = {}            # inc -> the time up to which cursors that were read count for it
        self.seen_inc = set()
        self.recover = {}            # inc -> (cursor to get back to, time since which, deadline)
        self.last_cursor = None
        self.recs = self.eff = self.nonEff = self.bad = 0
        self.backlog = []            # records that arrived before the harness knew where the endpoint starts (c0 is None until `activate`)
        self.lag_s = 0.0             # how long the service's own lag says an event waits for its first attempt at this endpoint (note_lag)
        self.ambiguous = False       # an endpoint whose creation was not answered: delivered to, but no claim about it is judged

    def expect(self, seed, typ, ev):
        from common import poison
        if not wanted(self.types, typ):
            return "skip"
        if self.cls == "gone" and poison(seed, self.label, ev, self.params.get("poison", 0.01)):
            return "never"
        return "deliver"


class Verifier:
    EXPIRY_SLACK_S = 2.0
    RETIRED_GRACE_S = 90.0        # a retired endpoint's receiver is gone 12 s after it is retired: what can still come to it, and what is kept for it, is kept this long
    RETIRED_FORGET_S = 3600.0     # and the endpoint itself is forgotten after this
    LAG_CAP_S = 300.0

    def __init__(self, seed, W=2.0, keep_s=300.0, violations=None, max_age_s=None):
        self.seed, self.W, self.keep_s = seed, W, keep_s
        self.max_age_s = max_age_s          # the service's hard maximum age in seconds (None: the run has none, nothing is excused as expired)
        self.expired_excused = Counter()    # label -> losses excused as expired
        self.v = violations or Violations()
        self.by_idx, self.by_label = {}, {}
        self.ev = {}                 # id -> [typ, n, src, first_seen, acked]
        self.cnt = {}                # (ep idx, ev) -> [eff count, last eff t, explained by kill, last risk]
        self.repl = Counter()        # (ep idx, ev) -> replays accepted
        self.n2id = {}               # n -> first id seen with it (src 0)
        self.keyed = set()
        self.posted_n = set()
        self.kills = []              # sorted times the service died without a drain
        self.kill_repeats = Counter()
        self.stops = []              # (time, budget) graceful stops: the attempts left on the wire
        self.stop_used = Counter()
        self.away = []               # (from, to, kind): service or database away (cron)
        self.cron = defaultdict(lambda: defaultdict(set))    # src -> scheduled second -> event ids
        self.cron_period = {}
        self.stats = Counter()
        self.floor = 0               # ids at or below are purged
        self.t_last = 0.0
        self.explained = Counter()
        self.replayed = {}           # event -> when a replay of it was asked for: an endpoint created while the replay waits is sent it too
        self.restarts = []           # times the service was stopped or killed to be started again
        self.recoveries = []         # seconds a cursor took to get back to what it was before a restart
        self.recover_s = 120.0
        self.forgotten = set()       # idx of endpoints retired long ago and dropped from the checker's memory: a record of one is a zombie that delivered
        self.max_ev = 600000         # the most events the window may hold (about four hours at the soak's rate): past it the window is cut and the run says so

    # ---- configuration of the run
    def add_endpoint(self, idx, label, cls, types, c0=0, created=0.0, params=None):
        """`c0=None`: the endpoint exists (a receiver is listening) but the harness does not yet know the event it starts after; what reaches it meanwhile is kept."""
        ep = EpState(idx, label, cls, types, c0, created, params)
        self.by_idx[idx] = ep
        self.by_label[label] = ep
        return ep

    def activate(self, label, c0, created, ambiguous=False):
        ep = self.by_label[label]
        ep.c0, ep.created, ep.settled, ep.ambiguous = (0 if ambiguous else c0), created, (0 if ambiguous else c0), ambiguous
        backlog, ep.backlog = ep.backlog, []
        if backlog:
            self.ingest(backlog)

    def note_lag(self, label, lag_events, rate):
        """The service's own lag at an endpoint (events its cursor is behind), as the time an event may wait for its first attempt there: what `_judge` allows a `sick` or `dead` endpoint's
        event before it takes its acknowledgement for the moment it could have been attempted."""
        ep = self.by_label.get(label)
        if ep is not None:
            ep.lag_s = min(self.LAG_CAP_S, max(0.0, lag_events) / max(1.0, rate))

    def note_kill(self, t):
        bisect.insort(self.kills, t)

    def note_window(self, label, a, b):
        self.by_label[label].windows.append((a - 1.0, b))

    def note_stop(self, t, on_wire):
        self.stops.append((t, on_wire))

    def note_away(self, a, b, kind):
        self.away.append((a, b, kind))

    def note_replay(self, label, ev, n=1):
        ep = self.by_label.get(label)
        if ep:
            self.repl[(ep.idx, ev)] += n
        if n > 0:
            self.replayed[ev] = self.t_last

    def note_rotation(self, label, t_start, t_end):
        """Two signatures are due from t_start + 3 s to t_end - 3 s."""
        ep = self.by_label[label]
        # a rotation ends the overlap of the one before it (two signatures are due only until three seconds before the next change)
        ep.overlaps = [(a, min(b, t_start - 3.0)) for (a, b) in ep.overlaps]
        if t_end - t_start > 6:
            ep.overlaps.append((t_start + 3.0, t_end - 3.0))

    # ---- a resumed run (docs/soak.md section 6): what the checker was told, and what it was holding open, is kept with the state of the run
    def export_notes(self, now=None):
        """The part of the checker's state that is not in the ledgers: what the harness told it (kills, stops, absences, replays, the windows in which an endpoint was made to fail,
        the overlaps of rotations) and what it held open (events that failed at a `sick` endpoint and may be dead, cursors read lately)."""
        return {
            "kills": self.kills[-300:], "stops": self.stops[-300:], "away": self.away[-400:], "restarts": self.restarts[-300:], "recoveries": self.recoveries[-2000:],
            "repl": [[k[0], k[1], n] for k, n in self.repl.items() if n][-30000:], "replayed": [[k, t] for k, t in self.replayed.items()][-30000:],
            "eps": {label: {"windows": ep.windows[-200:], "overlaps": ep.overlaps[-100:], "failed_once": sorted(ep.failed_once)[-50000:], "deferred": sorted(ep.deferred)[-50000:],
                            "hist": list(ep.hist), "last_cursor": ep.last_cursor}
                    for label, ep in self.by_label.items() if ep.retired is None or (now is not None and now - ep.retired < 600.0)},
        }

    def import_notes(self, n):
        self.kills = sorted(n.get("kills", []))
        self.stops = [tuple(x) for x in n.get("stops", [])]
        self.away = [tuple(x) for x in n.get("away", [])]
        self.restarts = list(n.get("restarts", []))
        self.recoveries = list(n.get("recoveries", []))
        self.repl = Counter({(i, e): c for i, e, c in n.get("repl", [])})
        self.replayed = {e: t for e, t in n.get("replayed", [])}
        for label, d in n.get("eps", {}).items():
            ep = self.by_label.get(label)
            if ep is None:
                continue
            ep.windows = [tuple(x) for x in d.get("windows", [])]
            ep.overlaps = [tuple(x) for x in d.get("overlaps", [])]
            ep.failed_once = set(d.get("failed_once", []))
            ep.deferred = set(d.get("deferred", []))
            ep.hist = deque(tuple(x) for x in d.get("hist", []))
            lc = d.get("last_cursor")
            ep.last_cursor = tuple(lc) if lc else None

    def note_restart(self, inc, t_stop, kind):
        """The service has been started again as incarnation `inc` after a stop of `kind` ('kill' or 'term') at t_stop: what its first cursors must reach."""
        self.restarts.append(t_stop)
        for ep in self.by_idx.values():
            lim = t_stop - (self.W if kind == "kill" else 0.0)
            # a service killed again before any cursor of it was read: what it must reach is what the one before it had to
            for k in list(ep.ref_lim):
                if k not in ep.seen_inc:
                    lim = min(lim, ep.ref_lim[k])
            ep.ref_lim[inc] = lim
            ref = None
            for (t, c, i) in ep.hist:
                if t <= lim:
                    ref = c if ref is None else max(ref, c)
            if ref is not None:
                ep.ref[inc] = ref

    # ---- the poster's ledger
    def ingest_acks(self, recs):
        for (t_ack, t_send, n, ident, typ, flags, _pad) in recs:
            self.posted_n.add(n)
            if flags & 1:
                self.keyed.add(n)
            if not ident:
                continue
            self.stats["acked"] += 1
            e = self.ev.get(ident)
            if e is None:
                self.ev[ident] = [typ, n, 0, t_ack, True]
            else:
                if e[1] != n or (e[0] != typ and e[0] != UNKNOWN_TYPE):
                    self.v.add("P_id_mismatch", id=ident, n_ack=n, n_seen=e[1])
                e[4] = True
            other = self.n2id.get(n)
            if other is None:
                self.n2id[n] = ident
            elif other != ident and n in self.keyed:
                self.v.add("P_id_mismatch", n=n, ids=[other, ident], why="one keyed event, two ids")

    # ---- the receivers' ledger
    def ingest(self, recs):
        v = self.v
        for rec in recs:
            (t, idx, ev, n, src, typ, flags, status, e2e) = rec[:9]      # a tenth field (when the answer was sent) is for whoever reads the ledger by hand: the judgements use the time of the request
            self.t_last = max(self.t_last, t)
            ep = self.by_idx.get(idx)
            if ep is None:
                self.stats["late_forgotten" if idx in self.forgotten else "unknown_ep"] += 1
                v.add("U_unknown_ep", idx=idx, ev=ev, why="an endpoint retired more than an hour ago delivered: a zombie" if idx in self.forgotten else "not in the spec")
                continue
            if ep.c0 is None:
                ep.backlog.append((t, idx, ev, n, src, typ, flags, status, e2e))
                continue
            ep.recs += 1
            self.stats["records"] += 1
            if not (flags & F_SIG):
                ep.bad += 1
                v.add("D_signature", ep=ep.label, ev=ev, t=t, status=status)
            if not (flags & F_TS):
                v.add("D_timestamp", ep=ep.label, ev=ev, t=t)
            if not (flags & F_BODY):
                v.add("D_body", ep=ep.label, ev=ev, t=t)
            elif (flags & F_SIG) and not (flags & F_TWO):
                for (a, b) in ep.overlaps:
                    if a <= t <= b:
                        v.add("D_overlap", ep=ep.label, ev=ev, t=t)
                        break
            # learn the event; but not one that is below the floor of what has been purged: it was judged and forgotten, and a late delivery of it (a replay that came late, a zombie)
            # is not a new event that nobody posted. What the record says of itself (signature, time, body, type against the endpoint's list) is judged all the same.
            late = bool(ev) and ev <= self.floor and src == 0 and ev not in self.ev
            if late:
                self.stats["late_after_purge"] += 1
                e = [typ, n, src, t, True]
            else:
                e = self.ev.get(ev)
                if e is None:
                    e = self.ev[ev] = [typ, n, src, t, False]
                elif src == 0 and (e[1] != n) and ev:
                    v.add("P_id_mismatch", id=ev, n_ack=e[1], n_seen=n, ep=ep.label)
                if src == 0 and ev:
                    other = self.n2id.get(n)
                    if other is None:
                        self.n2id[n] = ev
                    elif other != ev and n in self.keyed:
                        v.add("P_id_mismatch", n=n, ids=[other, ev], why="one keyed event, two ids")
                e[2] = src
                if src:
                    self.cron[src][n].add(ev)
            name = type_name(typ) if typ != UNKNOWN_TYPE else None
            exempt = self.repl.get((idx, ev), 0) > 0
            if typ == 254:
                v.add("C_foreign_type", ep=ep.label, ev=ev)
            elif not exempt:
                if not wanted(ep.types, name):
                    v.add("C_filter", ep=ep.label, ev=ev, type=name, subscribed=ep.types)
                elif ev <= ep.c0 and not ep.ambiguous and ev not in self.replayed:
                    v.add("C_old_event", ep=ep.label, ev=ev, c0=ep.c0)
            if flags & F_EFF:
                ep.eff += 1
                self._eff(ep, ev, t, flags)
            else:
                ep.nonEff += 1
                if ep.cls in ("sick", "dead"):
                    ep.failed_once.add(ev)

    def _eff(self, ep, ev, t, flags):
        key = (ep.idx, ev)
        c = self.cnt.get(key)
        risk = bool(flags & F_RISK)
        ep.deferred.discard(ev)
        if c is None:
            self.cnt[key] = [1, t, 0, risk, t]
            return
        prev_t = c[1]
        c[0] += 1
        c[1] = t
        prev_risk, c[3] = c[3], risk
        self.stats["repeats"] += 1
        if c[0] - 1 <= self.repl.get(key, 0) + c[2]:
            self.explained["replay"] += 1
            return
        if ep.ambiguous:
            # a create that was not answered (a 504): the row may be in the table while the running service does not know it, so a DELETE is a 404 and the next start
            # loads it, at the slowest cursor, where a replay to it is not one the harness asked for (docs/status.md: the table is read once). No claim about it is judged.
            self.explained["ambiguous"] += 1
            return
        k = self._kill_between(prev_t, t)
        if k is not None:
            c[2] += 1
            self.kill_repeats[k] += 1
            self.explained["kill"] += 1
            return
        s = self._stop_between(prev_t, t)
        if s is not None:
            c[2] += 1
            self.explained["stop"] += 1
            return
        if prev_risk:
            c[2] += 1
            self.explained["slow_answer"] += 1
            return
        # a repeat that follows a restart of the service (within fifteen minutes) of a delivery made before it, and that no kill or replay explains: the service made again
        # something it had recorded as delivered. It is its own tag because it is one finding with one cause (docs/soak.md, "What the shakedown found").
        after = any(c[4] < r <= t and t - r < 900.0 for r in self.restarts)
        self.v.add("B_restart_repeat" if after else "B_repeat", ep=ep.label, ev=ev, first=prev_t, again=t, gap=round(t - prev_t, 3), count=c[0])

    def _kill_between(self, prev_t, t):
        # a delivery may be read by the receiver a moment *after* the service that made it was killed (the request was in the kernel's buffers): one second either way
        i = bisect.bisect_left(self.kills, prev_t - 1.0)
        while i < len(self.kills) and self.kills[i] <= t:
            if -1.0 <= self.kills[i] - prev_t <= self.W:
                return self.kills[i]
            i += 1
        return None

    def _stop_between(self, prev_t, t):
        for i, (ts, budget) in enumerate(self.stops):
            if prev_t - 1.0 <= ts <= t and ts - prev_t <= self.W + 5.0 and self.stop_used[i] < budget:
                self.stop_used[i] += 1
                return i
        return None

    # ---- the service's claim
    def cursor_sample(self, label, cursor, t, inc, last_id=None):
        ep = self.by_label[label]
        if ep.c0 is None or ep.ambiguous:
            return
        if last_id is not None and cursor > last_id:
            self.v.add("E_cursor_ahead", ep=label, cursor=cursor, last_id=last_id)
        if ep.last_cursor is not None and ep.last_cursor[2] == inc and cursor < ep.last_cursor[0]:
            self.v.add("E_cursor_back", ep=label, was=ep.last_cursor[0], now=cursor, inc=inc)
        if inc not in ep.seen_inc:
            ep.seen_inc.add(inc)
            ref = ep.ref.get(inc)
            if ref is not None and cursor < ref:
                # a service that has just started has not yet worked its cursor up again (it re-reads the events above the one it recorded): that is allowed for `recover_s`
                ep.recover[inc] = (ref, t, t + self.recover_s)
        rec = ep.recover.get(inc)
        if rec is not None:
            if cursor >= rec[0]:
                self.recoveries.append(round(t - rec[1], 1))
                del ep.recover[inc]
            elif t > rec[2]:
                self.v.add("E_cursor_back", ep=label, was=rec[0], now=cursor, inc=inc, why=f"after a restart the cursor did not get back to what it was within {self.recover_s:.0f} s")
                del ep.recover[inc]
        ep.last_cursor = (cursor, t, inc)
        ep.hist.append((t, cursor, inc))
        while ep.hist and ep.hist[0][0] < t - 120:
            ep.hist.popleft()

    def dead_letters(self, label, n, t, after=60.0, since=0.0):
        ep = self.by_label[label]
        if n and ep.cls in MUST_DELIVER and ep.c0 is not None and not ep.ambiguous and t - ep.created > after:
            self.v.add("F_dead_letter", ep=label, dead=n, t=t)

    def settle(self, label, cursor, final=False, now=None):
        """Everything above the endpoint's last settled cursor, up to `cursor`, is the service's claim of delivered-or-dead: the ledger must agree."""
        ep = self.by_label[label]
        if ep.c0 is None or ep.ambiguous:
            return
        now = self.t_last if now is None else max(now, self.t_last) if final else now
        for ev in list(ep.unknown):
            e = self.ev.get(ev)
            if e is not None:
                ep.unknown.discard(ev)
                self._judge(ep, ev, e, final, now)
        i = ep.settled + 1
        while i <= cursor:
            e = self.ev.get(i)
            if e is None:
                ep.unknown.add(i)
            else:
                self._judge(ep, i, e, final, now)
            i += 1
        if cursor > ep.settled:
            ep.settled = cursor

    def _judge(self, ep, ev, e, final, now=None):
        name = type_name(e[0]) if e[0] != UNKNOWN_TYPE else None
        if e[2]:
            name = CRON_TYPE
        exp = ep.expect(self.seed, name, ev)
        if exp == "skip" or ev <= ep.c0:
            return
        c = self.cnt.get((ep.idx, ev))
        got = c[0] if c else 0
        if exp == "never":
            if got:
                self.v.add("F_never", ep=ep.label, ev=ev, deliveries=got)
        elif got == 0:
            if self.max_age_s and now is not None and now - e[3] >= self.max_age_s - self.EXPIRY_SLACK_S:
                # the cursor is past an event that is older than the service's hard maximum age: the service moves cursors past what it had not delivered when it drops a segment for age
                # (it counts them in events_expired, which `finish` holds this number against). Only events older than the age: a younger one is judged below.
                self.expired_excused[ep.label] += 1
                self.explained["expired"] += 1
                ep.deferred.discard(ev)
                return
            if ep.cls in ("sick", "dead") and not final:
                # the first attempt can be later than the acknowledgement by what the service's own lag says an event waits (the margin): an event acknowledged before a window
                # whose first attempt fell in it can die in it. An event acknowledged long before the window is not excused, and what is deferred is judged again at the end.
                margin = 2.0 + ep.lag_s
                if ev in ep.failed_once or any(a <= e[3] + margin and e[3] <= b + 70.0 for a, b in ep.windows):
                    ep.deferred.add(ev)
                    return
            self.v.add("A_missing_final" if final else "A_missing", ep=ep.label, ev=ev, type=name, n=e[1])

    def can_receive(self, idx, now):
        """Can a delivery still come to this endpoint? Yes while it is not retired, and for a while after (its receiver is taken away 12 s after, and a request may be on the wire)."""
        ep = self.by_idx.get(idx)
        return ep is not None and (ep.retired is None or now < ep.retired + self.RETIRED_GRACE_S)

    def purge(self, now, min_settled=None):
        """Forget what is old and settled everywhere: the window is `keep_s` of the newest events.

        The floor is the lowest settled cursor of the endpoints that are judged. An endpoint that is not (one whose creation was not answered, one that is retired) does not hold it down,
        but what is kept **for deliveries that can still come to it** is not purged with it: a replay noted for it (`repl`) stays while its receiver may still be answering, so that a
        late delivery is not taken for one nobody asked for (C_filter), and a late delivery of an event below the floor is not a phantom (`ingest`)."""
        live = [ep for ep in self.by_idx.values() if ep.retired is None and ep.c0 is not None and not ep.ambiguous]
        floor = min([ep.settled for ep in live], default=0) if min_settled is None else min_settled
        cutoff = now - self.keep_s
        if len(self.ev) > self.max_ev and min_settled is None:
            # an endpoint is further behind than the window can hold: cut the window rather than the memory, and say so (the run is not valid for that endpoint)
            older = sorted(self.ev)[: len(self.ev) - self.max_ev // 2]
            floor = max(floor, older[-1])
            cutoff = max(cutoff, now - 2.0)
            self.stats["forced_purge"] += 1
            if self.stats["forced_purge"] == 1:
                self.v.add("G_checker_overflow", events=len(self.ev), cut_at=older[-1], why="an endpoint is more events behind than the checker's window holds")
        drop = [i for i, e in self.ev.items() if i <= floor and e[3] < cutoff]
        for i in drop:
            e = self.ev.pop(i)
            if not e[4] and e[2] == 0 and e[1] not in self.posted_n:
                self.v.add("P_phantom", ev=i, n=e[1])
            if e[2] == 0:
                # the number is needed only while the event is in the window: it goes with it (it used to be trimmed by value once the set passed 600,000, which dropped the numbers of
                # events that were still in the window and made them phantoms)
                self.n2id.pop(e[1], None)
                self.posted_n.discard(e[1])
                self.keyed.discard(e[1])
        if drop:
            dset = max(drop)
            self.floor = max(self.floor, dset)
            self.cnt = {k: c for k, c in self.cnt.items() if c[1] >= cutoff or k[1] > self.floor}
            self.replayed = {k: v for k, v in self.replayed.items() if k > self.floor}
            if len(self.n2id) > 400000:
                self.n2id.clear()
        if self.repl:
            self.repl = Counter({k: c for k, c in self.repl.items() if k[1] > self.floor or self.can_receive(k[0], now)})
        # an endpoint retired for an hour is forgotten, and so is what the checker held of it
        for idx in [i for i, ep in self.by_idx.items() if ep.retired is not None and now - ep.retired > self.RETIRED_FORGET_S]:
            ep = self.by_idx.pop(idx)
            self.by_label.pop(ep.label, None)
            self.forgotten.add(idx)
        if len(self.recoveries) > 5000:
            del self.recoveries[:-2000]
        if len(self.away) > 2000:
            del self.away[:-1000]

    def retire(self, label, t):
        ep = self.by_label[label]
        ep.retired = t

    # ---- the end
    def finish(self, last_id, cursors, caught_up=True, now=None, expired_by_service=None):
        """`cursors`: {label: cursor read after everything drained}. The final judgement of what is still open. `expired_by_service`: the events the service says it expired
        (`events_expired`, summed over the incarnations as far as they were sampled), against the losses excused as expired."""
        for label, ep in self.by_label.items():
            if ep.retired is not None or ep.ambiguous or ep.c0 is None:
                continue
            c = cursors.get(label)
            if c is None:
                continue
            self.settle(label, c, final=True, now=now)
            if caught_up and ep.cls != "dead" and c < last_id:
                self.v.add("END_lag", ep=label, cursor=c, last_id=last_id)
            for ev in sorted(ep.deferred):
                got = self.cnt.get((ep.idx, ev))
                if not got:
                    self.v.add("A_missing_final", ep=label, ev=ev, why="died at a sick endpoint and was never replayed")
            ep.deferred.clear()
            for ev in sorted(ep.unknown):
                self.v.add("A_unseen_event", ep=label, ev=ev)
            ep.unknown.clear()
        for ev, e in list(self.ev.items()):
            if not e[4] and e[2] == 0 and e[1] not in self.posted_n:
                self.v.add("P_phantom", ev=ev, n=e[1])
        excused = sum(self.expired_excused.values())
        if excused and expired_by_service is not None and excused > expired_by_service * 1.1 + 50:
            self.v.add("A_expired_unexplained", excused=excused, expired_by_service=expired_by_service, by_endpoint=dict(self.expired_excused))
        self._cron_final()

    def _cron_final(self):
        for src, secs in self.cron.items():
            ordered = sorted(secs)
            period = self.cron_period.get(src, 1)
            for s in ordered:
                if len(secs[s]) > 1:
                    self.v.add("I_cron_twice", schedule=src, second=s, events=sorted(secs[s]))
            for a, b in zip(ordered, ordered[1:]):
                if b - a > period + 1 and not self._away_covers(a, b):
                    self.v.add("I_cron_gap", schedule=src, from_=a, to=b, seconds=b - a)

    # How long after a database fault ends a schedule may still be silent. The schedules are read through the database, and what the service documents
    # (docs/configuration.md) is: a request that is waiting with no byte coming back is given up and remade after `pg-request-ms` (10 s), an attempt to
    # connect may take `pg-attempt-ms` (5 s), and the wait between attempts is at most `pg-backoff-max-ms` (5 s). A blackholed database that comes back
    # can therefore take up to 20 s to be used again; the 5 s this had been (a gap of 14 to 19 s after a fault in a 40-minute validation run, found by
    # the harness's author) was shorter than what the service says it does. A service that is away (a kill, a stop) keeps the 5 s.
    DB_RECOVERY_S = 20.0
    AWAY_SLACK_S = 5.0

    def _away_covers(self, a, b):
        """Is the gap a..b (the scheduled seconds either side of it) explained by the service or the database being away? (5 s of slack before it and
        after a service; `DB_RECOVERY_S` after a database.)"""
        for (x, y, kind) in self.away:
            after = self.DB_RECOVERY_S if kind == "pg" else self.AWAY_SLACK_S
            if x - self.AWAY_SLACK_S <= b and y + after >= a:
                return True
        return False

    # ---- the numbers the report wants
    def summary(self):
        by_cls = defaultdict(lambda: Counter())
        for ep in self.by_idx.values():
            c = by_cls[ep.cls]
            c["endpoints"] += 1
            c["records"] += ep.recs
            c["delivered"] += ep.eff
            c["failed_attempts"] += ep.nonEff
        kills = sorted(self.kill_repeats.values())
        return {"records": self.stats["records"], "acked": self.stats["acked"], "repeats": self.stats["repeats"], "repeats_explained": dict(self.explained),
                "cursor_recovery_s": {"n": len(self.recoveries), "max": max(self.recoveries) if self.recoveries else 0, "median": sorted(self.recoveries)[len(self.recoveries) // 2] if self.recoveries else 0},
                "repeats_per_kill": {"kills_with_repeats": len(kills), "max": kills[-1] if kills else 0, "total": sum(kills)},
                "by_class": {k: dict(v) for k, v in by_cls.items()},
                "violations": dict(self.v.count)}
