edition 5;

module resolve;

import destination;
import std.bytes;

// `resolve` -- which name server the service asks (`docs/design.md` section 40).
//
// Names are resolved by the service itself (`attempt.ls`, `dns.ls`): `getaddrinfo` blocks the loop for as long as the answer takes (302 ms for a 300 ms
// answer, measured by the lex-sys spike) and resolves a second time at the connection. So the service needs the address of a name server. The `dns-server`
// setting is one (`ip` or `ip:port`); without it the first IPv4 `nameserver` of `/etc/resolv.conf` is, on port 53. What follows from asking one server over TCP
// and nothing else: no `/etc/hosts` (except that `localhost` is the loopback, `destination.is_localhost`), no search list (a name is used as written), no second
// server if the first is down, no UDP, no IPv6 name server and no AAAA records.

// The packed address (`destination.address`) of the first `nameserver` line of resolv.conf text whose address is an IPv4 literal, or 0 if there is none. A server with
// an IPv6 address is passed over.
pub fn nameserver[&t](text: &t [byte]) -> [] int {
    var at = 0;
    while at < len(text) {
        var end = at;
        while end < len(text) && int_of(text[end]) != '\n' {
            end = end + 1;
        }
        var s = at;
        while s < end && (int_of(text[s]) == ' ' || int_of(text[s]) == '\t') {
            s = s + 1;
        }
        if s + 10 < end && bytes.starts_with(text[s..end], "nameserver") && (int_of(text[s + 10]) == ' ' || int_of(text[s + 10]) == '\t') {
            var v = s + 10;
            while v < end && (int_of(text[v]) == ' ' || int_of(text[v]) == '\t') {
                v = v + 1;
            }
            var e = v;
            while e < end && int_of(text[e]) != ' ' && int_of(text[e]) != '\t' && int_of(text[e]) != '\r' && int_of(text[e]) != '#' {
                e = e + 1;
            }
            let a = destination.address(text[v..e]);
            if a > 0 {
                return a;
            }
        }
        at = end + 1;
    }
    return 0;
}

// Read the name server from `/etc/resolv.conf`: its packed address, or 0.
pub fn from_system[&f](fs: &f Fs("")) -> [fs_read("")] int {
    var found = 0;
    region r {
        let buf = alloc_slice[r](8192, byte_of(0));
        let got = fs_read(fs, "/etc/resolv.conf", buf);
        if got > 0 {
            found = nameserver(buf[0..got]);
        }
    }
    return found;
}
