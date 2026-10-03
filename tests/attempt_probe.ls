edition 5;

// A probe for `deliver.attempt`: `attempt_probe <host> <port> <timeout-ms>` sends one POST and prints the answer
// (a status, or a negative reason). It exists to test the delivery step alone, before the service uses it.

import std.io;
import deliver;

fn number_of[&t](text: &t [byte]) -> [] int {
    var n = 0;
    var i = 0;
    while i < len(text) {
        n = n * 10 + (int_of(text[i]) - 48);
        i = i + 1;
    }
    return n;
}

fn main(world: World) -> [] int {
    let Split { io, ffi, fs, heap, args, net, clock } = split(world);
    release(ffi);
    release(fs);
    release(heap);
    var port = 0;
    var timeout = 0;
    region a {
        let host = alloc_slice[a](256, byte_of(0));
        var host_len = 0;
        borrow args as &g in {
            if arg_count(g) > 3 {
                let h = arg(g, 1);
                var k = 0;
                while k < len(h) && k < 255 {
                    host[k] = h[k];
                    k = k + 1;
                }
                host_len = len(h);
                port = number_of(arg(g, 2));
                timeout = number_of(arg(g, 3));
            }
        }
        var status = 0 - 99;
        borrow net as &nn in {
            borrow clock as &c in {
                status = deliver.attempt(nn, host[0..host_len], port, "POST /h HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}", c, timeout);
            }
        }
        borrow mut io as &!i in {
            if status < 0 {
                io.write_all(i, "-");
                io.print_int(i, 0 - status);
            } else {
                io.print_int(i, status);
            }
            io.newline(i);
        }
    }
    release(net);
    release(clock);
    release(args);
    release(io);
    return 0;
}
