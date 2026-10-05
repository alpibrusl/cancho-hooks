edition 5;

import std.test;
import manage;

// The parts of `POST /endpoints` and `PATCH` that are only text (`src/manage.ls`, `docs/design.md` section 40): a `url`, and the host as it is stored.

fn url_host(url: &static [byte]) -> [] int {
    region a {
        let out = alloc_slice[a](253, byte_of(0));
        let parts = manage.split_url(url, out);
        return parts.0;
    }
}

fn test_a_url_is_a_scheme_a_host_and_maybe_a_port() -> [] int {
    region a {
        let out = alloc_slice[a](253, byte_of(0));
        let https = manage.split_url("https://hooks.example.com", out);
        test.assert_eq(https.0, 0);
        test.assert_eq(https.1, 17);
        test.assert_eq(https.2, 443);
        test.assert_eq(https.3, 1);
        test.assert_eq(int_of(out[0]), 'h');
        test.assert_eq(int_of(out[16]), 'm');
        let http = manage.split_url("http://hooks.example.com", out);
        test.assert_eq(http.0, 0);
        test.assert_eq(http.2, 80);
        test.assert_eq(http.3, 0);
        let port = manage.split_url("https://hooks.example.com:8443/", out);
        test.assert_eq(port.0, 0);
        test.assert_eq(port.1, 17);
        test.assert_eq(port.2, 8443);
        test.assert_eq(port.3, 1);
        let literal = manage.split_url("http://203.0.113.5:9", out);
        test.assert_eq(literal.0, 0);
        test.assert_eq(literal.1, 11);
        test.assert_eq(literal.2, 9);
    }
    return 0;
}

fn test_what_is_not_a_url_is_refused() -> [] int {
    test.assert_eq(url_host(""), 9);
    test.assert_eq(url_host("hooks.example.com"), 9);
    test.assert_eq(url_host("ftp://hooks.example.com"), 9);
    test.assert_eq(url_host("https:/hooks.example.com"), 9);
    test.assert_eq(url_host("https://"), 9);
    test.assert_eq(url_host("http://"), 9);
    test.assert_eq(url_host("https://:443"), 9);
    test.assert_eq(url_host("https://hooks.example.com:"), 9);
    test.assert_eq(url_host("https://hooks.example.com:0"), 9);
    test.assert_eq(url_host("https://hooks.example.com:65536"), 9);
    test.assert_eq(url_host("https://hooks.example.com:80a"), 9);
    test.assert_eq(url_host("https://hooks.example.com:123456"), 9);
    // a path, a query or a user are not part of it: they leave a "host" that is not a name, which `stored_host` refuses
    test.assert_eq(url_host("https://hooks.example.com/hook"), 0);
    return 0;
}

fn test_the_stored_host_carries_the_scheme_and_is_judged_whole() -> [] int {
    region a {
        let out = alloc_slice[a](264, byte_of(0));
        test.assert_eq(manage.stored_host("hooks.example.com", true, false, out), 25);
        test.assert_eq(int_of(out[0]), 'h');
        test.assert_eq(int_of(out[7]), '/');
        test.assert_eq(int_of(out[8]), 'h');
        test.assert_eq(manage.stored_host("hooks.example.com", false, false, out), 17);
        test.assert_eq(int_of(out[0]), 'h');
        test.assert_eq(int_of(out[1]), 'o');
        test.assert_eq(manage.stored_host("8.8.8.8", false, false, out), 7);
        // an address is not a host for https, open or not
        test.assert_eq(manage.stored_host("8.8.8.8", true, false, out), 0 - 1);
        test.assert_eq(manage.stored_host("127.0.0.1", true, true, out), 0 - 1);
        // a private address only when private hosts are allowed
        test.assert_eq(manage.stored_host("127.0.0.1", false, false, out), 0 - 1);
        test.assert_eq(manage.stored_host("127.0.0.1", false, true, out), 9);
        // not a host at all
        test.assert_eq(manage.stored_host("user@example.com", false, true, out), 0 - 1);
        test.assert_eq(manage.stored_host("hooks.example.com/hook", true, true, out), 0 - 1);
        test.assert_eq(manage.stored_host("127.1", false, true, out), 0 - 1);
        test.assert_eq(manage.stored_host("https://x.example", false, true, out), 0 - 1);
        // 248 bytes of name and the scheme fill the 256 there are
        let long = alloc_slice[a](249, byte_of('a'));
        long[63] = byte_of('.');
        long[127] = byte_of('.');
        long[191] = byte_of('.');
        test.assert_eq(manage.stored_host(long[0..248], true, false, out), 256);
        test.assert_eq(manage.stored_host(long[0..249], true, false, out), 0 - 1);
        test.assert_eq(manage.stored_host(long[0..249], false, false, out), 249);
    }
    return 0;
}
