edition 5;

import std.test;
import sign;
import state;
import endpoints;

// The endpoints file (`src/endpoints.ls`): what a good file gives, and that every kind of bad line is refused with its line.

fn test_a_file_gives_its_endpoints() -> [] int {
    region a {
        let table = alloc_slice[a](endpoints.table_size(), 0);
        let blob = alloc_slice[a](512, byte_of(0));
        // whsec_ + base64("0123456789abcdef") and base64("secret!!")
        let text = "# endpoints\n\n3 127.0.0.1 9001 whsec_MDEyMzQ1Njc4OWFiY2RlZg==\n  7\tlocalhost  9002 c2VjcmV0ISE=  \r\n";
        let n = endpoints.parse(text, table, blob);
        test.assert_eq(n, 2);
        test.assert_eq(endpoints.id_of(table, 0), 3);
        test.assert_eq(endpoints.port_of(table, 0), 9001);
        test.assert_eq(len(endpoints.host_of(table, blob, 0)), 9);
        test.assert_eq(int_of(endpoints.host_of(table, blob, 0)[0]), int_of(byte_of('1')));
        test.assert_eq(len(endpoints.key_of(table, blob, 0)), 16);
        test.assert_eq(int_of(endpoints.key_of(table, blob, 0)[0]), int_of(byte_of('0')));
        test.assert_eq(int_of(endpoints.key_of(table, blob, 0)[15]), int_of(byte_of('f')));
        test.assert_eq(endpoints.id_of(table, 1), 7);
        test.assert_eq(endpoints.port_of(table, 1), 9002);
        test.assert_eq(len(endpoints.host_of(table, blob, 1)), 9);
        test.assert_eq(len(endpoints.key_of(table, blob, 1)), 8);
        test.assert_eq(int_of(endpoints.key_of(table, blob, 1)[0]), int_of(byte_of('s')));
    }
    return 0;
}

fn refused[&t](text: &t [byte]) -> [] int {
    region a {
        let table = alloc_slice[a](endpoints.table_size(), 0);
        let blob = alloc_slice[a](512, byte_of(0));
        return endpoints.parse(text, table, blob);
    }
}

fn test_a_bad_line_is_refused_with_its_number() -> [] int {
    test.assert_eq(refused(""), 0);
    test.assert_eq(refused("# only a comment\n"), 0);
    // Three fields; five fields.
    test.assert_eq(refused("1 h 80\n"), 0 - 1);
    test.assert_eq(refused("0 h 80 c2VjcmV0ISE= extra\n"), 0 - 1);
    // The good first line does not hide the bad second one.
    test.assert_eq(refused("0 h 80 c2VjcmV0ISE=\n1 h x c2VjcmV0ISE=\n"), 0 - 2);
    // Id out of range, not a number, repeated; port 0 and 65536; secret not base64.
    test.assert_eq(refused("16 h 80 c2VjcmV0ISE=\n"), 0 - 1);
    test.assert_eq(refused("a h 80 c2VjcmV0ISE=\n"), 0 - 1);
    test.assert_eq(refused("2 h 80 c2VjcmV0ISE=\n\n2 g 81 c2VjcmV0ISE=\n"), 0 - 3);
    test.assert_eq(refused("1 h 0 c2VjcmV0ISE=\n"), 0 - 1);
    test.assert_eq(refused("1 h 65536 c2VjcmV0ISE=\n"), 0 - 1);
    test.assert_eq(refused("1 h 80 not*base64\n"), 0 - 1);
    // No final newline is fine.
    test.assert_eq(refused("1 h 80 c2VjcmV0ISE="), 1);
    return 0;
}
