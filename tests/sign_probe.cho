edition 5;

// A probe for `sign`: `sign_probe sig <secret> <id> <timestamp> <payload>` prints the `webhook-signature` value, and
// `sign_probe b64 <text>` / `sign_probe unb64 <text>` print the base64 of a text and the text of a base64 (or `error`).
// `tests/sign_test.py` compares all three with independent implementations.

import std.io;
import sign;

fn main(world: World) -> [] int {
    let Split { io, ffi, fs, heap, args, net, clock } = split(world);
    release(ffi);
    release(fs);
    release(net);
    release(clock);
    borrow mut heap as &!h in {
        borrow mut io as &!i in {
            borrow args as &g in {
                if arg_count(g) > 2 {
                    let mode = arg(g, 1);
                    let first = arg(g, 2);
                    let big = box_slice(h, 4 * len(first) + 64, byte_of(0));
                    borrow mut big as &!bw in {
                        let buf = contents(bw);
                        if int_of(mode[0]) == 's' && arg_count(g) > 5 {
                            // The key is the decoded secret; the key bytes go in a second box.
                            let keybuf = box_slice(h, len(first) + 8, byte_of(0));
                            var klen = 0 - 1;
                            borrow mut keybuf as &!kw in {
                                klen = sign.secret_key(first, contents(kw));
                                if klen < 0 {
                                    io.write_all(i, "error");
                                } else {
                                    sign.signature(h, contents(kw)[0..klen], arg(g, 3), arg(g, 4), arg(g, 5), buf);
                                    io.write_all(i, buf[0..47]);
                                }
                            }
                            unbox_slice(h, keybuf);
                        } else if int_of(mode[0]) == 'b' {
                            let n = sign.b64_encode(first, buf);
                            io.write_all(i, buf[0..n]);
                        } else if int_of(mode[0]) == 'u' {
                            let n = sign.b64_decode(first, buf);
                            if n < 0 {
                                io.write_all(i, "error");
                            } else {
                                io.write_all(i, buf[0..n]);
                            }
                        }
                    }
                    unbox_slice(h, big);
                }
            }
            io.newline(i);
        }
    }
    release(heap);
    release(args);
    release(io);
    return 0;
}
