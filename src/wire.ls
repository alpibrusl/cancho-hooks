edition 5;

module wire;

import std.buffer;
import destination;
import epx;
import sign;

// `wire` -- the request one delivery attempt sends (`docs/design.md` sections 4 and 35).
//
//     POST /hook HTTP/1.1
//     Host: receiver                      (or the host name, and `:port` unless it is the scheme's own, for an endpoint that has a name or is `https`)
//     Content-Type: application/json
//     webhook-id: evt_<id>
//     webhook-timestamp: <Unix seconds, when the attempt starts>
//     webhook-signature: v1,<signature under the secret>[ v1,<signature under the previous secret>]
//     <the endpoint's custom headers, one `Name: value` each>
//     Content-Length: <n>
//
// `webhook-id` is `evt_<id>`, the same for every attempt at the event, so a receiver can drop a repeat. While a rotation overlaps (the endpoint
// has a previous secret that has not expired), the signature header carries **both** signatures, the new one first, separated by a space, as
// the Standard Webhooks specification has it for key rotation: a receiver that knows either secret finds its own. The custom headers come after the
// three that the delivery sets, and cannot be any of those (`hdrs.forbidden`).
//
// `row` is the endpoint's row in the extras (`epx.ls`) and `now_ms` the Unix time in ms (the previous secret is signed with while `now_ms` is
// before the time it is valid until). `host` and `port` are the endpoint's, as stored (`destination.ls`): a plain endpoint at an IPv4 address says `Host: receiver`
// as it always did; one with a name, or with `https`, says its name (a server that serves several sites by the `Host` header needs to), with the port unless it
// is 80 for `http` or 443 for `https`.

pub fn request[&h, &b, &k, &x, &o](heap: &!h Heap, id: int, body: &b [byte], key: &k [byte], xt: &x [int], row: int, now_ms: int, host: &o [byte], port: int) -> [heap] buffer.Buffer {
    // The region is left by falling out of it: one left by a `return` is not given back, and this runs once for every attempt (`docs/lexsys-log-retention.md`, gap 6).
    let wn = epx.wire_len(xt, row);
    var q = buffer.empty(heap, len(body) + 448 + wn + len(host));
    region a {
        let msg_id = alloc_slice[a](24, byte_of(0));
        msg_id[0] = byte_of('e');
        msg_id[1] = byte_of('v');
        msg_id[2] = byte_of('t');
        msg_id[3] = byte_of('_');
        let id_len = 4 + sign.nat_text(id, msg_id[4..24]);
        let stamp = alloc_slice[a](24, byte_of(0));
        let stamp_len = sign.nat_text(now_ms / 1000, stamp);
        let sig = alloc_slice[a](48, byte_of(0));
        sign.signature(heap, key, msg_id[0..id_len], stamp[0..stamp_len], body, sig);
        q = buffer.append(heap, q, "POST /hook HTTP/1.1\r\nHost: ");
        let name = destination.bare(host);
        if destination.address(name) >= 0 && !destination.is_https(host) {
            q = buffer.append(heap, q, "receiver");
        } else {
            q = buffer.append(heap, q, name);
            if !(destination.is_https(host) && port == 443) && !(!destination.is_https(host) && port == 80) {
                q = buffer.append(heap, q, ":");
                q = buffer.push_nat(heap, q, port);
            }
        }
        q = buffer.append(heap, q, "\r\nContent-Type: application/json\r\nwebhook-id: ");
        q = buffer.append(heap, q, msg_id[0..id_len]);
        q = buffer.append(heap, q, "\r\nwebhook-timestamp: ");
        q = buffer.append(heap, q, stamp[0..stamp_len]);
        q = buffer.append(heap, q, "\r\nwebhook-signature: ");
        q = buffer.append(heap, q, sig[0..47]);
        if epx.old_active(xt, row, now_ms) {
            let old = alloc_slice[a](96, byte_of(0));
            let n = epx.old_len(xt, row);
            var j = 0;
            while j < n {
                old[j] = byte_of(epx.old_byte(xt, row, j));
                j = j + 1;
            }
            let sig2 = alloc_slice[a](48, byte_of(0));
            sign.signature(heap, old[0..n], msg_id[0..id_len], stamp[0..stamp_len], body, sig2);
            q = buffer.append(heap, q, " ");
            q = buffer.append(heap, q, sig2[0..47]);
        }
        q = buffer.append(heap, q, "\r\n");
        if wn > 0 {
            let hb = alloc_slice[a](2048, byte_of(0));
            var k2 = 0;
            while k2 < wn {
                hb[k2] = byte_of(epx.wire_byte(xt, row, k2));
                k2 = k2 + 1;
            }
            q = buffer.append(heap, q, hb[0..wn]);
        }
        q = buffer.append(heap, q, "Content-Length: ");
        q = buffer.push_nat(heap, q, len(body));
        // No `Connection: close`: the connection may be kept for the endpoint's next request (`docs/design.md` section 53).
        q = buffer.append(heap, q, "\r\n\r\n");
        q = buffer.append(heap, q, body);
    }
    return q;
}
