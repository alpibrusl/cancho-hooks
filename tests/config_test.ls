edition 5;

import std.test;
import config;
import std.bytes;

// The settings (`src/config.ls`): the defaults, each setting from a file, every way a file or a value is refused and that a
// refusal changes nothing, and the split of one command-line argument.

fn test_nothing_set_gives_the_defaults() -> [] int {
    region a {
        let cfg = alloc_slice[a](config.size(), 0);
        config.defaults(cfg);
        test.assert_eq(config.port_of(cfg), 0 - 1);
        test.assert_eq(config.deadline_ms(cfg), 0);
        test.assert_eq(config.window_ms(cfg), 86400000);
        test.assert_eq(config.dir_len(cfg), 0);
        test.assert_eq(config.sched_len(cfg), 0);
        test.assert_eq(config.pg_port(cfg), 5432);
        test.assert_eq(config.pg_host_len(cfg), 0);
        test.assert_eq(config.breaker_days(cfg), 5);
    }
    return 0;
}

fn test_a_file_sets_every_setting() -> [] int {
    region a {
        let cfg = alloc_slice[a](config.size(), 0);
        let blob = alloc_slice[a](config.blob_size(), byte_of(0));
        config.defaults(cfg);
        let text = "# the service\n\nport = 8080\ndir=/var/lib/hooks\n  schedule 100,200,400  \r\ndeadline-ms =  500\nwindow-ms=60000\n";
        test.assert_eq(config.parse_file(text, cfg, blob), 0);
        test.assert_eq(config.port_of(cfg), 8080);
        test.assert_eq(config.dir_len(cfg), 14);
        test.assert_eq(int_of(blob[0]), '/');
        test.assert_eq(int_of(blob[13]), 's');
        test.assert_eq(config.sched_len(cfg), 11);
        test.assert_eq(int_of(blob[config.sched_at()]), '1');
        test.assert_eq(config.deadline_ms(cfg), 500);
        test.assert_eq(config.window_ms(cfg), 60000);
    }
    return 0;
}

fn test_the_later_setting_wins() -> [] int {
    region a {
        let cfg = alloc_slice[a](config.size(), 0);
        let blob = alloc_slice[a](config.blob_size(), byte_of(0));
        config.defaults(cfg);
        test.assert_eq(config.parse_file("port = 1\nport = 2\n", cfg, blob), 0);
        test.assert_eq(config.port_of(cfg), 2);
        test.assert_eq(config.set(cfg, blob, "port", "3"), 0);
        test.assert_eq(config.port_of(cfg), 3);
        // a shorter directory over a longer one leaves only the shorter
        config.set(cfg, blob, "dir", "/long/directory");
        config.set(cfg, blob, "dir", "/d");
        test.assert_eq(config.dir_len(cfg), 2);
    }
    return 0;
}

fn test_a_file_with_a_wrong_line_is_refused_at_that_line() -> [] int {
    region a {
        let cfg = alloc_slice[a](config.size(), 0);
        let blob = alloc_slice[a](config.blob_size(), byte_of(0));
        config.defaults(cfg);
        // an unknown setting, with the line it is on
        test.assert_eq(config.parse_file("port = 1\n\nprot = 2\n", cfg, blob), 3);
        test.assert_eq(config.why(cfg), config.why_key());
        // the line before it took effect, which is why a refused file means the service does not start
        test.assert_eq(config.port_of(cfg), 1);
        // a key with no value, and a value with no key
        test.assert_eq(config.parse_file("port\n", cfg, blob), 1);
        test.assert_eq(config.why(cfg), config.why_line());
        test.assert_eq(config.parse_file("# c\n = 5\n", cfg, blob), 2);
        test.assert_eq(config.why(cfg), config.why_line());
        test.assert_eq(config.parse_file("port =\n", cfg, blob), 1);
    }
    return 0;
}

fn refused_value[&k, &v](key: &k [byte], value: &v [byte]) -> [] int {
    region a {
        let cfg = alloc_slice[a](config.size(), 0);
        let blob = alloc_slice[a](config.blob_size(), byte_of(0));
        config.defaults(cfg);
        config.set(cfg, blob, "port", "9");
        config.set(cfg, blob, "dir", "/d");
        let before = config.port_of(cfg) * 1000 + config.dir_len(cfg) * 100 + config.window_ms(cfg);
        let r = config.set(cfg, blob, key, value);
        let after = config.port_of(cfg) * 1000 + config.dir_len(cfg) * 100 + config.window_ms(cfg);
        // a refusal changes nothing
        test.assert_eq(before, after);
        test.assert_eq(config.deadline_ms(cfg), 0);
        test.assert_eq(config.sched_len(cfg), 0);
        return r;
    }
}

fn test_a_value_a_setting_does_not_take_is_refused_and_changes_nothing() -> [] int {
    test.assert_eq(refused_value("port", "0"), config.why_value());
    test.assert_eq(refused_value("port", "65536"), config.why_value());
    test.assert_eq(refused_value("port", "-1"), config.why_value());
    test.assert_eq(refused_value("port", "80x"), config.why_value());
    test.assert_eq(refused_value("port", ""), config.why_value());
    test.assert_eq(refused_value("window-ms", "-5"), config.why_value());
    test.assert_eq(refused_value("window-ms", "1234567890123"), config.why_value());
    test.assert_eq(refused_value("deadline-ms", "soon"), config.why_value());
    test.assert_eq(refused_value("schedule", ""), config.why_value());
    test.assert_eq(refused_value("dir", ""), config.why_value());
    test.assert_eq(refused_value("nonsense", "1"), config.why_key());
    test.assert_eq(refused_value("Port", "1"), config.why_key());
    test.assert_eq(refused_value("", "1"), config.why_key());
    return 0;
}

fn test_the_edges_of_what_is_accepted() -> [] int {
    region a {
        let cfg = alloc_slice[a](config.size(), 0);
        let blob = alloc_slice[a](config.blob_size(), byte_of(0));
        config.defaults(cfg);
        test.assert_eq(config.set(cfg, blob, "port", "1"), 0);
        test.assert_eq(config.set(cfg, blob, "port", "65535"), 0);
        test.assert_eq(config.port_of(cfg), 65535);
        test.assert_eq(config.set(cfg, blob, "window-ms", "0"), 0);
        test.assert_eq(config.window_ms(cfg), 0);
        test.assert_eq(config.set(cfg, blob, "window-ms", "999999999999"), 0);
        let long = alloc_slice[a](2001, byte_of('d'));
        test.assert_eq(config.set(cfg, blob, "dir", long[0..2000]), 0);
        test.assert_eq(config.dir_len(cfg), 2000);
        test.assert_eq(config.set(cfg, blob, "dir", long[0..2001]), config.why_value());
        test.assert_eq(config.dir_len(cfg), 2000);
        let sched = alloc_slice[a](251, byte_of('1'));
        test.assert_eq(config.set(cfg, blob, "schedule", sched[0..250]), 0);
        test.assert_eq(config.set(cfg, blob, "schedule", sched[0..251]), config.why_value());
        // the schedule's bytes stay clear of the directory's
        test.assert_eq(int_of(blob[1999]), 'd');
        test.assert_eq(int_of(blob[config.sched_at()]), '1');
    }
    return 0;
}

fn test_one_argument_is_a_flag_with_its_value_or_without_it() -> [] int {
    let a = config.split_flag("--port");
    test.assert_eq(a.0, 2);
    test.assert_eq(a.1, 6);
    test.assert_eq(a.2, 0 - 1);
    let b = config.split_flag("--window-ms=60000");
    test.assert_eq(b.0, 2);
    test.assert_eq(b.1, 11);
    test.assert_eq(b.2, 12);
    test.assert_eq(b.3, 17);
    // the first `=` splits, so a value may hold one
    let c = config.split_flag("--dir=/a=b");
    test.assert_eq(c.1, 5);
    test.assert_eq(c.3, 10);
    // an empty value is still a value
    let d = config.split_flag("--dir=");
    test.assert_eq(d.2, 6);
    test.assert_eq(d.3, 6);
    // not flags
    test.assert_eq(config.split_flag("8080").0, 0 - 1);
    test.assert_eq(config.split_flag("-p").0, 0 - 1);
    test.assert_eq(config.split_flag("--").0, 0 - 1);
    test.assert_eq(config.split_flag("").0, 0 - 1);
    return 0;
}

// The database settings: each is kept where it says, the port is checked, and the strings do not run into each other.
fn test_the_database_settings() -> [] int {
    region a {
        let cfg = alloc_slice[a](config.size(), 0);
        let blob = alloc_slice[a](config.blob_size(), byte_of(0));
        config.defaults(cfg);
        let text = "pg-host = db.internal\npg-port = 6432\npg-user = hooks_rw\npg-database = hooksdb\npg-password = s3cret pass\n";
        test.assert_eq(config.parse_file(text, cfg, blob), 0);
        test.assert_eq(config.pg_port(cfg), 6432);
        test.assert_eq(config.pg_host_len(cfg), 11);
        test.assert_eq(int_of(blob[config.pg_host_at()]), 'd');
        test.assert_eq(int_of(blob[config.pg_host_at() + 10]), 'l');
        test.assert_eq(config.pg_user_len(cfg), 8);
        test.assert_eq(int_of(blob[config.pg_user_at()]), 'h');
        test.assert_eq(config.pg_database_len(cfg), 7);
        test.assert_eq(int_of(blob[config.pg_database_at()]), 'h');
        test.assert_eq(config.pg_password_len(cfg), 11);
        test.assert_eq(int_of(blob[config.pg_password_at() + 6]), ' ');
        test.assert_eq(int_of(blob[config.pg_password_at() + 10]), 's');
        // the neighbours are not written over
        test.assert_eq(int_of(blob[config.pg_host_at() + 11]), 0);
        test.assert_eq(config.dir_len(cfg), 0);
    }
    return 0;
}

fn test_the_database_settings_have_limits() -> [] int {
    test.assert_eq(refused_value("pg-port", "0"), config.why_value());
    test.assert_eq(refused_value("pg-port", "65536"), config.why_value());
    test.assert_eq(refused_value("pg-port", "five"), config.why_value());
    test.assert_eq(refused_value("pg-host", ""), config.why_value());
    test.assert_eq(refused_value("pg-user", ""), config.why_value());
    test.assert_eq(refused_value("pg-database", ""), config.why_value());
    test.assert_eq(refused_value("pg-password", ""), config.why_value());
    region a {
        let cfg = alloc_slice[a](config.size(), 0);
        let blob = alloc_slice[a](config.blob_size(), byte_of(0));
        config.defaults(cfg);
        let long = alloc_slice[a](300, byte_of('x'));
        test.assert_eq(config.set(cfg, blob, "pg-host", long[0..253]), 0);
        test.assert_eq(config.set(cfg, blob, "pg-host", long[0..254]), config.why_value());
        test.assert_eq(config.set(cfg, blob, "pg-user", long[0..63]), 0);
        test.assert_eq(config.set(cfg, blob, "pg-user", long[0..64]), config.why_value());
        test.assert_eq(config.set(cfg, blob, "pg-database", long[0..63]), 0);
        test.assert_eq(config.set(cfg, blob, "pg-database", long[0..64]), config.why_value());
        test.assert_eq(config.set(cfg, blob, "pg-password", long[0..255]), 0);
        test.assert_eq(config.set(cfg, blob, "pg-password", long[0..256]), config.why_value());
        // the largest of each ends inside its own place and touches no other
        test.assert_eq(int_of(blob[config.pg_host_at() + 252]), 'x');
        test.assert_eq(int_of(blob[config.pg_host_at() + 253]), 0);
        test.assert_eq(int_of(blob[config.pg_user_at() + 62]), 'x');
        test.assert_eq(int_of(blob[config.pg_user_at() + 63]), 0);
        test.assert_eq(int_of(blob[config.pg_database_at() + 62]), 'x');
        test.assert_eq(int_of(blob[config.pg_database_at() + 63]), 0);
        test.assert_eq(int_of(blob[config.pg_password_at() + 254]), 'x');
        test.assert_eq(int_of(blob[config.pg_password_at() + 255]), 0);
    }
    return 0;
}

fn test_import_endpoints_is_zero_or_one() -> [] int {
    region a {
        let cfg = alloc_slice[a](config.size(), 0);
        let blob = alloc_slice[a](config.blob_size(), byte_of(0));
        config.defaults(cfg);
        test.assert(!config.import_endpoints(cfg));
        test.assert_eq(config.set(cfg, blob, "import-endpoints", "1"), 0);
        test.assert(config.import_endpoints(cfg));
        test.assert_eq(config.set(cfg, blob, "import-endpoints", "yes"), config.why_value());
        test.assert(config.import_endpoints(cfg));
        test.assert_eq(config.set(cfg, blob, "import-endpoints", "0"), 0);
        test.assert(!config.import_endpoints(cfg));
        test.assert(!config.allow_private_hosts(cfg));
        test.assert_eq(config.set(cfg, blob, "allow-private-hosts", "1"), 0);
        test.assert(config.allow_private_hosts(cfg));
        test.assert_eq(config.set(cfg, blob, "allow-private-hosts", "yes"), config.why_value());
        test.assert(config.allow_private_hosts(cfg));
        test.assert_eq(config.set(cfg, blob, "allow-private-hosts", "0"), 0);
        test.assert(!config.allow_private_hosts(cfg));
    }
    return 0;
}

fn test_the_cron_settings_are_zero_or_one_and_catch_up_is_on_by_default() -> [] int {
    region a {
        let cfg = alloc_slice[a](config.size(), 0);
        let blob = alloc_slice[a](config.blob_size(), byte_of(0));
        config.defaults(cfg);
        test.assert_eq(config.cron_catchup(cfg), 1);
        test.assert_eq(config.cron_seconds(cfg), 0);
        test.assert_eq(config.set(cfg, blob, "cron-catchup", "0"), 0);
        test.assert_eq(config.cron_catchup(cfg), 0);
        test.assert_eq(config.set(cfg, blob, "cron-catchup", "2"), config.why_value());
        test.assert_eq(config.set(cfg, blob, "cron-catchup", "on"), config.why_value());
        test.assert_eq(config.set(cfg, blob, "cron-catchup", ""), config.why_value());
        test.assert_eq(config.cron_catchup(cfg), 0);
        test.assert_eq(config.set(cfg, blob, "cron-catchup", "1"), 0);
        test.assert_eq(config.cron_catchup(cfg), 1);
        test.assert_eq(config.set(cfg, blob, "cron-seconds", "1"), 0);
        test.assert_eq(config.cron_seconds(cfg), 1);
        test.assert_eq(config.set(cfg, blob, "cron-seconds", "yes"), config.why_value());
        test.assert_eq(config.cron_seconds(cfg), 1);
        test.assert_eq(config.set(cfg, blob, "cron-seconds", "0"), 0);
        test.assert_eq(config.cron_seconds(cfg), 0);
        // a setting of the other kind is not a cron one
        test.assert_eq(config.set(cfg, blob, "cron", "1"), config.why_key());
    }
    return 0;
}

fn test_the_admin_token_is_eight_to_255_visible_characters() -> [] int {
    region a {
        let cfg = alloc_slice[a](config.size(), 0);
        let blob = alloc_slice[a](config.blob_size(), byte_of(0));
        config.defaults(cfg);
        test.assert_eq(config.token_len(cfg), 0);
        test.assert_eq(config.set(cfg, blob, "admin-token", "seven77"), config.why_value());
        test.assert_eq(config.set(cfg, blob, "admin-token", "has a space"), config.why_value());
        test.assert_eq(config.token_len(cfg), 0);
        test.assert_eq(config.set(cfg, blob, "admin-token", "eight888"), 0);
        test.assert_eq(config.token_len(cfg), 8);
        test.assert_eq(int_of(blob[config.token_at()]), 'e');
        test.assert_eq(int_of(blob[config.token_at() + 7]), '8');
        // a refused token leaves the one that was set
        test.assert_eq(config.set(cfg, blob, "admin-token", "tab\there1"), config.why_value());
        test.assert_eq(config.token_len(cfg), 8);
        let long = "0123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789";
        test.assert_eq(config.set(cfg, blob, "admin-token", long[0..255]), 0);
        test.assert_eq(config.token_len(cfg), 255);
        test.assert_eq(config.set(cfg, blob, "admin-token", long[0..256]), config.why_value());
        // the largest token ends inside its own place: the last byte of the blob is the token's
        test.assert_eq(int_of(blob[config.token_at() + 254]), int_of(long[254]));
        // the three tokens' places are side by side and the read token's is the last of the blob
        test.assert_eq(config.token_at() + 256, config.ingest_token_at());
        test.assert_eq(config.ingest_token_at() + 256, config.read_token_at());
        test.assert_eq(len(blob), config.read_token_at() + 256);
    }
    return 0;
}

// `ingest-token` and `read-token` (`docs/design.md` section 33) follow the admin token's rule exactly -- 8 to 255 visible characters, a
// refusal leaves the one that was set -- and each lands in its own place without touching the others.
fn test_the_ingest_and_read_tokens_follow_the_admin_tokens_rule() -> [] int {
    region a {
        let cfg = alloc_slice[a](config.size(), 0);
        let blob = alloc_slice[a](config.blob_size(), byte_of(0));
        config.defaults(cfg);
        test.assert_eq(config.ingest_token_len(cfg), 0);
        test.assert_eq(config.read_token_len(cfg), 0);
        test.assert_eq(config.set(cfg, blob, "ingest-token", "seven77"), config.why_value());
        test.assert_eq(config.set(cfg, blob, "read-token", "seven77"), config.why_value());
        test.assert_eq(config.set(cfg, blob, "ingest-token", "has a space"), config.why_value());
        test.assert_eq(config.set(cfg, blob, "read-token", "tab\there1"), config.why_value());
        test.assert_eq(config.set(cfg, blob, "ingest-token", "nul\0nul123"), config.why_value());
        test.assert_eq(config.ingest_token_len(cfg), 0);
        test.assert_eq(config.read_token_len(cfg), 0);
        test.assert_eq(config.set(cfg, blob, "admin-token", "aaaaaaaa"), 0);
        test.assert_eq(config.set(cfg, blob, "ingest-token", "iiiiiiiii"), 0);
        test.assert_eq(config.set(cfg, blob, "read-token", "rrrrrrrrrr"), 0);
        test.assert_eq(config.token_len(cfg), 8);
        test.assert_eq(config.ingest_token_len(cfg), 9);
        test.assert_eq(config.read_token_len(cfg), 10);
        test.assert_eq(int_of(blob[config.token_at()]), 'a');
        test.assert_eq(int_of(blob[config.ingest_token_at()]), 'i');
        test.assert_eq(int_of(blob[config.ingest_token_at() + 8]), 'i');
        test.assert_eq(int_of(blob[config.read_token_at()]), 'r');
        test.assert_eq(int_of(blob[config.read_token_at() + 9]), 'r');
        // a refused token leaves the one that was set
        test.assert_eq(config.set(cfg, blob, "ingest-token", "short"), config.why_value());
        test.assert_eq(config.ingest_token_len(cfg), 9);
        let long = "0123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789012345678901234567890123456789";
        test.assert_eq(config.set(cfg, blob, "ingest-token", long[0..255]), 0);
        test.assert_eq(config.set(cfg, blob, "read-token", long[0..255]), 0);
        test.assert_eq(config.set(cfg, blob, "ingest-token", long[0..256]), config.why_value());
        test.assert_eq(config.set(cfg, blob, "read-token", long[0..256]), config.why_value());
        // the longest ones fill their own places and the admin token is untouched by either
        test.assert_eq(int_of(blob[config.ingest_token_at() + 254]), int_of(long[254]));
        test.assert_eq(int_of(blob[config.read_token_at() + 254]), int_of(long[254]));
        test.assert_eq(int_of(blob[config.token_at()]), 'a');
        test.assert_eq(config.token_len(cfg), 8);
    }
    return 0;
}

// `production` is 0 or 1, 0 until set, and a file can set it.
fn test_production_is_zero_or_one() -> [] int {
    region a {
        let cfg = alloc_slice[a](config.size(), 0);
        let blob = alloc_slice[a](config.blob_size(), byte_of(0));
        config.defaults(cfg);
        test.assert(!config.production(cfg));
        test.assert_eq(config.set(cfg, blob, "production", "1"), 0);
        test.assert(config.production(cfg));
        test.assert_eq(config.set(cfg, blob, "production", "2"), config.why_value());
        test.assert_eq(config.set(cfg, blob, "production", "yes"), config.why_value());
        test.assert_eq(config.set(cfg, blob, "production", ""), config.why_value());
        test.assert(config.production(cfg));
        test.assert_eq(config.set(cfg, blob, "production", "0"), 0);
        test.assert(!config.production(cfg));
        test.assert_eq(config.parse_file("production = 1\n", cfg, blob), 0);
        test.assert(config.production(cfg));
    }
    return 0;
}

// The production profile's judgement of the settings (`config.production_status`): one status for each cause, and the order in which they are
// found (the admin token first), so that a service with several things wrong says one at a time, the same one each time.
fn test_the_production_profile_names_one_cause_at_a_time() -> [] int {
    region a {
        let cfg = alloc_slice[a](config.size(), 0);
        let blob = alloc_slice[a](config.blob_size(), byte_of(0));
        config.defaults(cfg);
        test.assert_eq(config.production_status(cfg, blob), 30);
        config.set(cfg, blob, "ingest-token", "iiiiiiii");
        test.assert_eq(config.production_status(cfg, blob), 30);
        config.set(cfg, blob, "ingest-token", "iiiiiiii");
        config.set(cfg, blob, "admin-token", "aaaaaaaa");
        test.assert_eq(config.production_status(cfg, blob), 0);
        // the read token is optional
        config.set(cfg, blob, "read-token", "rrrrrrrr");
        test.assert_eq(config.production_status(cfg, blob), 0);
        // private hosts
        config.set(cfg, blob, "allow-private-hosts", "1");
        test.assert_eq(config.production_status(cfg, blob), 32);
        config.set(cfg, blob, "allow-private-hosts", "0");
        test.assert_eq(config.production_status(cfg, blob), 0);
        // two tokens the same, each pair, and a prefix is not the same
        config.set(cfg, blob, "ingest-token", "aaaaaaaa");
        test.assert_eq(config.production_status(cfg, blob), 34);
        config.set(cfg, blob, "ingest-token", "aaaaaaaaa");
        test.assert_eq(config.production_status(cfg, blob), 0);
        config.set(cfg, blob, "read-token", "aaaaaaaa");
        test.assert_eq(config.production_status(cfg, blob), 34);
        config.set(cfg, blob, "read-token", "aaaaaaaaa");
        test.assert_eq(config.production_status(cfg, blob), 34);
        config.set(cfg, blob, "read-token", "aaaaaaaaaa");
        test.assert_eq(config.production_status(cfg, blob), 0);
        // no ingest token
        let none = alloc_slice[a](config.size(), 0);
        config.defaults(none);
        config.set(none, blob, "admin-token", "aaaaaaaa");
        test.assert_eq(config.production_status(none, blob), 31);
    }
    return 0;
}

// Every status the profile ends with has a message that names what is wrong, and they are all different.
fn test_each_production_refusal_says_which_setting() -> [] int {
    test.assert(bytes.find(config.unsafe_message(30), "admin-token") >= 0);
    test.assert(bytes.find(config.unsafe_message(31), "ingest-token") >= 0);
    test.assert(bytes.find(config.unsafe_message(32), "allow-private-hosts") >= 0);
    test.assert(bytes.find(config.unsafe_message(34), "ingest-token") >= 0);
    test.assert(bytes.find(config.unsafe_message(33), "umask") >= 0);
    test.assert(!bytes.equal(config.unsafe_message(30), config.unsafe_message(31)));
    test.assert(!bytes.equal(config.unsafe_message(33), config.unsafe_message(35)));
    return 0;
}

// `breaker-days` (`docs/design.md` section 31): 5 unless set, 0 is a setting (off), 36,500 is the most, and a refusal changes nothing.
fn test_breaker_days_default_off_and_limits() -> [] int {
    region a {
        let cfg = alloc_slice[a](config.size(), 0);
        let blob = alloc_slice[a](config.blob_size(), byte_of(0));
        config.defaults(cfg);
        test.assert_eq(config.breaker_days(cfg), 5);
        test.assert_eq(config.set(cfg, blob, "breaker-days", "0"), 0);
        test.assert_eq(config.breaker_days(cfg), 0);
        test.assert_eq(config.set(cfg, blob, "breaker-days", "36500"), 0);
        test.assert_eq(config.breaker_days(cfg), 36500);
        test.assert_eq(config.set(cfg, blob, "breaker-days", "7"), 0);
        test.assert_eq(config.breaker_days(cfg), 7);
        test.assert(config.set(cfg, blob, "breaker-days", "36501") != 0);
        test.assert(config.set(cfg, blob, "breaker-days", "-1") != 0);
        test.assert(config.set(cfg, blob, "breaker-days", "five") != 0);
        test.assert(config.set(cfg, blob, "breaker-days", "") != 0);
        test.assert_eq(config.breaker_days(cfg), 7);
        test.assert_eq(config.parse_file("breaker-days = 2\n", cfg, blob), 0);
        test.assert_eq(config.breaker_days(cfg), 2);
    }
    return 0;
}

// `stop-deadline-ms` (5000 unless set, 0 is a setting, an hour is the most) and `repair-logs` (0 or 1, off unless set): `docs/design.md` sections 34.4 and 34.5.
fn test_stop_deadline_and_repair_logs() -> [] int {
    region a {
        let cfg = alloc_slice[a](config.size(), 0);
        let blob = alloc_slice[a](config.blob_size(), byte_of(0));
        config.defaults(cfg);
        test.assert_eq(config.stop_deadline_ms(cfg), 5000);
        test.assert(!config.repair_logs(cfg));
        test.assert_eq(config.set(cfg, blob, "stop-deadline-ms", "0"), 0);
        test.assert_eq(config.stop_deadline_ms(cfg), 0);
        test.assert_eq(config.set(cfg, blob, "stop-deadline-ms", "3600000"), 0);
        test.assert_eq(config.stop_deadline_ms(cfg), 3600000);
        test.assert(config.set(cfg, blob, "stop-deadline-ms", "3600001") != 0);
        test.assert(config.set(cfg, blob, "stop-deadline-ms", "-1") != 0);
        test.assert(config.set(cfg, blob, "stop-deadline-ms", "soon") != 0);
        test.assert(config.set(cfg, blob, "stop-deadline-ms", "") != 0);
        test.assert_eq(config.stop_deadline_ms(cfg), 3600000);
        test.assert_eq(config.set(cfg, blob, "repair-logs", "1"), 0);
        test.assert(config.repair_logs(cfg));
        test.assert(config.set(cfg, blob, "repair-logs", "2") != 0);
        test.assert(config.set(cfg, blob, "repair-logs", "yes") != 0);
        test.assert(config.repair_logs(cfg));
        test.assert_eq(config.set(cfg, blob, "repair-logs", "0"), 0);
        test.assert(!config.repair_logs(cfg));
        test.assert_eq(config.parse_file("stop-deadline-ms = 1200\nrepair-logs = 1\n", cfg, blob), 0);
        test.assert_eq(config.stop_deadline_ms(cfg), 1200);
        test.assert(config.repair_logs(cfg));
    }
    return 0;
}

// `rotation-grace-ms` (`docs/design.md` section 35): a day unless set, 1 ms to 30 days, and a refusal changes nothing.
fn test_rotation_grace_default_and_limits() -> [] int {
    region a {
        let cfg = alloc_slice[a](config.size(), 0);
        let blob = alloc_slice[a](config.blob_size(), byte_of(0));
        config.defaults(cfg);
        test.assert_eq(config.rotation_grace_ms(cfg), 86400000);
        test.assert_eq(config.set(cfg, blob, "rotation-grace-ms", "1"), 0);
        test.assert_eq(config.rotation_grace_ms(cfg), 1);
        test.assert_eq(config.set(cfg, blob, "rotation-grace-ms", "2592000000"), 0);
        test.assert_eq(config.rotation_grace_ms(cfg), 2592000000);
        test.assert_eq(config.set(cfg, blob, "rotation-grace-ms", "3600000"), 0);
        test.assert(config.set(cfg, blob, "rotation-grace-ms", "0") != 0);
        test.assert(config.set(cfg, blob, "rotation-grace-ms", "2592000001") != 0);
        test.assert(config.set(cfg, blob, "rotation-grace-ms", "-1") != 0);
        test.assert(config.set(cfg, blob, "rotation-grace-ms", "a day") != 0);
        test.assert(config.set(cfg, blob, "rotation-grace-ms", "") != 0);
        test.assert_eq(config.rotation_grace_ms(cfg), 3600000);
        test.assert_eq(config.parse_file("rotation-grace-ms = 5000\n", cfg, blob), 0);
        test.assert_eq(config.rotation_grace_ms(cfg), 5000);
    }
    return 0;
}

// The retention settings (`docs/retention.md` section 3): their defaults, each at the edges of what it takes, and that a refusal changes nothing.
fn test_retention_settings_defaults_and_edges() -> [] int {
    region a {
        let cfg = alloc_slice[a](config.size(), 0);
        let blob = alloc_slice[a](config.blob_size(), byte_of(0));
        config.defaults(cfg);
        test.assert_eq(config.retention_days(cfg), 30);
        test.assert_eq(config.segment_bytes(cfg), 67108864);
        test.assert_eq(config.delivery_log_bytes(cfg), 33554432);
        test.assert_eq(config.idem_keys(cfg), 262144);
        test.assert(!config.compact_now(cfg));
        test.assert_eq(config.retention_ms_knob(cfg), 0);
        test.assert_eq(config.compact_kill_at(cfg), 0);
        // 0 days keeps for ever; 36,500 is the most
        test.assert_eq(config.set(cfg, blob, "retention-days", "0"), 0);
        test.assert_eq(config.retention_days(cfg), 0);
        test.assert_eq(config.set(cfg, blob, "retention-days", "36500"), 0);
        test.assert_eq(config.retention_days(cfg), 36500);
        test.assert(config.set(cfg, blob, "retention-days", "36501") != 0);
        test.assert(config.set(cfg, blob, "retention-days", "a month") != 0);
        test.assert_eq(config.retention_days(cfg), 36500);
        // a segment of at least 256 KiB, a log limit of at least 64 KiB
        test.assert_eq(config.set(cfg, blob, "segment-bytes", "262144"), 0);
        test.assert_eq(config.segment_bytes(cfg), 262144);
        test.assert(config.set(cfg, blob, "segment-bytes", "262143") != 0);
        test.assert_eq(config.segment_bytes(cfg), 262144);
        test.assert_eq(config.set(cfg, blob, "delivery-log-bytes", "65536"), 0);
        test.assert(config.set(cfg, blob, "delivery-log-bytes", "65535") != 0);
        test.assert_eq(config.delivery_log_bytes(cfg), 65536);
        // 16 to 4,194,304 keys
        test.assert_eq(config.set(cfg, blob, "idem-keys", "16"), 0);
        test.assert_eq(config.idem_keys(cfg), 16);
        test.assert(config.set(cfg, blob, "idem-keys", "15") != 0);
        test.assert_eq(config.set(cfg, blob, "idem-keys", "4194304"), 0);
        test.assert(config.set(cfg, blob, "idem-keys", "4194305") != 0);
        test.assert_eq(config.idem_keys(cfg), 4194304);
        // the switch and the two test knobs
        test.assert_eq(config.set(cfg, blob, "compact-now", "1"), 0);
        test.assert(config.compact_now(cfg));
        test.assert(config.set(cfg, blob, "compact-now", "yes") != 0);
        test.assert(config.compact_now(cfg));
        test.assert_eq(config.set(cfg, blob, "retention-ms", "1500"), 0);
        test.assert_eq(config.retention_ms_knob(cfg), 1500);
        test.assert_eq(config.set(cfg, blob, "compact-kill-at", "19"), 0);
        test.assert_eq(config.compact_kill_at(cfg), 19);
        test.assert(config.set(cfg, blob, "compact-kill-at", "65") != 0);
        // from a file
        test.assert_eq(config.parse_file("retention-days = 7\nsegment-bytes = 1048576\nidem-keys=5000\n", cfg, blob), 0);
        test.assert_eq(config.retention_days(cfg), 7);
        test.assert_eq(config.segment_bytes(cfg), 1048576);
        test.assert_eq(config.idem_keys(cfg), 5000);
    }
    return 0;
}

// The settings of the database's connections (`docs/design.md` section 37): each has a default, a range, and is read from a file; a refusal
// changes nothing; and the longest wait may not be below the first (`pg_status`, which is judged once every setting is in).
fn test_pg_connection_settings_default_and_limits() -> [] int {
    region a {
        let cfg = alloc_slice[a](config.size(), 0);
        let blob = alloc_slice[a](config.blob_size(), byte_of(0));
        config.defaults(cfg);
        test.assert_eq(config.pg_backoff_min_ms(cfg), 100);
        test.assert_eq(config.pg_backoff_max_ms(cfg), 5000);
        test.assert_eq(config.pg_attempt_ms(cfg), 5000);
        test.assert_eq(config.pg_request_ms(cfg), 10000);
        test.assert_eq(config.pg_start_wait_ms(cfg), 30000);
        test.assert_eq(config.pg_status(cfg), 0);
        test.assert_eq(config.set(cfg, blob, "pg-backoff-min-ms", "1"), 0);
        test.assert_eq(config.pg_backoff_min_ms(cfg), 1);
        test.assert_eq(config.set(cfg, blob, "pg-backoff-min-ms", "600000"), 0);
        test.assert(config.set(cfg, blob, "pg-backoff-min-ms", "0") != 0);
        test.assert(config.set(cfg, blob, "pg-backoff-min-ms", "600001") != 0);
        test.assert_eq(config.pg_backoff_min_ms(cfg), 600000);
        test.assert_eq(config.set(cfg, blob, "pg-backoff-max-ms", "3600000"), 0);
        test.assert(config.set(cfg, blob, "pg-backoff-max-ms", "0") != 0);
        test.assert(config.set(cfg, blob, "pg-backoff-max-ms", "3600001") != 0);
        test.assert(config.set(cfg, blob, "pg-backoff-max-ms", "soon") != 0);
        test.assert_eq(config.pg_backoff_max_ms(cfg), 3600000);
        test.assert_eq(config.set(cfg, blob, "pg-attempt-ms", "250"), 0);
        test.assert(config.set(cfg, blob, "pg-attempt-ms", "0") != 0);
        test.assert(config.set(cfg, blob, "pg-attempt-ms", "600001") != 0);
        test.assert_eq(config.pg_attempt_ms(cfg), 250);
        test.assert_eq(config.set(cfg, blob, "pg-request-ms", "0"), 0);
        test.assert_eq(config.pg_request_ms(cfg), 0);
        test.assert_eq(config.set(cfg, blob, "pg-request-ms", "3600000"), 0);
        test.assert(config.set(cfg, blob, "pg-request-ms", "3600001") != 0);
        test.assert(config.set(cfg, blob, "pg-request-ms", "-1") != 0);
        test.assert(config.set(cfg, blob, "pg-request-ms", "") != 0);
        test.assert_eq(config.pg_request_ms(cfg), 3600000);
        test.assert_eq(config.set(cfg, blob, "pg-start-wait-ms", "0"), 0);
        test.assert_eq(config.pg_start_wait_ms(cfg), 0);
        test.assert_eq(config.set(cfg, blob, "pg-start-wait-ms", "86400000"), 0);
        test.assert(config.set(cfg, blob, "pg-start-wait-ms", "86400001") != 0);
        test.assert(config.set(cfg, blob, "pg-start-wait-ms", "a while") != 0);
        test.assert_eq(config.pg_start_wait_ms(cfg), 86400000);
        test.assert_eq(config.parse_file("pg-backoff-min-ms = 20\npg-backoff-max-ms = 800\npg-attempt-ms = 1500\npg-request-ms = 4000\npg-start-wait-ms = 9000\n", cfg, blob), 0);
        test.assert_eq(config.pg_backoff_min_ms(cfg), 20);
        test.assert_eq(config.pg_backoff_max_ms(cfg), 800);
        test.assert_eq(config.pg_attempt_ms(cfg), 1500);
        test.assert_eq(config.pg_request_ms(cfg), 4000);
        test.assert_eq(config.pg_start_wait_ms(cfg), 9000);
        test.assert_eq(config.pg_status(cfg), 0);
        test.assert_eq(config.set(cfg, blob, "pg-backoff-max-ms", "19"), 0);
        test.assert_eq(config.pg_status(cfg), 1);
        test.assert_eq(config.set(cfg, blob, "pg-backoff-max-ms", "20"), 0);
        test.assert_eq(config.pg_status(cfg), 0);
    }
    return 0;
}

