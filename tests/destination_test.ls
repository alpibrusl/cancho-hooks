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

fn test_a_name_is_never_allowed() -> [] int {
    test.assert(!is_allowed("example.com"));
    test.assert(!is_allowed("localhost"));
    test.assert(!is_allowed("8.8.8.8.nip.io"));
    test.assert(!is_allowed("0x08.8.8.8"));
    return 0;
}

fn test_the_endpoints_file_applies_the_rule() -> [] int {
    region a {
        let table = alloc_slice[a](endpoints.table_size(), 0);
        let blob = alloc_slice[a](512, byte_of(0));
        // whsec_ + base64("0123456789abcdef")
        let good = "1 8.8.8.8 9001 whsec_MDEyMzQ1Njc4OWFiY2RlZg==\n2 1.1.1.1 9002 whsec_MDEyMzQ1Njc4OWFiY2RlZg==\n";
        test.assert_eq(endpoints.parse(good, table, blob, false), 2);
        let loopback = "1 8.8.8.8 9001 whsec_MDEyMzQ1Njc4OWFiY2RlZg==\n2 127.0.0.1 9002 whsec_MDEyMzQ1Njc4OWFiY2RlZg==\n";
        test.assert_eq(endpoints.parse(loopback, table, blob, false), 0 - 2);
        test.assert_eq(endpoints.parse(loopback, table, blob, true), 2);
        let named = "# a name\n1 example.com 9001 whsec_MDEyMzQ1Njc4OWFiY2RlZg==\n";
        test.assert_eq(endpoints.parse(named, table, blob, false), 0 - 2);
        test.assert_eq(endpoints.parse(named, table, blob, true), 1);
        let short = "1 127.1 9001 whsec_MDEyMzQ1Njc4OWFiY2RlZg==\n";
        test.assert_eq(endpoints.parse(short, table, blob, false), 0 - 1);
    }
    return 0;
}
