edition 5;

import std.test;
import std.bytes;
import dbup;

// What the service does with a database that is not there when it starts (`src/dbup.ls`, `docs/design.md` section 37): which failures end the
// start at once, which are waited for, and when the wait is over.

fn test_a_login_the_server_refuses_for_good_ends_it_at_once() -> [] int {
    test.assert_eq(dbup.verdict(4, "28P01", 0, 30000), 2);
    test.assert_eq(dbup.verdict(4, "28000", 0, 30000), 2);
    test.assert_eq(dbup.verdict(4, "3D000", 0, 30000), 2);
    test.assert_eq(dbup.verdict(4, "28P01", 0, 0), 2);
    return 0;
}

fn test_a_refusal_for_now_is_waited_out() -> [] int {
    test.assert_eq(dbup.verdict(4, "57P03", 0, 30000), 0);
    test.assert_eq(dbup.verdict(4, "57P03", 29999, 30000), 0);
    test.assert_eq(dbup.verdict(4, "53300", 5, 30000), 0);
    test.assert_eq(dbup.verdict(4, "", 5, 30000), 0);
    test.assert_eq(dbup.verdict(4, "28", 5, 30000), 0);
    test.assert_eq(dbup.verdict(4, "3D00", 5, 30000), 0);
    test.assert_eq(dbup.verdict(4, "57P03", 30000, 30000), 2);
    return 0;
}

fn test_a_login_this_client_cannot_do_or_a_statement_refused_ends_it_at_once() -> [] int {
    test.assert_eq(dbup.verdict(5, "", 0, 30000), 2);
    test.assert_eq(dbup.verdict(7, "", 0, 30000), 2);
    test.assert_eq(dbup.verdict(9, "42P01", 0, 30000), 3);
    test.assert_eq(dbup.verdict(9, "", 0, 0), 3);
    return 0;
}

fn test_a_connection_that_cannot_be_made_is_waited_for_until_the_limit() -> [] int {
    test.assert_eq(dbup.verdict(20, "", 0, 30000), 0);
    test.assert_eq(dbup.verdict(20, "", 29999, 30000), 0);
    test.assert_eq(dbup.verdict(20, "", 30000, 30000), 1);
    test.assert_eq(dbup.verdict(20, "", 90000, 30000), 1);
    test.assert_eq(dbup.verdict(1, "", 40000, 30000), 1);
    test.assert_eq(dbup.verdict(3, "", 40000, 30000), 1);
    test.assert_eq(dbup.verdict(6, "", 40000, 30000), 1);
    test.assert_eq(dbup.verdict(10, "", 40000, 30000), 1);
    return 0;
}

fn test_a_server_that_does_not_answer_is_the_other_reason() -> [] int {
    test.assert_eq(dbup.verdict(21, "", 29999, 30000), 0);
    test.assert_eq(dbup.verdict(21, "", 30000, 30000), 4);
    test.assert_eq(dbup.verdict(0, "", 30000, 30000), 4);
    test.assert_eq(dbup.verdict(0, "", 29999, 30000), 0);
    return 0;
}

fn test_zero_is_wait_for_ever() -> [] int {
    test.assert_eq(dbup.verdict(20, "", 100000000, 0), 0);
    test.assert_eq(dbup.verdict(21, "", 100000000, 0), 0);
    test.assert_eq(dbup.verdict(0, "", 100000000, 0), 0);
    test.assert_eq(dbup.verdict(4, "57P03", 100000000, 0), 0);
    return 0;
}

fn test_what_the_table_read_found_is_a_reason_with_words() -> [] int {
    test.assert_eq(dbup.reason_of_text(0 - 3), 3);
    test.assert_eq(dbup.reason_of_text(0 - 4), 5);
    test.assert_eq(dbup.reason_of_text(0 - 5), 6);
    test.assert(bytes.equal(dbup.message(1), "cannot connect"));
    test.assert(bytes.equal(dbup.message(2), "cannot log in"));
    test.assert(bytes.starts_with(dbup.message(3), "the query failed"));
    test.assert(bytes.starts_with(dbup.message(4), "the database did not answer"));
    test.assert(bytes.starts_with(dbup.message(5), "a row has an empty field"));
    test.assert(bytes.starts_with(dbup.message(6), "the table is too large"));
    return 0;
}
