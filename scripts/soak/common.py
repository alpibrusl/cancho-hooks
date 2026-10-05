"""What the parts of the soak harness share (docs/soak.md): the event types and their proportions, the classes of endpoint, the
ledger formats, and the small deterministic functions the receivers and the checker must agree on.

Standard library only. Nothing here is the thing under test."""
import hashlib
import os
import struct
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TESTS = os.path.join(ROOT, "tests")
if TESTS not in sys.path:
    sys.path.insert(0, TESTS)

# ---- the stream ------------------------------------------------------------------------------------------------------

# (type, weight in per cent): fixed, so that a class of endpoint's share of the stream is known.
TYPES = [("user.created", 30), ("user.updated", 25), ("order.paid", 15), ("order.refunded", 6), ("invoice.paid", 10), ("ping", 12), ("rare.event", 2)]
TYPE_NAMES = [t for t, _ in TYPES]
TYPE_CODE = {t: i for i, t in enumerate(TYPE_NAMES)}
CRON_TYPE = "cron.tick"
TYPE_NAMES.append(CRON_TYPE)
TYPE_CODE[CRON_TYPE] = len(TYPE_NAMES) - 1
UNKNOWN_TYPE = 255


def type_name(code):
    return TYPE_NAMES[code] if code < len(TYPE_NAMES) else "?"


def wanted(patterns, typ):
    """Does an endpoint with these event type patterns want an event of type `typ`? (docs/design.md section 35: no list is everything; `*` is everything,
    an event with no type too; `a.*` is a prefix; anything else is exact.)"""
    if not patterns:
        return True
    for p in patterns:
        if p == "*":
            return True
        if typ is None:
            continue
        if p.endswith(".*"):
            if typ.startswith(p[:-1]):
                return True
        elif p == typ:
            return True
    return False


def share(patterns):
    """The fraction of the poster's stream (cron excluded) that these patterns want."""
    return sum(w for t, w in TYPES if wanted(patterns, t)) / 100.0


# ---- classes of endpoint ----------------------------------------------------------------------------------------------

# class -> (event type patterns, parameters). `must`: every wanted event must be delivered (a dead letter is a violation).
CLASSES = {
    "oracle": ([], {"must": True}),
    "healthy": ([], {"must": True}),
    "healthy2": (["rare.event", "order.refunded"], {"must": True}),
    "filter": (["user.*", "invoice.paid"], {"must": True}),
    "slow": (["order.*"], {"must": True, "delay": [0.02, 0.12]}),
    "flapping": (["user.updated", "order.*"], {"must": True}),
    "http5xx": (["order.paid", "ping", "invoice.paid"], {"must": True}),
    "gone": (["invoice.*", "ping"], {"must": False, "poison": 0.01}),
    "dead": (["rare.event"], {"must": False}),
    "https": (["user.*"], {"must": True, "tls": True}),
    "rate": (["user.created"], {"must": True}),
    "sick": (["ping", "order.paid"], {"must": False}),
    "churn": (["user.*", "order.*"], {"must": True}),
}

# The order in which `--endpoints N` takes them, and what the ones past the twelfth are.
BASE_ORDER = ["oracle", "healthy", "filter", "slow", "flapping", "http5xx", "gone", "dead", "https", "rate", "sick", "healthy2"]
EXTRA = [("healthy2", ["order.*"]), ("filter", ["user.created", "ping"]), ("slow", ["invoice.*"]), ("http5xx", ["user.updated"]),
         ("https", ["order.paid"]), ("healthy2", ["ping"])]

EP_MAX = 62


def plan_endpoints(n):
    """[(class, patterns)] for N long-lived endpoints."""
    out = []
    for i in range(n):
        if i < len(BASE_ORDER):
            cls = BASE_ORDER[i]
            out.append((cls, list(CLASSES[cls][0])))
        else:
            cls, pats = EXTRA[(i - len(BASE_ORDER)) % len(EXTRA)]
            out.append((cls, list(pats)))
    return out


def h01(seed, *parts):
    """A number in [0, 1) that is a function of its arguments only: what the receiver and the checker both use to decide which events are poison for an endpoint,
    how long a flapping endpoint is down, and so on."""
    key = "/".join([str(seed), *[str(p) for p in parts]]).encode()
    return int.from_bytes(hashlib.blake2b(key, digest_size=8).digest(), "big") / 2.0 ** 64


def poison(seed, label, ev, p):
    return h01(seed, "poison", label, ev) < p


def fail_count(seed, label, ev):
    """How many attempts of an event a `http5xx` endpoint fails first: 0 or 1 (the retry horizon has to outlast this and an outage on top of it)."""
    return int(h01(seed, "fails", label, ev) * 2)


FLAP_CYCLE = 30.0


def flap_window(seed, label, cycle):
    """(start offset, length, mode) of the down time in cycle number `cycle`: 3 to 8 s, somewhere in the 30 s, by one of three kinds of down."""
    length = 3.0 + 5.0 * h01(seed, "flaplen", label, cycle)
    start = (FLAP_CYCLE - length - 1.0) * h01(seed, "flapat", label, cycle)
    mode = ("refuse", "reset", "close")[int(h01(seed, "flapmode", label, cycle) * 3)]
    return start, length, mode


# ---- the ledgers -------------------------------------------------------------------------------------------------------

# The receivers' ledger: one record for every request that reached a receiver, written before the receiver answers.
#   t       when the request was fully read (receiver's clock, Unix seconds)
#   ep      the endpoint's index in spec.json (not the service's id: the harness's own name for it)
#   ev      the event id the service gave (from `webhook-id`)
#   n       the poster's number of the event (a cron event: the scheduled second)
#   src     0 for the poster's events, else the schedule's id
#   typ     the type's code (TYPE_CODE), 255 if the body had none
#   flags   below
#   status  the status the receiver answered (0: it closed without one)
#   e2e_ms  the receiver's clock minus the time the poster sent the event (0 for cron), capped
REC = struct.Struct("<dHQQHBBHI")
F_SIG, F_EFF, F_TS, F_BODY, F_TWO, F_NEW, F_OLD, F_RISK = 1, 2, 4, 8, 16, 32, 64, 128   # F_RISK: answered later than half a second

# The poster's ledger: every event that got a `202`, and every one that did not (in doubt).
#   t_ack, t_send, n, id (0 if in doubt), typ, flags, pad
ACK = struct.Struct("<ddQQBBH")
A_KEYED, A_INDOUBT, A_BIG = 1, 2, 4


def read_records(path, offset, st=REC, limit=None):
    """The whole records of a ledger file from `offset`: (list of tuples, new offset). A torn tail (the writer died in a write) is left."""
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read(limit) if limit else f.read()
    except OSError:
        return [], offset
    n = len(data) // st.size
    out = [st.unpack_from(data, i * st.size) for i in range(n)]
    return out, offset + n * st.size


def crc_pad(pad):
    return zlib.crc32(pad.encode()) & 0xFFFFFFFF


def secret_bytes(s):
    import base64
    return base64.b64decode(s[len("whsec_"):])


def chaos_module():
    """tests/chaos.py (an independent reader of the log) reads sys.argv when it is imported: give it an empty one."""
    saved = sys.argv
    sys.argv = ["chaos.py"]
    try:
        import chaos
    finally:
        sys.argv = saved
    return chaos
