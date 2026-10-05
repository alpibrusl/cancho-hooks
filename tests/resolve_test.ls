edition 5;

import std.test;
import resolve;

// Which name server the service asks when none is named (`src/resolve.ls`, `docs/design.md` section 40): the first IPv4 `nameserver` of resolv.conf.

fn test_the_first_ipv4_nameserver_is_the_one() -> [] int {
    test.assert_eq(resolve.nameserver("nameserver 10.0.0.2\n"), 167772162);
    test.assert_eq(resolve.nameserver("# generated\nsearch example.com\nnameserver 127.0.0.53\nnameserver 8.8.8.8\noptions edns0\n"), 2130706485);
    test.assert_eq(resolve.nameserver("nameserver\t192.168.1.1\r\n"), 3232235777);
    test.assert_eq(resolve.nameserver("  nameserver 1.1.1.1  \n"), 16843009);
    test.assert_eq(resolve.nameserver("nameserver 1.1.1.1 # the usual"), 16843009);
    return 0;
}

fn test_an_ipv6_server_is_passed_over_and_none_is_zero() -> [] int {
    test.assert_eq(resolve.nameserver("nameserver ::1\nnameserver fe80::1%eth0\nnameserver 9.9.9.9\n"), 151587081);
    test.assert_eq(resolve.nameserver("nameserver ::1\n"), 0);
    test.assert_eq(resolve.nameserver(""), 0);
    test.assert_eq(resolve.nameserver("search example.com\n"), 0);
    test.assert_eq(resolve.nameserver("nameserver\n"), 0);
    test.assert_eq(resolve.nameserver("nameserver \n"), 0);
    test.assert_eq(resolve.nameserver("nameservers 1.2.3.4\n"), 0);
    test.assert_eq(resolve.nameserver("nameserver 1.2.3\n"), 0);
    test.assert_eq(resolve.nameserver("nameserver localhost\n"), 0);
    test.assert_eq(resolve.nameserver("# nameserver 1.2.3.4\n"), 0);
    // 0.0.0.0 is no server
    test.assert_eq(resolve.nameserver("nameserver 0.0.0.0\nnameserver 4.4.4.4\n"), 67372036);
    return 0;
}
