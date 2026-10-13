# Employee Attendance & Analytics API

## Prerequisites

- Python 3.11+
- MongoDB 6.0+ (local Docker or Atlas free tier M0)

## Local setup

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt
```

Create a `.env` file at the project root:

```
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

For local Docker:
```bash
docker run -p 27017:27017 mongo:7
```

For Atlas: `MONGO_URI=mongodb+srv://<user>:<password>@<cluster>.mongodb.net/`

Real environment variables always take priority over `.env`.

## Seed sample data

```bash
python sample_seed.py
```

**Warning:** This drops and recreates the `employees` and `attendance_logs` collections. Run only against a disposable development database, never against a valuable one.

## Start the server

```bash
uvicorn app.main:app --port 8000
```

Run from the project root. All required indexes are created at startup (idempotent — safe to restart).

## API documentation

Interactive docs: http://localhost:8000/docs  
OpenAPI JSON: http://localhost:8000/openapi.json

## Verify

```bash
# Health check (also confirms MongoDB is reachable)
curl http://localhost:8000/health

# Create an employee
curl -X POST http://localhost:8000/employees \
  -H "Content-Type: application/json" \
  -d '{"emp_code":"EMP0001","name":"Asha Rao","email":"asha@example.com","department":"Engineering","joined_on":"2026-01-05"}'

# Punch in
curl -X POST http://localhost:8000/attendance/punch-in \
  -H "Content-Type: application/json" \
  -d '{"emp_code":"EMP0001","punched_at":1751775480000}'

# List attendance
curl "http://localhost:8000/attendance?emp_code=EMP0001"

# Monthly analytics
curl "http://localhost:8000/analytics/employees/EMP0001/monthly?month=2026-07"

# Department summary
curl "http://localhost:8000/analytics/departments/summary?month=2026-07"

# Late leaderboard
curl "http://localhost:8000/analytics/leaderboard/late?month=2026-07"

# Department trend
curl "http://localhost:8000/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-31"

# Explain (proves index usage)
curl "http://localhost:8000/admin/explain/attendance_list?emp_code=EMP0001"
```

## Implementation notes

- All timestamps in requests/responses are **epoch milliseconds** (integers). Floats, strings, booleans, and values below `100000000000` are rejected with 422.
- Attendance dates are IST (UTC+05:30) calendar dates. The server does not need to run in IST.
- Overnight shifts (`shift_end <= shift_start`, e.g. 22:00–06:00) are handled correctly per R1.
- Late minutes (R2), work hours (R4), overtime (R3), and half-day (R5) use the exact boundary rules from the contract.
- All rounding uses decimal half-up (not Python's built-in `round()`).
- The `$densify` stage (department trend gap-filling) requires MongoDB 5.1+.
- The `$setWindowFields` / `$rank` stage (late leaderboard) requires MongoDB 5.0+.
- Both are within the MongoDB 6.0+ requirement.
- No authentication is implemented; `regularized_by` is free text.

## What is missing

Nothing. All 12 endpoints are implemented:

| Method | Path | Status |
|--------|------|--------|
| GET | /health | ✓ |
| POST | /employees | ✓ |
| GET | /employees | ✓ |
| POST | /attendance/punch-in | ✓ |
| GET | /attendance | ✓ |
| POST | /attendance/punch-out | ✓ |
| PATCH | /attendance/{emp_code}/{date} | ✓ |
| GET | /analytics/employees/{emp_code}/monthly | ✓ |
| GET | /analytics/departments/summary | ✓ |
| GET | /analytics/leaderboard/late | ✓ |
| GET | /analytics/departments/{department}/trend | ✓ |
| GET | /admin/explain/{endpoint} | ✓ |
