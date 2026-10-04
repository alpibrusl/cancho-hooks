#!/bin/sh
# The container's entry point: a umask of 077, then the service. With `production = 1` the service refuses to start when its data
# directory or its logs can be read or written by the group or by others (docs/design.md section 33), and the logs it makes itself are
# 0666 less the umask: under the default umask of 022 they would be 0644 and the first start in production would be refused.
# It is `exec`: the service replaces this shell, so it is the process `tini` signals and the one whose exit status is the container's.
umask 077
exec /usr/local/bin/hooks "$@"
