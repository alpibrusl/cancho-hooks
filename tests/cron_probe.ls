edition 5;

// A probe for `cron`, for `tests/cron_test.py`, which compares it with an independent implementation:
//
//     cron_probe next  <0|1> <expression> <unix-second>    the first fire after it, or -1
//     cron_probe prev  <0|1> <expression> <unix-second>    the last fire up to it, or -1
//     cron_probe check <0|1> <expression>                  0 if the expression is accepted, else the code of the refusal
//     cron_probe iso   <unix-second>                       the time as YYYY-MM-DDTHH:MM:SSZ
//
// The second argument says whether the expression has a leading seconds field. Prints one number or text and a newline.

import std.io;
import cron;

fn number_of[&t](text: &t [byte]) -> [] int {
    var n = 0;
    var i = 0;
    while i < len(text) {
        n = n * 10 + (int_of(text[i]) - '0');
        i = i + 1;
    }
    return n;
}

fn say_int[&i, &b](out: &!i Io, scratch: &!b [byte], n: int) -> [io_write] int {
    var m = n;
    if n < 0 {
        io.write_all(out, "-");
        m = 0 - n;
    }
    var digits = 0;
    var k = m;
    while k > 0 {
        digits = digits + 1;
        k = k / 10;
    }
    if digits == 0 {
        digits = 1;
    }
    var j = digits;
    while j > 0 {
        scratch[j - 1] = byte_of('0' + m % 10);
        m = m / 10;
        j = j - 1;
    }
    return io.write_all(out, scratch[0..digits]);
}

fn main(world: World) -> [] int {
    let Split { io, ffi, fs, heap, args, net, clock } = split(world);
    release(ffi);
    release(fs);
    release(net);
    release(clock);
    release(heap);
    borrow mut io as &!i in {
        borrow args as &g in {
            region a {
                let scratch = alloc_slice[a](32, byte_of(0));
                let spec = alloc_slice[a](8, 0);
                if arg_count(g) > 2 {
                    let mode = arg(g, 1);
                    if int_of(mode[0]) == 'i' {
                        let n = cron.iso(number_of(arg(g, 2)), scratch);
                        if n == 0 {
                            io.write_all(i, "none");
                        } else {
                            io.write_all(i, scratch[0..n]);
                        }
                    } else if arg_count(g) > 3 {
                        let seconds = int_of(arg(g, 2)[0]) == '1';
                        let expr = arg(g, 3);
                        var answer = 0;
                        if int_of(mode[0]) == 'c' {
                            answer = cron.valid(expr, seconds, spec);
                        } else {
                            let code = cron.valid(expr, seconds, spec);
                            if code != 0 {
                                answer = 0 - 1000 - code;
                            } else if int_of(mode[0]) == 'n' && arg_count(g) > 4 {
                                answer = cron.next_after(spec, number_of(arg(g, 4)));
                            } else if arg_count(g) > 4 {
                                answer = cron.prev_upto(spec, number_of(arg(g, 4)));
                            }
                        }
                        say_int(i, scratch, answer);
                    }
                }
                io.newline(i);
            }
        }
    }
    release(args);
    release(io);
    return 0;
}
