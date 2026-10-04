edition 5;

import std.test;
import sign;
import state;
import endpoints;
import epx;

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

// `remove` (`docs/design.md` section 25.5): the entries after the removed one move down, every other endpoint says what it said, the removed one's
// bytes are zeroed, and what a later `append` needs is there.

fn test_remove_shifts_the_later_entries_and_keeps_every_other_one_unchanged() -> [] int {
    region a {
        let table = alloc_slice[a](endpoints.table_size(), 0);
        let blob = alloc_slice[a](512, byte_of(0));
        let text = "1 8.8.8.8 9001 whsec_MDEyMzQ1Njc4OWFiY2RlZg==\n2 1.1.1.1 9002 c2VjcmV0ISE=\n3 2.2.2.2 9003 c2VjcmV0ISE=\n4 3.3.3.3 9004 whsec_MDEyMzQ1Njc4OWFiY2RlZg==\n";
        test.assert_eq(endpoints.parse(text, table, blob, true), 4);
        endpoints.set_slot(table, 0, 10);
        endpoints.set_slot(table, 1, 11);
        endpoints.set_slot(table, 2, 12);
        endpoints.set_slot(table, 3, 13);
        // the middle one
        test.assert_eq(endpoints.remove(table, blob, 4, 1), 3);
        test.assert_eq(endpoints.ident_of(table, 0), 1);
        test.assert_eq(endpoints.ident_of(table, 1), 3);
        test.assert_eq(endpoints.ident_of(table, 2), 4);
        test.assert_eq(endpoints.slot_of(table, 0), 10);
        test.assert_eq(endpoints.slot_of(table, 1), 12);
        test.assert_eq(endpoints.slot_of(table, 2), 13);
        test.assert_eq(endpoints.port_of(table, 0), 9001);
        test.assert_eq(endpoints.port_of(table, 1), 9003);
        test.assert_eq(endpoints.port_of(table, 2), 9004);
        test.assert_eq(int_of(endpoints.host_of(table, blob, 0)[0]), int_of(byte_of('8')));
        test.assert_eq(int_of(endpoints.host_of(table, blob, 1)[0]), int_of(byte_of('2')));
        test.assert_eq(int_of(endpoints.host_of(table, blob, 2)[0]), int_of(byte_of('3')));
        test.assert_eq(len(endpoints.key_of(table, blob, 0)), 16);
        test.assert_eq(len(endpoints.key_of(table, blob, 1)), 8);
        test.assert_eq(len(endpoints.key_of(table, blob, 2)), 16);
        test.assert_eq(int_of(endpoints.key_of(table, blob, 1)[0]), int_of(byte_of('s')));
        test.assert_eq(int_of(endpoints.key_of(table, blob, 2)[15]), int_of(byte_of('f')));
        // the vacated row is cleared
        test.assert_eq(endpoints.ident_of(table, 3), 0);
        test.assert_eq(endpoints.port_of(table, 3), 0);
        // the first one, then the last, then the only one left
        test.assert_eq(endpoints.remove(table, blob, 3, 0), 2);
        test.assert_eq(endpoints.ident_of(table, 0), 3);
        test.assert_eq(endpoints.ident_of(table, 1), 4);
        test.assert_eq(endpoints.remove(table, blob, 2, 1), 1);
        test.assert_eq(endpoints.ident_of(table, 0), 3);
        test.assert_eq(int_of(endpoints.host_of(table, blob, 0)[0]), int_of(byte_of('2')));
        test.assert_eq(endpoints.remove(table, blob, 1, 0), 0);
        test.assert_eq(endpoints.blob_used(table, 0), 0);
    }
    return 0;
}

fn test_remove_zeroes_the_removed_endpoints_host_and_key() -> [] int {
    region a {
        let table = alloc_slice[a](endpoints.table_size(), 0);
        let blob = alloc_slice[a](512, byte_of(0));
        test.assert_eq(endpoints.parse("1 8.8.8.8 9001 whsec_MDEyMzQ1Njc4OWFiY2RlZg==\n2 1.1.1.1 9002 c2VjcmV0ISE=\n", table, blob, true), 2);
        let host_at = table[2];
        let key_at = table[4];
        test.assert_eq(int_of(blob[host_at]), int_of(byte_of('8')));
        test.assert_eq(int_of(blob[key_at]), int_of(byte_of('0')));
        test.assert_eq(endpoints.remove(table, blob, 2, 0), 1);
        var k = 0;
        while k < 7 {
            test.assert_eq(int_of(blob[host_at + k]), 0);
            k = k + 1;
        }
        k = 0;
        while k < 16 {
            test.assert_eq(int_of(blob[key_at + k]), 0);
            k = k + 1;
        }
        // and the one that stayed is untouched
        test.assert_eq(int_of(endpoints.host_of(table, blob, 0)[0]), int_of(byte_of('1')));
        test.assert_eq(int_of(endpoints.key_of(table, blob, 0)[0]), int_of(byte_of('s')));
    }
    return 0;
}

fn test_remove_refuses_an_index_that_is_not_there_and_changes_nothing() -> [] int {
    region a {
        let table = alloc_slice[a](endpoints.table_size(), 0);
        let blob = alloc_slice[a](512, byte_of(0));
        test.assert_eq(endpoints.parse("1 8.8.8.8 9001 c2VjcmV0ISE=\n2 1.1.1.1 9002 c2VjcmV0ISE=\n", table, blob, true), 2);
        test.assert_eq(endpoints.remove(table, blob, 2, 2), 0 - 1);
        test.assert_eq(endpoints.remove(table, blob, 2, 0 - 1), 0 - 1);
        test.assert_eq(endpoints.remove(table, blob, 0, 0), 0 - 1);
        test.assert_eq(endpoints.ident_of(table, 0), 1);
        test.assert_eq(endpoints.ident_of(table, 1), 2);
        test.assert_eq(int_of(endpoints.host_of(table, blob, 0)[0]), int_of(byte_of('8')));
        test.assert_eq(int_of(endpoints.key_of(table, blob, 1)[0]), int_of(byte_of('s')));
    }
    return 0;
}

fn test_after_remove_the_freed_room_is_used_by_append_and_the_survivors_are_intact() -> [] int {
    region a {
        let table = alloc_slice[a](endpoints.table_size(), 0);
        let blob = alloc_slice[a](40, byte_of(0));
        let scratch = alloc_slice[a](40, byte_of(0));
        // two entries of 7 + 8 bytes: 30 of 40
        test.assert_eq(endpoints.parse("1 8.8.8.8 9001 c2VjcmV0ISE=\n2 1.1.1.1 9002 c2VjcmV0ISE=\n", table, blob, true), 2);
        let key = alloc_slice[a](8, byte_of('k'));
        // the last one leaves: its 15 bytes are the end of the used part again, so a 15 byte entry fits where it was
        test.assert_eq(endpoints.remove(table, blob, 2, 1), 1);
        test.assert_eq(endpoints.blob_used(table, 1), 15);
        test.assert_eq(endpoints.append(table, blob, 1, 5, 9, 9009, "7.7.7.7", key), 2);
        test.assert_eq(endpoints.ident_of(table, 1), 9);
        test.assert_eq(endpoints.slot_of(table, 1), 5);
        // the first one leaves: its 15 bytes are a hole at the front; a second append of 15 does not fit after the used 30, `replace` compacts
        test.assert_eq(endpoints.remove(table, blob, 2, 0), 1);
        test.assert_eq(endpoints.ident_of(table, 0), 9);
        test.assert_eq(endpoints.append(table, blob, 1, 6, 10, 9010, "6.6.6.6", key), 0 - 1);
        test.assert_eq(endpoints.compact(table, blob, 1, scratch), 15);
        test.assert_eq(endpoints.append(table, blob, 1, 6, 10, 9010, "6.6.6.6", key), 2);
        test.assert_eq(int_of(endpoints.host_of(table, blob, 0)[0]), int_of(byte_of('7')));
        test.assert_eq(int_of(endpoints.host_of(table, blob, 1)[0]), int_of(byte_of('6')));
        test.assert_eq(int_of(endpoints.key_of(table, blob, 0)[7]), int_of(byte_of('k')));
    }
    return 0;
}

// The optional words after the secret (`docs/design.md` section 35): a subscription, custom headers, a previous secret.

fn test_the_optional_words_are_kept_beside_the_endpoint() -> [] int {
    region a {
        let table = alloc_slice[a](endpoints.table_size(), 0);
        let blob = alloc_slice[a](512, byte_of(0));
        let xt = alloc_slice[a](2 * epx.stride(), 0);
        // the previous secret is base64("secret!!"); words in any order
        let text = "1 8.8.8.8 9001 c2VjcmV0ISE= old=c2VjcmV0ISE=@1700000000000 headers=Authorization:Bearer%20abc,X-Key:k types=user.*,ping\n2 1.1.1.1 9002 c2VjcmV0ISE=\n";
        test.assert_eq(endpoints.parse_x(text, table, blob, true, xt), 2);
        test.assert_eq(epx.types_len(xt, 0), 11);
        test.assert(epx.accepts(xt, 0, "user.created"));
        test.assert(epx.accepts(xt, 0, "ping"));
        test.assert(!epx.accepts(xt, 0, "order.created"));
        // "Authorization: Bearer abc\r\nX-Key: k\r\n"
        test.assert_eq(epx.wire_len(xt, 0), 27 + 10);
        test.assert_eq(epx.wire_byte(xt, 0, 15), int_of(byte_of('B')));
        test.assert_eq(epx.wire_byte(xt, 0, 21), int_of(byte_of(' ')));
        test.assert_eq(epx.old_len(xt, 0), 8);
        test.assert_eq(epx.old_until(xt, 0), 1700000000000);
        test.assert_eq(epx.old_byte(xt, 0, 0), int_of(byte_of('s')));
        // the key and the host of the line are what they were without the words
        test.assert_eq(len(endpoints.key_of(table, blob, 0)), 8);
        test.assert_eq(len(endpoints.host_of(table, blob, 0)), 7);
        test.assert_eq(endpoints.port_of(table, 0), 9001);
        // the line without words has none
        test.assert_eq(epx.types_len(xt, 1), 0);
        test.assert_eq(epx.wire_len(xt, 1), 0);
        test.assert_eq(epx.old_len(xt, 1), 0);
        test.assert(epx.accepts(xt, 1, "anything"));
        test.assert_eq(endpoints.port_of(table, 1), 9002);
        // `parse`, with nowhere to keep them, judges the same text and keeps nothing
        test.assert_eq(endpoints.parse(text, table, blob, true), 2);
    }
    return 0;
}

fn test_a_bad_optional_word_refuses_the_line() -> [] int {
    // the good line first, so the number is the bad one's
    test.assert_eq(refused("1 h 80 c2VjcmV0ISE=\n2 h 80 c2VjcmV0ISE= types=a,,b\n"), 0 - 2);
    test.assert_eq(refused("1 h 80 c2VjcmV0ISE=\n2 h 80 c2VjcmV0ISE= types=a*\n"), 0 - 2);
    test.assert_eq(refused("1 h 80 c2VjcmV0ISE=\n2 h 80 c2VjcmV0ISE= headers=Host:x\n"), 0 - 2);
    test.assert_eq(refused("1 h 80 c2VjcmV0ISE=\n2 h 80 c2VjcmV0ISE= headers=A:x%0d%0aB:y\n"), 0 - 2);
    test.assert_eq(refused("1 h 80 c2VjcmV0ISE=\n2 h 80 c2VjcmV0ISE= headers=A\n"), 0 - 2);
    // a word that is none of them, or one of them twice
    test.assert_eq(refused("1 h 80 c2VjcmV0ISE=\n2 h 80 c2VjcmV0ISE= tag=x\n"), 0 - 2);
    test.assert_eq(refused("1 h 80 c2VjcmV0ISE= types=a types=b\n"), 0 - 1);
    test.assert_eq(refused("1 h 80 c2VjcmV0ISE= headers=A:b headers=C:d\n"), 0 - 1);
    test.assert_eq(refused("1 h 80 c2VjcmV0ISE= types\n"), 0 - 1);
    // the previous secret: needs the secret, an '@' and a time, and the secret must be base64
    test.assert_eq(refused("1 h 80 c2VjcmV0ISE= old=c2VjcmV0ISE=\n"), 0 - 1);
    test.assert_eq(refused("1 h 80 c2VjcmV0ISE= old=c2VjcmV0ISE=@\n"), 0 - 1);
    test.assert_eq(refused("1 h 80 c2VjcmV0ISE= old=c2VjcmV0ISE=@12x\n"), 0 - 1);
    test.assert_eq(refused("1 h 80 c2VjcmV0ISE= old=c2VjcmV0ISE=@0\n"), 0 - 1);
    test.assert_eq(refused("1 h 80 c2VjcmV0ISE= old=@100\n"), 0 - 1);
    test.assert_eq(refused("1 h 80 c2VjcmV0ISE= old=not*base64@100\n"), 0 - 1);
    // the limits (`lim.ls`): in range, once each
    test.assert_eq(refused("1 h 80 c2VjcmV0ISE=\n2 h 80 c2VjcmV0ISE= concurrency=0\n"), 0 - 2);
    test.assert_eq(refused("1 h 80 c2VjcmV0ISE=\n2 h 80 c2VjcmV0ISE= concurrency=9\n"), 0 - 2);
    test.assert_eq(refused("1 h 80 c2VjcmV0ISE=\n2 h 80 c2VjcmV0ISE= concurrency=\n"), 0 - 2);
    test.assert_eq(refused("1 h 80 c2VjcmV0ISE=\n2 h 80 c2VjcmV0ISE= concurrency=x\n"), 0 - 2);
    test.assert_eq(refused("1 h 80 c2VjcmV0ISE=\n2 h 80 c2VjcmV0ISE= rate=0\n"), 0 - 2);
    test.assert_eq(refused("1 h 80 c2VjcmV0ISE=\n2 h 80 c2VjcmV0ISE= rate=100001\n"), 0 - 2);
    test.assert_eq(refused("1 h 80 c2VjcmV0ISE=\n2 h 80 c2VjcmV0ISE= rate=\n"), 0 - 2);
    test.assert_eq(refused("1 h 80 c2VjcmV0ISE= rate=2 rate=3\n"), 0 - 1);
    test.assert_eq(refused("1 h 80 c2VjcmV0ISE= concurrency=2 concurrency=2\n"), 0 - 1);
    test.assert_eq(refused("1 h 80 c2VjcmV0ISE= concurency=2\n"), 0 - 1);
    // and the good ones
    test.assert_eq(refused("1 h 80 c2VjcmV0ISE= concurrency=1 rate=1\n"), 1);
    test.assert_eq(refused("1 h 80 c2VjcmV0ISE= rate=100000 concurrency=8 types=a\n"), 1);
    test.assert_eq(refused("1 h 80 c2VjcmV0ISE= types=*\n"), 1);
    test.assert_eq(refused("1 h 80 c2VjcmV0ISE= old=c2VjcmV0ISE=@100\n"), 1);
    return 0;
}

fn test_the_limits_are_kept_beside_the_endpoint_in_either_order() -> [] int {
    region a {
        let table = alloc_slice[a](endpoints.table_size(), 0);
        let blob = alloc_slice[a](512, byte_of(0));
        let xt = alloc_slice[a](2 * epx.stride(), 0);
        let text = "1 8.8.8.8 9001 c2VjcmV0ISE= concurrency=2 rate=9\n2 1.1.1.1 9002 c2VjcmV0ISE= types=a rate=3 concurrency=5\n";
        test.assert_eq(endpoints.parse_x(text, table, blob, true, xt), 2);
        test.assert_eq(epx.conc(xt, 0), 2);
        test.assert_eq(epx.rate(xt, 0), 9);
        test.assert_eq(epx.conc(xt, 1), 5);
        test.assert_eq(epx.rate(xt, 1), 3);
        test.assert_eq(epx.types_len(xt, 1), 1);
    }
    return 0;
}

fn test_a_line_without_limits_follows_the_service() -> [] int {
    region a {
        let table = alloc_slice[a](endpoints.table_size(), 0);
        let blob = alloc_slice[a](512, byte_of(0));
        let xt = alloc_slice[a](2 * epx.stride(), 0);
        epx.set_conc(xt, 0, 4);
        epx.set_rate(xt, 0, 40);
        test.assert_eq(endpoints.parse_x("3 1.1.1.2 9003 c2VjcmV0ISE=\n", table, blob, true, xt), 1);
        test.assert_eq(epx.conc(xt, 0), 0);
        test.assert_eq(epx.rate(xt, 0), 0);
    }
    return 0;
}
