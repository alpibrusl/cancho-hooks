edition 5;

import std.test;
import sign;
import state;
import endpoints;
import destination;

// Where a delivery may go (`src/destination.ls`, `docs/design.md` section 26): which texts are an address, and which addresses are public.

fn is_allowed(host: &static [byte]) -> [] bool {
    return destination.allowed(host);
}

fn test_a_dotted_quad_is_an_address() -> [] int {
    test.assert_eq(destination.address("1.2.3.4"), 16909060);
    test.assert_eq(destination.address("0.0.0.0"), 0);
    test.assert_eq(destination.address("255.255.255.255"), 4294967295);
    test.assert_eq(destination.address("8.8.8.8"), 134744072);
    return 0;
}

fn test_anything_else_is_not() -> [] int {
    // names, the short and number forms a resolver accepts, leading zeros (octal to some), signs, spaces, IPv6, and wrong counts
    test.assert_eq(destination.address(""), 0 - 1);
    test.assert_eq(destination.address("localhost"), 0 - 1);
    test.assert_eq(destination.address("example.com"), 0 - 1);
    test.assert_eq(destination.address("127.1"), 0 - 1);
    test.assert_eq(destination.address("2130706433"), 0 - 1);
    test.assert_eq(destination.address("0x7f.0.0.1"), 0 - 1);
    test.assert_eq(destination.address("0177.0.0.1"), 0 - 1);
    test.assert_eq(destination.address("127.0.0.01"), 0 - 1);
    test.assert_eq(destination.address("00.0.0.1"), 0 - 1);
    test.assert_eq(destination.address("1.2.3"), 0 - 1);
    test.assert_eq(destination.address("1.2.3.4.5"), 0 - 1);
    test.assert_eq(destination.address("1.2.3."), 0 - 1);
    test.assert_eq(destination.address(".1.2.3"), 0 - 1);
    test.assert_eq(destination.address("1..2.3"), 0 - 1);
    test.assert_eq(destination.address("256.1.1.1"), 0 - 1);
    test.assert_eq(destination.address("1.1.1.1000"), 0 - 1);
    test.assert_eq(destination.address("1.1.1.1 "), 0 - 1);
    test.assert_eq(destination.address(" 1.1.1.1"), 0 - 1);
    test.assert_eq(destination.address("-1.1.1.1"), 0 - 1);
    test.assert_eq(destination.address("::1"), 0 - 1);
    test.assert_eq(destination.address("[::1]"), 0 - 1);
    test.assert_eq(destination.address("::ffff:127.0.0.1"), 0 - 1);
    return 0;
}

fn test_the_ranges_that_are_not_public_are_refused() -> [] int {
    test.assert(!is_allowed("0.0.0.0"));
    test.assert(!is_allowed("0.255.255.255"));
    test.assert(!is_allowed("10.0.0.0"));
    test.assert(!is_allowed("10.255.255.255"));
    test.assert(!is_allowed("100.64.0.0"));
    test.assert(!is_allowed("100.127.255.255"));
    test.assert(!is_allowed("127.0.0.1"));
    test.assert(!is_allowed("127.255.255.255"));
    test.assert(!is_allowed("169.254.0.0"));
    test.assert(!is_allowed("169.254.169.254"));
    test.assert(!is_allowed("172.16.0.0"));
    test.assert(!is_allowed("172.31.255.255"));
    test.assert(!is_allowed("192.0.0.1"));
    test.assert(!is_allowed("192.0.2.1"));
    test.assert(!is_allowed("192.88.99.1"));
    test.assert(!is_allowed("192.168.0.1"));
    test.assert(!is_allowed("192.168.255.255"));
    test.assert(!is_allowed("198.18.0.1"));
    test.assert(!is_allowed("198.19.255.255"));
    test.assert(!is_allowed("198.51.100.7"));
    test.assert(!is_allowed("203.0.113.7"));
    test.assert(!is_allowed("224.0.0.1"));
    test.assert(!is_allowed("239.255.255.255"));
    test.assert(!is_allowed("240.0.0.1"));
    test.assert(!is_allowed("255.255.255.255"));
    return 0;
}

fn test_the_edges_of_those_ranges_are_public() -> [] int {
    test.assert(is_allowed("1.1.1.1"));
    test.assert(is_allowed("8.8.8.8"));
    test.assert(is_allowed("9.255.255.255"));
    test.assert(is_allowed("11.0.0.0"));
    test.assert(is_allowed("100.63.255.255"));
    test.assert(is_allowed("100.128.0.0"));
    test.assert(is_allowed("126.255.255.255"));
    test.assert(is_allowed("128.0.0.1"));
    test.assert(is_allowed("169.253.255.255"));
    test.assert(is_allowed("169.255.0.0"));
    test.assert(is_allowed("172.15.255.255"));
    test.assert(is_allowed("172.32.0.0"));
    test.assert(is_allowed("192.0.1.1"));
    test.assert(is_allowed("192.0.3.1"));
    test.assert(is_allowed("192.88.98.1"));
    test.assert(is_allowed("192.88.100.1"));
    test.assert(is_allowed("192.167.255.255"));
    test.assert(is_allowed("192.169.0.0"));
    test.assert(is_allowed("198.17.255.255"));
    test.assert(is_allowed("198.20.0.0"));
    test.assert(is_allowed("198.51.99.1"));
    test.assert(is_allowed("198.51.101.1"));
    test.assert(is_allowed("203.0.112.1"));
    test.assert(is_allowed("203.0.114.1"));
    test.assert(is_allowed("223.255.255.255"));
    return 0;
}

// `allowed` is the rule of an address: a name is not one. (Unchanged: what a name may be is `host_ok`, below.)
fn test_a_name_is_never_allowed() -> [] int {
    test.assert(!is_allowed("example.com"));
    test.assert(!is_allowed("localhost"));
    test.assert(!is_allowed("8.8.8.8.nip.io"));
    test.assert(!is_allowed("0x08.8.8.8"));
    return 0;
}

fn test_the_endpoints_file_applies_the_rule() -> [] int {
    region a {
        let table = alloc_slice[a](32 * endpoints.stride(), 0);
        let blob = alloc_slice[a](512, byte_of(0));
        // whsec_ + base64("0123456789abcdef")
        let good = "1 8.8.8.8 9001 whsec_MDEyMzQ1Njc4OWFiY2RlZg==\n2 1.1.1.1 9002 whsec_MDEyMzQ1Njc4OWFiY2RlZg==\n";
        test.assert_eq(endpoints.parse(good, table, blob, false), 2);
        let loopback = "1 8.8.8.8 9001 whsec_MDEyMzQ1Njc4OWFiY2RlZg==\n2 127.0.0.1 9002 whsec_MDEyMzQ1Njc4OWFiY2RlZg==\n";
        test.assert_eq(endpoints.parse(loopback, table, blob, false), 0 - 2);
        test.assert_eq(endpoints.parse(loopback, table, blob, true), 2);
        // CHANGED (docs/design.md section 40): a name used to be refused at the write, because it could not be judged. It is a valid host now and is judged at every attempt
        // (`attempt.ls`, `tests/names_test.py`); what stays refused at the write is a text that is not a name.
        let named = "# a name\n1 example.com 9001 whsec_MDEyMzQ1Njc4OWFiY2RlZg==\n";
        test.assert_eq(endpoints.parse(named, table, blob, false), 1);
        test.assert_eq(endpoints.parse(named, table, blob, true), 1);
        let short = "1 127.1 9001 whsec_MDEyMzQ1Njc4OWFiY2RlZg==\n";
        test.assert_eq(endpoints.parse(short, table, blob, false), 0 - 1);
        // CHANGED: open used to accept any text; it does not make an address out of what is not one
        test.assert_eq(endpoints.parse(short, table, blob, true), 0 - 1);
        let secure = "1 https://hooks.example.com 443 whsec_MDEyMzQ1Njc4OWFiY2RlZg==\n2 8.8.8.8 9002 whsec_MDEyMzQ1Njc4OWFiY2RlZg==\n";
        test.assert_eq(endpoints.parse(secure, table, blob, false), 2);
        test.assert_eq(endpoints.scheme_of(table, 0), 1);
        test.assert_eq(endpoints.scheme_of(table, 1), 0);
        test.assert(destination.is_https(endpoints.host_of(table, blob, 0)));
        // https takes a name, not an address, open or not
        let secure_address = "1 https://8.8.8.8 443 whsec_MDEyMzQ1Njc4OWFiY2RlZg==\n";
        test.assert_eq(endpoints.parse(secure_address, table, blob, false), 0 - 1);
        test.assert_eq(endpoints.parse(secure_address, table, blob, true), 0 - 1);
    }
    return 0;
}

fn test_a_name_is_a_name_when_its_labels_and_its_last_label_say_so() -> [] int {
    test.assert(destination.name_ok("example.com"));
    test.assert(destination.name_ok("localhost"));
    test.assert(destination.name_ok("receiver"));
    test.assert(destination.name_ok("my_service.internal"));
    test.assert(destination.name_ok("a-b.c-d.example"));
    test.assert(destination.name_ok("Hooks.Example.COM"));
    test.assert(destination.name_ok("8.8.8.8.nip.io"));
    test.assert(destination.name_ok("1a.example"));
    test.assert(destination.name_ok("xn--bcher-kva.example"));
    // an address, or something a person would read as one, or something that is not a name
    test.assert(!destination.name_ok(""));
    test.assert(!destination.name_ok("127.1"));
    test.assert(!destination.name_ok("2130706433"));
    test.assert(!destination.name_ok("0x7f.0.0.1"));
    test.assert(!destination.name_ok("1.2.3"));
    test.assert(!destination.name_ok("1.2.3.4.5"));
    test.assert(!destination.name_ok("256.1.1.1"));
    test.assert(!destination.name_ok("example.com."));
    test.assert(!destination.name_ok(".example.com"));
    test.assert(!destination.name_ok("a..b"));
    test.assert(!destination.name_ok("-a.example"));
    test.assert(!destination.name_ok("a-.example"));
    test.assert(!destination.name_ok("a b.example"));
    test.assert(!destination.name_ok("a/b.example"));
    test.assert(!destination.name_ok("user@example.com"));
    test.assert(!destination.name_ok("example.com:80"));
    test.assert(!destination.name_ok("[::1]"));
    test.assert(!destination.name_ok("::1"));
    test.assert(!destination.name_ok("*.example.com"));
    test.assert(!destination.name_ok("exa\tmple.com"));
    return 0;
}

fn test_a_label_is_63_bytes_at_most_and_a_name_253() -> [] int {
    let l63 = "0123456789012345678901234567890123456789012345678901234567890ab";
    test.assert_eq(len(l63), 63);
    test.assert(destination.name_ok(l63));
    test.assert(!destination.name_ok("01234567890123456789012345678901234567890123456789012345678901ab"));
    region a {
        // three labels of 63 and one of 61, with their dots: 253 bytes
        let long = alloc_slice[a](253, byte_of('a'));
        var i = 63;
        while i < 253 {
            long[i] = byte_of('.');
            i = i + 64;
        }
        test.assert(destination.name_ok(long));
        // one more byte in the last label: 254
        let too = alloc_slice[a](254, byte_of('a'));
        i = 63;
        while i < 253 {
            too[i] = byte_of('.');
            i = i + 64;
        }
        test.assert(!destination.name_ok(too));
        // a dot in place of the last byte of the full name leaves a trailing dot, which is not a name here
        long[252] = byte_of('.');
        test.assert(!destination.name_ok(long));
    }
    return 0;
}

fn test_a_host_is_judged_in_the_form_it_is_stored() -> [] int {
    test.assert(destination.host_ok("example.com", false));
    test.assert(destination.host_ok("https://example.com", false));
    test.assert(destination.host_ok("8.8.8.8", false));
    test.assert(!destination.host_ok("127.0.0.1", false));
    test.assert(destination.host_ok("127.0.0.1", true));
    test.assert(destination.host_ok("localhost", false));
    // https takes a name only
    test.assert(!destination.host_ok("https://8.8.8.8", false));
    test.assert(!destination.host_ok("https://127.0.0.1", true));
    // another scheme is not a host, whatever open says
    test.assert(!destination.host_ok("http://example.com", false));
    test.assert(!destination.host_ok("ftp://example.com", true));
    test.assert(!destination.host_ok("https://", false));
    test.assert(!destination.host_ok("https:///x", true));
    test.assert(!destination.host_ok("https://127.1", true));
    test.assert(!destination.host_ok("127.1", true));
    test.assert(!destination.host_ok("::1", true));
    return 0;
}

fn test_localhost_is_the_loopback_in_any_case() -> [] int {
    test.assert(destination.is_localhost("localhost"));
    test.assert(destination.is_localhost("LocalHost"));
    test.assert(destination.is_localhost("app.localhost"));
    test.assert(destination.is_localhost("a.b.LOCALHOST"));
    test.assert(!destination.is_localhost("notlocalhost"));
    test.assert(!destination.is_localhost("localhost.example.com"));
    test.assert(!destination.is_localhost("xlocalhost"));
    test.assert(!destination.is_localhost("local"));
    test.assert(!destination.is_localhost(".localhost"));
    test.assert(!destination.is_localhost(""));
    return 0;
}

fn test_the_scheme_is_part_of_the_host() -> [] int {
    test.assert(destination.is_https("https://a.example"));
    test.assert(!destination.is_https("http://a.example"));
    test.assert(!destination.is_https("a.example"));
    test.assert(!destination.is_https("https:/a.example"));
    test.assert(!destination.is_https("HTTPS://a.example"));
    test.assert(!destination.is_https(""));
    test.assert_eq(len(destination.bare("https://a.example")), 9);
    test.assert_eq(len(destination.bare("a.example")), 9);
    return 0;
}
