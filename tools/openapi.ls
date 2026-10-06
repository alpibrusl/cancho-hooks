edition 5;

// `hooks-openapi` -- prints the service's OpenAPI document to standard output and exits 0 (`docs/design.md` section 52).
//
// The document comes from the same declaration the service routes with (`src/api.ls`). This program is not the service: it has no
// network, no clock, no filesystem and no foreign code, and writes to the console only. `scripts/openapi.sh` runs it and keeps
// `docs/openapi.json` the printed document.

import std.buffer;
import std.io;
import api;
import schema;
import web;

fn main(world: World) -> [] int {
    let Split { io, ffi, fs, heap, args, net, clock } = split(world);
    release(ffi);
    release(fs);
    release(net);
    release(clock);
    release(args);
    borrow mut heap as &!h in {
        let (declared, sc) = api.declare(h);
        borrow declared as &ar in {
            borrow sc as &sr in {
                let doc = api.document(h, ar, sr);
                borrow mut io as &!i in {
                    borrow doc as &d in {
                        io.write_all(i, buffer.bytes(d));
                    }
                    io.newline(i);
                }
                buffer.drop(h, doc);
            }
        }
        web.drop(h, declared);
        schema.drop(h, sc);
    }
    release(heap);
    release(io);
    return 0;
}
