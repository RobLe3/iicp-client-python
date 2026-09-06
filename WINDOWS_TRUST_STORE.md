# Windows dispatch trust-store boundary

The opt-in dispatch-ticket v2 file store uses a protected Windows DACL for the
current identity and LocalSystem. Its directory, lock and temporary state files
receive restrictive descriptors at creation. Existing ACLs are verified rather
than silently repaired. Relative Windows paths are made absolute without resolving
away symlinks or junctions.

Load and update reject broad ACLs, unexpected owners, reparse paths and unavailable
verification tools. The bounded Windows PowerShell adapter follows the same ACL
rules as the maintained TypeScript and Rust stores. It uses a fixed operation and
an encoded path, without an interactive profile or shell interpolation. This does
not prevent administrators from exercising Windows ownership/recovery privileges.

File data is flushed before atomic replacement. Unix additionally flushes the
parent directory; Windows does not claim that additional crash-durability barrier.
Ordinary recovery tests are not power-loss tests.

ACL verification starts local processes and has measurable overhead. Applications
can set `lock_timeout_s` explicitly when contention warrants a larger bounded
budget. The serialization fixture uses a fifteen-second Windows contention budget;
the separate zero-timeout fixture still verifies refusal. The default remains two
seconds. No high-throughput or qualification claim follows from positive store tests.
