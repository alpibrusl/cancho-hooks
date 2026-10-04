edition 5;

module wire;

import std.buffer;
import epx;
import sign;

// `wire` -- the request one delivery attempt sends (`docs/design.md` sections 4 and 35).
//
//     POST /hook HTTP/1.1
//     Host: receiver
//     Content-Type: application/json
//     webhook-id: evt_<id>
//     webhook-timestamp: <Unix seconds, when the attempt starts>
//     webhook-signature: v1,<signature under the secret>[ v1,<signature under the previous secret>]
//     <the endpoint's custom headers, one `Name: value` each>
//     Content-Length: <n>
//     Connection: close
//
// `webhook-id` is `evt_<id>`, the same for every attempt at the event, so a receiver can drop a repeat. While a rotation overlaps (the endpoint
// has a previous secret that has not expired), the signature header carries **both** signatures, the new one first, separated by a space, as
// the Standard Webhooks specification has it for key rotation: a receiver that knows either secret finds its own. The custom headers come after the
// three that the delivery sets, and cannot be any of those (`hdrs.forbidden`).
//
// `row` is the endpoint's row in the extras (`epx.ls`) and `now_ms` the Unix time in ms (the previous secret is signed with while `now_ms` is
// before the time it is valid until).

pub fn request[&h, &b, &k, &x](heap: &!h Heap, id: int, body: &b [byte], key: &k [byte], xt: &x [int], row: int, now_ms: int) -> [heap] buffer.Buffer {
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
        let wn = epx.wire_len(xt, row);
        var q = buffer.append(heap, buffer.empty(heap, len(body) + 448 + wn), "POST /hook HTTP/1.1\r\nHost: receiver\r\nContent-Type: application/json\r\nwebhook-id: ");
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
        q = buffer.append(heap, q, "\r\nConnection: close\r\n\r\n");
        q = buffer.append(heap, q, body);
        return q;
    }
}
