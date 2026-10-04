edition 5;

module destination;

// `destination` -- where a delivery may go (`docs/design.md` section 26).
//
// An endpoint is an address the service will `POST` to from inside the operator's network, so whoever can name an address can
// make the service call something only it can reach: a database on `10.0.0.5`, a cloud metadata service on `169.254.169.254`, an
// admin port on `127.0.0.1`. (Server-side request forgery.) The rule here is the narrowest one that is checkable:
//
//   * the host must be an IPv4 **literal**, four decimal numbers of 0 to 255 with no leading zeros and nothing else. A name is
//     refused because the service cannot know what it resolves to: the lookup is `getaddrinfo` inside the connect, so a check
//     before it and the connection after it could disagree (a name can change in between, DNS rebinding), and it blocks the
//     loop besides. The forms `inet_aton` accepts and a person does not expect (`127.1`, `2130706433`, `0x7f.0.0.1`,
//     `0177.0.0.1`) are refused for the same reason: they are not a literal in the sense above, and what this module does not
//     understand it does not allow;
//   * and the address must be public: none of the ranges below.
//
// `allow-private-hosts 1` turns the rule off, for a service whose receivers are on its own network (and the demos and the tests).

// The four numbers of `text` packed as `a * 2^24 + b * 2^16 + c * 2^8 + d`, or -1 if `text` is not exactly a dotted quad of them.
pub fn address[&t](text: &t [byte]) -> [] int {
    var value = 0;
    var groups = 0;
    var digits = 0;
    var n = 0;
    var i = 0;
    while i <= len(text) {
        var c = 0 - 1;
        if i < len(text) {
            c = int_of(text[i]);
        }
        if c >= '0' && c <= '9' {
            if digits == 1 && n == 0 {
                return 0 - 1;
            }
            n = n * 10 + (c - '0');
            digits = digits + 1;
            if digits > 3 || n > 255 {
                return 0 - 1;
            }
        } else if (c == '.' || i == len(text)) && digits > 0 && groups < 4 {
            value = value * 256 + n;
            groups = groups + 1;
            n = 0;
            digits = 0;
        } else {
            return 0 - 1;
        }
        i = i + 1;
    }
    if groups != 4 {
        return 0 - 1;
    }
    return value;
}

// Whether the packed address `a` is outside every range that is not for the public internet: 0/8 (this network), 10/8, 100.64/10
// (carrier-grade NAT), 127/8 (loopback), 169.254/16 (link-local: the cloud metadata address is in it), 172.16/12, 192.0.0/24 and
// 192.0.2/24 (protocol assignments and documentation), 192.88.99/24, 192.168/16, 198.18/15 (benchmarking), 198.51.100/24 and
// 203.0.113/24 (documentation), 224/4 (multicast) and 240/4 (reserved, which holds the broadcast address).
pub fn is_public(a: int) -> [] bool {
    let first = a / 16777216;
    let second = a / 65536 % 256;
    let third = a / 256 % 256;
    if first == 0 || first == 10 || first == 127 || first >= 224 {
        return false;
    }
    if first == 100 && second >= 64 && second <= 127 {
        return false;
    }
    if first == 169 && second == 254 {
        return false;
    }
    if first == 172 && second >= 16 && second <= 31 {
        return false;
    }
    if first == 192 && second == 168 {
        return false;
    }
    if first == 192 && second == 0 && (third == 0 || third == 2) {
        return false;
    }
    if first == 192 && second == 88 && third == 99 {
        return false;
    }
    if first == 198 && (second == 18 || second == 19) {
        return false;
    }
    if first == 198 && second == 51 && third == 100 {
        return false;
    }
    if first == 203 && second == 0 && third == 113 {
        return false;
    }
    return true;
}

// Whether a delivery may go to `host` when private hosts are not allowed.
pub fn allowed[&t](host: &t [byte]) -> [] bool {
    let a = address(host);
    return a >= 0 && is_public(a);
}
