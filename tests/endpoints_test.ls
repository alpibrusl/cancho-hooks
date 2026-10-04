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
        let n = endpoints.parse(text, table, blob, true);
        test.assert_eq(n, 2);
        test.assert_eq(endpoints.slot_of(table, 0), 3);
        test.assert_eq(endpoints.ident_of(table, 0), 3);
        test.assert_eq(endpoints.port_of(table, 0), 9001);
        test.assert_eq(len(endpoints.host_of(table, blob, 0)), 9);
        test.assert_eq(int_of(endpoints.host_of(table, blob, 0)[0]), int_of(byte_of('1')));
        test.assert_eq(len(endpoints.key_of(table, blob, 0)), 16);
        test.assert_eq(int_of(endpoints.key_of(table, blob, 0)[0]), int_of(byte_of('0')));
        test.assert_eq(int_of(endpoints.key_of(table, blob, 0)[15]), int_of(byte_of('f')));
        test.assert_eq(endpoints.slot_of(table, 1), 7);
        test.assert_eq(endpoints.ident_of(table, 1), 7);
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
        return endpoints.parse(text, table, blob, true);
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
    // Id with seven digits, not a number, repeated; port 0 and 65536; secret not base64. An id of 16 or 999999 is an id.
    test.assert_eq(refused("1000000 h 80 c2VjcmV0ISE=\n"), 0 - 1);
    test.assert_eq(refused("16 h 80 c2VjcmV0ISE=\n999999 h 80 c2VjcmV0ISE=\n"), 2);
    test.assert_eq(refused("a h 80 c2VjcmV0ISE=\n"), 0 - 1);
    test.assert_eq(refused("2 h 80 c2VjcmV0ISE=\n\n2 g 81 c2VjcmV0ISE=\n"), 0 - 3);
    test.assert_eq(refused("1 h 0 c2VjcmV0ISE=\n"), 0 - 1);
    test.assert_eq(refused("1 h 65536 c2VjcmV0ISE=\n"), 0 - 1);
    test.assert_eq(refused("1 h 80 not*base64\n"), 0 - 1);
    // No final newline is fine.
    test.assert_eq(refused("1 h 80 c2VjcmV0ISE="), 1);
    return 0;
}

fn test_the_id_is_kept_beside_the_slot_and_the_slot_can_change() -> [] int {
    region a {
        let table = alloc_slice[a](endpoints.table_size(), 0);
        let blob = alloc_slice[a](512, byte_of(0));
        test.assert_eq(endpoints.parse("20 h 80 c2VjcmV0ISE=\n999999 g 81 c2VjcmV0ISE=\n", table, blob, true), 2);
        // `parse` cannot know the slot: it writes the id there, for the caller to replace.
        test.assert_eq(endpoints.slot_of(table, 0), 20);
        test.assert_eq(endpoints.set_slot(table, 0, 5), 0);
        test.assert_eq(endpoints.slot_of(table, 0), 5);
        test.assert_eq(endpoints.ident_of(table, 0), 20);
        test.assert_eq(endpoints.ident_of(table, 1), 999999);
        test.assert_eq(endpoints.slot_of(table, 1), 999999);
    }
    return 0;
}

fn test_at_most_max_endpoints_are_accepted() -> [] int {
    region a {
        let table = alloc_slice[a](endpoints.table_size(), 0);
        let blob = alloc_slice[a](endpoints.text_limit(), byte_of(0));
        let text = alloc_slice[a](2048, byte_of(0));
        // 62 lines "<n> h 80 c2VjcmV0ISE=": the 62nd is the last that is accepted, a 63rd is refused with its own number.
        var at = 0;
        var n = 0;
        while n < 63 {
            text[at] = byte_of('0' + n / 10);
            text[at + 1] = byte_of('0' + n % 10);
            let tail = " h 80 c2VjcmV0ISE=\n";
            var k = 0;
            while k < len(tail) {
                text[at + 2 + k] = tail[k];
                k = k + 1;
            }
            at = at + 2 + len(tail);
            n = n + 1;
        }
        test.assert_eq(endpoints.parse(text[0..at - 21], table, blob, true), 62);
        test.assert_eq(endpoints.parse(text[0..at], table, blob, true), 0 - 63);
    }
    return 0;
}

// `replace` and `compact` (`docs/design.md` section 25.4): a change of host and key goes after the used bytes, the old ones are left until the blob is full, and then
// everything is moved to the front without changing what any endpoint says.

fn test_replace_changes_one_endpoint_and_leaves_the_others() -> [] int {
    region a {
        let table = alloc_slice[a](endpoints.table_size(), 0);
        let blob = alloc_slice[a](512, byte_of(0));
        let scratch = alloc_slice[a](512, byte_of(0));
        let text = "1 8.8.8.8 9001 whsec_MDEyMzQ1Njc4OWFiY2RlZg==\n2 1.1.1.1 9002 c2VjcmV0ISE=\n";
        test.assert_eq(endpoints.parse(text, table, blob, true), 2);
        let key = alloc_slice[a](3, byte_of('k'));
        test.assert_eq(endpoints.replace(table, blob, 2, 0, 9100, "9.9.9.9", key, scratch), 0);
        test.assert_eq(endpoints.port_of(table, 0), 9100);
        test.assert_eq(len(endpoints.host_of(table, blob, 0)), 7);
        test.assert_eq(int_of(endpoints.host_of(table, blob, 0)[0]), int_of(byte_of('9')));
        test.assert_eq(len(endpoints.key_of(table, blob, 0)), 3);
        test.assert_eq(int_of(endpoints.key_of(table, blob, 0)[2]), int_of(byte_of('k')));
        test.assert_eq(endpoints.port_of(table, 1), 9002);
        test.assert_eq(len(endpoints.host_of(table, blob, 1)), 7);
        test.assert_eq(int_of(endpoints.host_of(table, blob, 1)[0]), int_of(byte_of('1')));
        test.assert_eq(len(endpoints.key_of(table, blob, 1)), 8);
        test.assert_eq(int_of(endpoints.key_of(table, blob, 1)[0]), int_of(byte_of('s')));
        test.assert_eq(endpoints.slot_of(table, 0), 1);
        test.assert_eq(endpoints.ident_of(table, 1), 2);
    }
    return 0;
}

fn test_compact_moves_everything_to_the_front_unchanged() -> [] int {
    region a {
        let table = alloc_slice[a](endpoints.table_size(), 0);
        let blob = alloc_slice[a](512, byte_of(0));
        let scratch = alloc_slice[a](512, byte_of(0));
        let text = "1 8.8.8.8 9001 whsec_MDEyMzQ1Njc4OWFiY2RlZg==\n2 1.1.1.1 9002 c2VjcmV0ISE=\n";
        test.assert_eq(endpoints.parse(text, table, blob, true), 2);
        let used = endpoints.blob_used(table, 2);
        let key = alloc_slice[a](3, byte_of('k'));
        // three replacements leave three old entries behind
        test.assert_eq(endpoints.replace(table, blob, 2, 0, 1, "5.5.5.5", key, scratch), 0);
        test.assert_eq(endpoints.replace(table, blob, 2, 0, 2, "6.6.6.6", key, scratch), 0);
        test.assert_eq(endpoints.replace(table, blob, 2, 0, 3, "7.7.7.7", key, scratch), 0);
        test.assert(endpoints.blob_used(table, 2) > used + 20);
        let packed = endpoints.compact(table, blob, 2, scratch);
        test.assert_eq(packed, 7 + 3 + 7 + 8);
        test.assert_eq(endpoints.blob_used(table, 2), packed);
        test.assert_eq(int_of(endpoints.host_of(table, blob, 0)[0]), int_of(byte_of('7')));
        test.assert_eq(endpoints.port_of(table, 0), 3);
        test.assert_eq(int_of(endpoints.key_of(table, blob, 0)[1]), int_of(byte_of('k')));
        test.assert_eq(int_of(endpoints.host_of(table, blob, 1)[6]), int_of(byte_of('1')));
        test.assert_eq(int_of(endpoints.key_of(table, blob, 1)[7]), int_of(byte_of('!')));
    }
    return 0;
}

fn test_replace_compacts_when_the_blob_is_full_and_refuses_when_it_cannot() -> [] int {
    region a {
        let table = alloc_slice[a](endpoints.table_size(), 0);
        let blob = alloc_slice[a](40, byte_of(0));
        let scratch = alloc_slice[a](40, byte_of(0));
        // 7 + 8 (host and key of 1) and 7 + 8 (of 2) = 30 of 40 bytes
        let text = "1 8.8.8.8 9001 c2VjcmV0ISE=\n2 1.1.1.1 9002 c2VjcmV0ISE=\n";
        test.assert_eq(endpoints.parse(text, table, blob, true), 2);
        let key = alloc_slice[a](8, byte_of('k'));
        // 7 + 8 more does not fit after 30, but does once the old 15 bytes of endpoint 1 (the one being changed) are taken out
        test.assert_eq(endpoints.replace(table, blob, 2, 0, 5, "9.9.9.9", key, scratch), 0);
        test.assert_eq(int_of(endpoints.host_of(table, blob, 0)[0]), int_of(byte_of('9')));
        test.assert_eq(int_of(endpoints.host_of(table, blob, 1)[0]), int_of(byte_of('1')));
        test.assert_eq(endpoints.blob_used(table, 2), 30);
        // a host that cannot fit however the blob is packed is refused and nothing changes
        test.assert_eq(endpoints.replace(table, blob, 2, 0, 6, "123456789012345678901234567890", key, scratch), 0 - 1);
        test.assert_eq(endpoints.port_of(table, 0), 5);
        test.assert_eq(int_of(endpoints.host_of(table, blob, 0)[0]), int_of(byte_of('9')));
    }
    return 0;
}
