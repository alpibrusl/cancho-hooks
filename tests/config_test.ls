edition 5;

import std.test;
import config;

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
    }
    return 0;
}
