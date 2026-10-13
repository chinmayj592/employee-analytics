# DECISIONS.md

**Indexes.**
I created six indexes. A unique index on `employees.emp_code` enforces uniqueness and serves every employee lookup. Indexes on `employees.department` and `employees.joined_on` serve department filters and headcount queries. On `attendance_logs` the unique compound index on `(emp_code, date)` is the concurrency guard for punch-in and addresses every record lookup by natural key. The compound index on `(date DESC, emp_code ASC)` serves the attendance list sort and pagination without a collection scan. A single-field index on `emp_code` serves punch-out and monthly analytics. I considered a partial index on `status` filtered to presence values for punch-out lookups but rejected it because the existing `emp_code` index is selective enough at current scale.

**Punch-in race.**
Both requests pass validation and reach `insert_one` with the same `emp_code` and attendance date. MongoDB's storage engine enforces the unique compound index atomically — exactly one insert succeeds and the other raises `DuplicateKeyError`. The winner returns 201; the loser catches the error and returns 409. There is no check-then-insert window.

**Ties.**
`$rank` in `$setWindowFields` assigns the same rank to tied employees and skips the next rank (1, 2, 2, 4). The subsequent `$match {rank: {$lte: limit}}` includes every employee at or below the cutoff rank, so if two employees tie exactly at `limit` both are returned. The final sort is `total_late_minutes DESC, emp_code ASC` for deterministic ordering.

**Headcount.**
The department summary pipeline starts from the `employees` collection, not `attendance_logs`. It matches employees whose `joined_on` is on or before the last day of the month, then left-joins logs with `$lookup`. Employees with no logs receive an empty array. The `$group` stage counts every employee document as 1 toward headcount regardless of whether their logs array is empty.

**100x data.**
I would materialise daily and monthly aggregates into a separate summary collection on every punch-in and punch-out (write-time fan-out), so analytics reads become simple indexed lookups instead of full pipeline scans. I would also shard `attendance_logs` on `emp_code` to distribute write load evenly.
