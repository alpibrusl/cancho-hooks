"""How much the machine is taking from this process, measured while a test runs (for the checks that are of a time and not of a count).

    meter = LoadMeter()          # a thread; it starts at once
    ...                          # the test
    meter.stop()
    meter.stall_s                # the longest time this process waited for its turn beyond what it asked for (a 5 ms sleep that took 105 ms: 0.1)
    meter.slowdown()             # wall clock / processor time of a short spin, at the 90th percentile of those made every 250 ms: 1.0 on a machine with a core to spare

A check of a latency or of a rate seen from outside (a request answered in so many milliseconds, so many requests in a second at a receiver) is a measure of the service and of the
machine the test runs on, and on a shared runner the second is not ours. These two numbers say what the machine did to the test process while the check was being made, so that a
bound can be stated for a machine that gives the whole processor (`slowdown` 1) and be scaled by what this one gave, and not by a guess.
"""
import threading
import time


class LoadMeter:
    def __init__(self, spin_every=0.25, spin_s=0.01):
        self.stall_s, self.factors = 0.0, []
        self._stop = threading.Event()
        self._spin_every, self._spin_s = spin_every, spin_s
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        next_spin = time.perf_counter() + self._spin_every
        while not self._stop.is_set():
            t0 = time.perf_counter()
            time.sleep(0.005)
            self.stall_s = max(self.stall_s, time.perf_counter() - t0 - 0.005)
            if time.perf_counter() >= next_spin:
                c0, w0 = time.thread_time(), time.perf_counter()
                while time.thread_time() - c0 < self._spin_s:
                    pass
                cpu = time.thread_time() - c0
                self.factors.append((time.perf_counter() - w0) / cpu if cpu > 0 else 1.0)
                next_spin = time.perf_counter() + self._spin_every

    def stop(self):
        self._stop.set()
        self._thread.join(2)

    def slowdown(self, q=0.9):
        f = sorted(self.factors)
        return max(1.0, f[min(len(f) - 1, int(q * len(f)))]) if f else 1.0
