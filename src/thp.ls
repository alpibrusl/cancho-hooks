edition 5;

module thp;

// `thp` -- small pages for the whole process (`docs/design.md` section 46).
//
// The delivery state is one zero-filled block of about 280 MiB of address space that is resident only where it is written (section 41.2: an idle endpoint
// costs about 0.15 KB). With transparent huge pages set to `always` (the default of some distributions, and of CI's runners) the first write to any part of
// it makes a whole 2 MiB page resident instead of 4 KiB, so the memory the service holds follows how many 2 MiB stretches it has written once, up to the
// whole block: on CI a schedule's fires grew it 8 MiB in two minutes where a host with `madvise` saw 68 bytes a fire. `prctl(PR_SET_THP_DISABLE)` asks the
// kernel for small pages for this process only, whatever the host's setting; it is the one other function the service calls in libc (after `statx`).

extern fn prctl[&f](ffi: &f Ffi("libc"), option: int, arg2: int, arg3: int, arg4: int, arg5: int) -> [ffi("libc")] c_int;

fn pr_set_thp_disable() -> [] int {
    return 41;
}

// Ask for small pages. Answers 0, or what the kernel said (a kernel without the option refuses it; the service goes on with the host's setting).
pub fn small_pages[&f](ffi: &f Ffi("libc")) -> [ffi("libc")] int {
    if prctl(ffi, pr_set_thp_disable(), 1, 0, 0, 0) != 0 {
        return 0 - 1;
    }
    return 0;
}
