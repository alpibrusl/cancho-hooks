/* A delivery.seg for the worst start (docs/design.md section 41.9): `slots` endpoints in slots 0 to slots-1 (ids equal to the slots), each with `dead`
 * dead letters (events 1 to `dead`), so that every table of dead letters is full when `dead` is 2,048. The records are what src/state.ls writes
 * (`put_outcome`, 77 bytes each); the file begins with the header (kind 15, 62, format 2) and, if a slot of 62 or above is in use, the marker (kind 18).
 *
 *     gcc -O2 -o scripts/bench/mklog scripts/bench/mklog.c && scripts/bench/mklog delivery.seg 1024 2048
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <time.h>

static uint32_t table[256];
static void init(void) { for (uint32_t i = 0; i < 256; i++) { uint32_t c = i; for (int k = 0; k < 8; k++) c = (c & 1) ? (c >> 1) ^ 0x82F63B78u : c >> 1; table[i] = c; } }
static uint32_t crc32c(const unsigned char *p, size_t n) { uint32_t c = 0xFFFFFFFFu; while (n--) c = (c >> 8) ^ table[(c ^ *p++) & 0xFF]; return c ^ 0xFFFFFFFFu; }

static uint64_t seq = 0;
static void rec(FILE *f, uint64_t kind, uint64_t slot, uint64_t event, uint64_t attempts, uint64_t next_at) {
    unsigned char body[69];
    uint64_t ms = seq++, zero = 0; uint32_t one = 1, kl = 1, vl = 40;
    memcpy(body, &ms, 8); memcpy(body + 8, &zero, 8); memcpy(body + 16, &one, 4); memcpy(body + 20, &kl, 4); body[24] = 'o'; memcpy(body + 25, &vl, 4);
    uint64_t v[5] = {kind, slot, event, attempts, next_at}; memcpy(body + 29, v, 40);
    uint32_t len = 4 + 69, crc = crc32c(body, 69);
    fwrite(&len, 4, 1, f); fwrite(&crc, 4, 1, f); fwrite(body, 69, 1, f);
}

int main(int argc, char **argv) {
    if (argc < 4) { fprintf(stderr, "usage: mklog out slots dead\n"); return 2; }
    init();
    long slots = atol(argv[2]), dead = atol(argv[3]);
    uint64_t now = (uint64_t)time(NULL) * 1000;
    FILE *f = fopen(argv[1], "wb");
    rec(f, 15, 62, 2, 0, now);
    if (slots > 62) rec(f, 18, 0, 0, 0, 0);
    for (long e = 0; e < slots; e++) {
        rec(f, 10, e, e, 0, 0);
        for (long k = 1; k <= dead; k++) rec(f, 3, e, k, 10, now - 3600000 + k);
    }
    fclose(f);
    return 0;
}
