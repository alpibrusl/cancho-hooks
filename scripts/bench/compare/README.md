# Comparing cancho-hooks with other open-source webhook services

`compare.py` runs one workload against several systems on one machine and measures the same things the same way. It is a harness, not a result: nothing here says which
system is better, and no figure from it is published without the machine, the versions and the settings beside it.

## What it compares

| system | what runs | why |
|---|---|---|
| `hooks` | the released container image, its own log in a volume, endpoints from `endpoints.conf` | the service as its README runs it |
| `hooks-pg` | the same image with a PostgreSQL: endpoints from the table, every delivery attempt a row | the same data work as the next one does |
| `svix` | `svix/svix-server`, PostgreSQL, Redis (queue) | the open-source Svix server as its documentation runs it; it implements the same Standard Webhooks signing |

Add a system by writing a class like `Svix` in `compare.py`: `start(E, sink_base)` brings it up with `E` endpoints at `127.0.0.1:sink_base+i` and returns where and how to post.
Convoy and Hook0 are the next candidates; they were not added because each needs its own reading of its API, and a system set up wrongly gives a number that is worse than none.

## What is measured

For each system and each number of endpoints `E` (default 1 and 10), three times, each on a fresh system and in an order that rotates:

* **Throughput.** `N` events posted over 64 keep-alive connections as fast as the service answers; the time until all `N x E` deliveries reached the receivers; the CPU of *every
  container of the system* (service, database, queue) per delivery, from the cgroups; peak memory of all of them; deliveries **missing** and **duplicated**, counted per
  endpoint and event at the receiver (a system that is fast because it loses or repeats deliveries must not look fast).
* **Latency.** Events posted at a fixed rate (default 100 and 500 a second) for 30 s, open loop: a request is timed from when it was *due*, so a service that falls behind
  shows the queue it makes. The figure is the time from the post to the first arrival at the receiver (p50, p99, max).
* **Idle.** The system with its endpoints configured and nothing sent, 30 s: CPU and memory.

## What keeps it fair, and what it cannot

* The same receivers (`sink.c`, C, keep-alive, 204), the same load generator (`load.c`, C), the same payload size (about 200 bytes), the same cores for the system under test
  (`--sut-cpus`, shared by all its containers). The receivers and the load run on cores of their own and are checked: a run in which either used more than 85% of its core is
  marked `INVALID` in the table.
* Every system acknowledges a post after it is stored. That is **not the same work**: cancho-hooks flushes a group commit of its log; Svix commits to PostgreSQL and enqueues in
  Redis (here with no persistence: `--save ""`, which is Svix's queue configured for speed, not for loss-proof restarts). The result says so beside the numbers; it cannot make
  them equal.
* The cancho-hooks service is one thread. Given four cores it uses one, and the table shows that, which is the honest picture of what you get by installing it, not a defect of
  the harness. Run once with `--sut-cpus 3` (one core for everyone) to compare per core.
* Defaults everywhere except what a local receiver needs (private addresses allowed). No system is tuned. If a vendor's documentation recommends a production profile, run that
  as a second configuration and say so.
* `--svix-image` defaults to `latest` for the smoke run. **For a result, pin a tag**; the image id is recorded either way.
* The CPU governor of every core used must be `performance`; the script refuses to run otherwise. On the benchmark host: `sudo cpupower frequency-set -g performance` before,
  and set it back after.

## Running it

```sh
python3 scripts/bench/compare/compare.py --smoke --out bench-smoke       # a minute: checks that every system is set up right, and delivers
python3 scripts/bench/compare/compare.py --out bench-out --svix-image svix/svix-server:<tag>
```

Output: `compare.md` (the tables, the machine, the images) and `compare.json` (every run). The smoke run does not need the governor and its numbers mean nothing.

## Publishing a result

Say: the machine, the versions, the settings, that it is one workload on one host, and where it is unfavourable to us. Do not write "faster than" or "more reliable than"; write
what was measured. A result goes to `docs/` with the run's `compare.json` beside it, and the page states it with the same narrowness as the soak.
