"""
Employee Attendance & Analytics API
Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB
"""
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
MS_MIN = 100_000_000_000
MS_MAX = 4_102_444_800_000

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes():
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index("department")
    db.employees.create_index("joined_on")
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True
    )
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index("emp_code")
    db.attendance_logs.create_index("status")
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)]
    )


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------
def validate_ms(v: Any, field: str = "punched_at") -> int:
    if isinstance(v, bool):
        raise ValueError(f"{field} must be integer epoch milliseconds")
    if not isinstance(v, int):
        raise ValueError(f"{field} must be integer epoch milliseconds")
    if v < MS_MIN or v > MS_MAX:
        raise ValueError(f"{field} out of valid millisecond range")
    return v


def ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def dt_to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) * 1000


def dt_to_ms_nullable(dt) -> Optional[int]:
    if dt is None:
        return None
    return dt_to_ms(dt)


# ---------------------------------------------------------------------------
# Business rule helpers
# ---------------------------------------------------------------------------
def half_up(value: float, decimals: int = 2) -> float:
    q = Decimal(10) ** -decimals
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def _aware(dt: datetime) -> datetime:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def compute_late_minutes(punch_in_utc: datetime, shift_start: str) -> int:
    """R2: late only if strictly > 10 min after shift_start; floor from shift_start."""
    punch_in_utc = _aware(punch_in_utc)
    punch_ist = punch_in_utc.astimezone(IST)
    h, m = map(int, shift_start.split(":"))
    start_ist = punch_ist.replace(hour=h, minute=m, second=0, microsecond=0)
    diff_sec = int((punch_ist - start_ist).total_seconds())
    if diff_sec <= 600:
        return 0
    return diff_sec // 60


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4: half-up to 2 decimals."""
    secs = int((_aware(punch_out_utc) - _aware(punch_in_utc)).total_seconds())
    return half_up(secs / 3600, 2)


def compute_overtime(punch_out_utc: datetime, shift_end: str, shift_start: str, att_date: str) -> int:
    """R3: floor minutes after shift_end, only if >= 30; handles overnight."""
    punch_out_utc = _aware(punch_out_utc)
    eh, em = map(int, shift_end.split(":"))
    sh, sm = map(int, shift_start.split(":"))
    base = datetime.fromisoformat(att_date).replace(tzinfo=IST)
    end_ist = base.replace(hour=eh, minute=em, second=0, microsecond=0)
    if (eh, em) <= (sh, sm):  # overnight
        end_ist += timedelta(days=1)
    end_utc = end_ist.astimezone(timezone.utc)
    diff = int((punch_out_utc - end_utc).total_seconds())
    if diff < 1800:
        return 0
    return diff // 60


def attendance_date_ist(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date for punch-in, handling overnight shifts."""
    punch_in_utc = _aware(punch_in_utc)
    punch_ist = punch_in_utc.astimezone(IST)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    if (eh, em) <= (sh, sm):  # overnight shift
        if (punch_ist.hour, punch_ist.minute, punch_ist.second) < (eh, em, 0):
            return (punch_ist - timedelta(days=1)).date().isoformat()
    return punch_ist.date().isoformat()


def format_record(doc: dict) -> dict:
    history = []
    for h in doc.get("history") or []:
        changes = {}
        for field, val in (h.get("changes") or {}).items():
            if field in ("punch_in", "punch_out"):
                changes[field] = {
                    "from": dt_to_ms_nullable(val.get("from")),
                    "to": dt_to_ms_nullable(val.get("to")),
                }
            else:
                changes[field] = val
        history.append({
            "at": dt_to_ms(_aware(h["at"])),
            "by": h["by"],
            "reason": h["reason"],
            "changes": changes,
        })
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms_nullable(doc.get("punch_in")),
        "punch_out": dt_to_ms_nullable(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }


def format_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(_aware(doc["created_at"])),
    }


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
EMP_CODE_RE = re.compile(r"^EMP\d{4,6}$")
HH_MM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class EmployeeCreate(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def val_emp_code(cls, v):
        if not EMP_CODE_RE.match(v):
            raise ValueError("emp_code must match EMP followed by 4-6 digits")
        return v

    @field_validator("name")
    @classmethod
    def val_name(cls, v):
        if not v or len(v) > 100:
            raise ValueError("name 1-100 chars")
        return v

    @field_validator("email")
    @classmethod
    def val_email(cls, v):
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v) or len(v) > 120:
            raise ValueError("invalid email")
        return v

    @field_validator("department")
    @classmethod
    def val_dept(cls, v):
        if not v or len(v) > 50:
            raise ValueError("department 1-50 chars")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def val_shift(cls, v):
        if not HH_MM_RE.match(v):
            raise ValueError("shift must be HH:MM")
        return v

    @field_validator("joined_on")
    @classmethod
    def val_joined(cls, v):
        if not DATE_RE.match(v):
            raise ValueError("joined_on must be YYYY-MM-DD")
        return v

    @model_validator(mode="after")
    def shift_ne(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[Any] = None
    status: str = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def val_ts(cls, v):
        if v is None:
            return v
        return validate_ms(v, "punched_at")

    @field_validator("status")
    @classmethod
    def val_status(cls, v):
        if v not in ("PRESENT", "WFH", "ON_DUTY"):
            raise ValueError("status must be PRESENT, WFH or ON_DUTY")
        return v


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[Any] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def val_ts(cls, v):
        if v is None:
            return v
        return validate_ms(v, "punched_at")


class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[Any] = None
    punch_out: Optional[Any] = None
    reason: str
    regularized_by: str

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def val_ts(cls, v):
        if v is None:
            return v
        return validate_ms(v, "punch_in/punch_out")

    @field_validator("status")
    @classmethod
    def val_status(cls, v):
        if v is not None and v not in ("PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"):
            raise ValueError("invalid status")
        return v

    @field_validator("reason")
    @classmethod
    def val_reason(cls, v):
        if len(v) < 5 or len(v) > 200:
            raise ValueError("reason 5-200 chars")
        return v

    @field_validator("regularized_by")
    @classmethod
    def val_by(cls, v):
        if not v or len(v) > 50:
            raise ValueError("regularized_by 1-50 chars")
        return v


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, detail="MongoDB unavailable")


# ---------------------------------------------------------------------------
# Employees
# ---------------------------------------------------------------------------
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(tz=timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="emp_code already exists")
    doc.pop("_id", None)
    return format_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items": [format_employee(e) for e in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# Punch-in
# ---------------------------------------------------------------------------
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    if body.punched_at is not None:
        ts_utc = ms_to_dt(body.punched_at)
    else:
        ts_utc = datetime.now(tz=timezone.utc).replace(microsecond=0)

    att_date = attendance_date_ist(ts_utc, emp["shift_start"], emp["shift_end"])
    late = compute_late_minutes(ts_utc, emp["shift_start"])

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": ts_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="Already punched in for this date")
    doc.pop("_id", None)
    return format_record(doc)


# ---------------------------------------------------------------------------
# Attendance list
# ---------------------------------------------------------------------------
@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, detail="date_from must be <= date_to")

    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    docs = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items": [format_record(d) for d in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# Punch-out
# ---------------------------------------------------------------------------
@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    if body.punched_at is not None:
        ts_utc = ms_to_dt(body.punched_at)
    else:
        ts_utc = datetime.now(tz=timezone.utc).replace(microsecond=0)

    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": ts_utc},
            "punch_out": None,
            "status": {"$in": ["PRESENT", "WFH", "ON_DUTY"]},
        },
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        closed = db.attendance_logs.find_one(
            {
                "emp_code": body.emp_code,
                "punch_in": {"$lte": ts_utc},
                "punch_out": {"$ne": None},
                "status": {"$in": ["PRESENT", "WFH", "ON_DUTY"]},
            },
            sort=[("punch_in", DESCENDING)],
        )
        if closed:
            raise HTTPException(409, detail="Already punched out")
        raise HTTPException(404, detail="No open punch-in found")

    punch_in_utc = _aware(record["punch_in"])

    if ts_utc <= punch_in_utc:
        raise HTTPException(422, detail="punched_at must be after punch_in")
    if (ts_utc - punch_in_utc).total_seconds() > 86400:
        raise HTTPException(422, detail="punch_out more than 24 hours after punch_in")

    wh = compute_work_hours(punch_in_utc, ts_utc)
    ot = compute_overtime(ts_utc, emp["shift_end"], emp["shift_start"], record["date"])
    hd = wh < 4.50

    result = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": ts_utc, "work_hours": wh, "overtime_minutes": ot, "half_day": hd}},
        return_document=True,
    )
    if result is None:
        raise HTTPException(409, detail="Already punched out")
    result.pop("_id", None)
    return format_record(result)


# ---------------------------------------------------------------------------
# Regularize
# ---------------------------------------------------------------------------
@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeRequest):
    if not DATE_RE.match(date):
        raise HTTPException(422, detail="date must be YYYY-MM-DD")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, detail="Attendance record not found")

    cur_pi = _aware(record.get("punch_in"))
    cur_po = _aware(record.get("punch_out"))

    new_status = body.status if body.status is not None else record["status"]
    new_pi = ms_to_dt(body.punch_in) if body.punch_in is not None else cur_pi
    new_po = ms_to_dt(body.punch_out) if body.punch_out is not None else cur_po

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, detail="Cannot supply punch times with ABSENT or LEAVE")
        new_pi = None
        new_po = None
    else:
        if new_pi is None:
            raise HTTPException(422, detail="Presence status requires punch_in")
        pi_ist_date = new_pi.astimezone(IST).date().isoformat()
        if pi_ist_date != date:
            raise HTTPException(422, detail="punch_in must be on the attendance date")
        if new_po is not None:
            if new_po <= new_pi:
                raise HTTPException(422, detail="punch_out must be after punch_in")
            if (new_po - new_pi).total_seconds() > 86400:
                raise HTTPException(422, detail="punch_out more than 24 hours after punch_in")

    if new_status in ("ABSENT", "LEAVE"):
        new_late, new_wh, new_ot, new_hd = 0, None, 0, False
    else:
        new_late = compute_late_minutes(new_pi, emp["shift_start"])
        if new_po is not None:
            new_wh = compute_work_hours(new_pi, new_po)
            new_ot = compute_overtime(new_po, emp["shift_end"], emp["shift_start"], date)
            new_hd = new_wh < 4.50
        else:
            new_wh, new_ot, new_hd = None, 0, False

    old_status = record["status"]
    old_wh = record.get("work_hours")
    old_late = record.get("late_minutes", 0)
    old_ot = record.get("overtime_minutes", 0)
    old_hd = record.get("half_day", False)

    changes = {}
    if new_status != old_status:
        changes["status"] = {"from": old_status, "to": new_status}
    if new_pi != cur_pi:
        changes["punch_in"] = {"from": cur_pi, "to": new_pi}
    if new_po != cur_po:
        changes["punch_out"] = {"from": cur_po, "to": new_po}
    if new_wh != old_wh:
        changes["work_hours"] = {"from": old_wh, "to": new_wh}
    if new_late != old_late:
        changes["late_minutes"] = {"from": old_late, "to": new_late}
    if new_ot != old_ot:
        changes["overtime_minutes"] = {"from": old_ot, "to": new_ot}
    if new_hd != old_hd:
        changes["half_day"] = {"from": old_hd, "to": new_hd}

    if not changes:
        raise HTTPException(422, detail="No fields changed")

    now_utc = datetime.now(tz=timezone.utc)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    current_history_len = len(record.get("history") or [])
    result = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, current_history_len]},
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_pi,
                "punch_out": new_po,
                "work_hours": new_wh,
                "late_minutes": new_late,
                "overtime_minutes": new_ot,
                "half_day": new_hd,
            },
            "$push": {"history": history_entry},
        },
        return_document=True,
    )
    if result is None:
        raise HTTPException(409, detail="Concurrent modification detected, please retry")
    result.pop("_id", None)
    return format_record(result)


# ---------------------------------------------------------------------------
# Analytics helpers
# ---------------------------------------------------------------------------
def month_bounds(month: str):
    """Return (first_day_str, last_day_str) for a YYYY-MM string."""
    year, mon = int(month[:4]), int(month[5:])
    first = datetime(year, mon, 1, tzinfo=IST).date().isoformat()
    if mon == 12:
        last = datetime(year + 1, 1, 1, tzinfo=IST).date()
    else:
        last = datetime(year, mon + 1, 1, tzinfo=IST).date()
    last = (last - timedelta(days=1)).isoformat()
    return first, last


def working_days_in_range(start_str: str, end_str: str, joined_on: str) -> int:
    """Count Mon-Fri days between max(start, joined_on) and end inclusive."""
    from datetime import date
    s = max(date.fromisoformat(start_str), date.fromisoformat(joined_on))
    e = date.fromisoformat(end_str)
    if s > e:
        return 0
    count = 0
    cur = s
    while cur <= e:
        if cur.weekday() < 5:  # Mon=0 .. Fri=4
            count += 1
        cur += timedelta(days=1)
    return count


def validate_month(month: str):
    if not MONTH_RE.match(month):
        raise HTTPException(422, detail="month must be YYYY-MM")


# ---------------------------------------------------------------------------
# Employee monthly analytics
# ---------------------------------------------------------------------------
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(...)):
    validate_month(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    first, last = month_bounds(month)
    working_days = working_days_in_range(first, last, emp["joined_on"])

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {
                "$cond": [
                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1.0]},
                    0.0,
                ]
            }},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]

    rows = list(db.attendance_logs.aggregate(pipeline))
    if rows:
        r = rows[0]
        present_days = half_up(r["present_days"], 2)
        leave_days = int(r["leave_days"])
        late_count = int(r["late_count"])
        total_late = int(r["total_late_minutes"])
        total_ot = int(r["total_overtime_minutes"])
    else:
        present_days = 0.0
        leave_days = late_count = total_late = total_ot = 0

    att_pct = None
    if working_days > 0:
        att_pct = half_up(present_days / working_days * 100, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": att_pct,
    }


# ---------------------------------------------------------------------------
# Department summary
# ---------------------------------------------------------------------------
@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(...), department: Optional[str] = None):
    validate_month(month)
    first, last = month_bounds(month)

    emp_match = {"joined_on": {"$lte": last}}
    if department:
        emp_match["department"] = department

    pipeline = [
        {"$match": emp_match},
        # Lookup attendance logs for this month
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"ec": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {
                    "$and": [
                        {"$eq": ["$emp_code", "$$ec"]},
                        {"$gte": ["$date", first]},
                        {"$lte": ["$date", last]},
                    ]
                }}}
            ],
            "as": "logs",
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": {
                "$sum": {
                    "$map": {
                        "input": "$logs",
                        "as": "l",
                        "in": {"$cond": [
                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": [{"$ifNull": ["$$l.half_day", False]}, True]}, 0.5, 1.0]},
                            0.0,
                        ]}
                    }
                }
            }},
            "work_hours_list": {"$push": {
                "$filter": {
                    "input": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [
                                {"$and": [
                                    {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$gt": [{"$ifNull": ["$$l.work_hours", None]}, None]},
                                ]},
                                "$$l.work_hours",
                                None,
                            ]}
                        }
                    },
                    "as": "wh",
                    "cond": {"$ne": ["$$wh", None]},
                }
            }},
            "late_count": {"$sum": {
                "$sum": {
                    "$map": {
                        "input": "$logs",
                        "as": "l",
                        "in": {"$cond": [{"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}, 1, 0]}
                    }
                }
            }},
            "total_late_minutes": {"$sum": {
                "$sum": {
                    "$map": {
                        "input": "$logs",
                        "as": "l",
                        "in": {"$ifNull": ["$$l.late_minutes", 0]}
                    }
                }
            }},
            "leave_count": {"$sum": {
                "$sum": {
                    "$map": {
                        "input": "$logs",
                        "as": "l",
                        "in": {"$cond": [{"$eq": ["$$l.status", "LEAVE"]}, 1, 0]}
                    }
                }
            }},
            "on_duty_count": {"$sum": {
                "$sum": {
                    "$map": {
                        "input": "$logs",
                        "as": "l",
                        "in": {"$cond": [{"$eq": ["$$l.status", "ON_DUTY"]}, 1, 0]}
                    }
                }
            }},
        }},
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"_id": 1}},
    ]

    rows = list(db.employees.aggregate(pipeline))
    items = []
    for r in rows:
        # Flatten work_hours_list (it's a list of lists from $push of filtered arrays)
        all_wh = []
        for sub in r.get("work_hours_list", []):
            if isinstance(sub, list):
                all_wh.extend(sub)
            elif sub is not None:
                all_wh.append(sub)

        avg_wh = None
        if all_wh:
            avg_wh = half_up(sum(all_wh) / len(all_wh), 2)

        items.append({
            "department": r["_id"],
            "headcount": int(r["headcount"]),
            "present_days": half_up(r["present_days"], 2),
            "avg_work_hours": avg_wh,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })

    return {"month": month, "items": items}


# ---------------------------------------------------------------------------
# Late leaderboard
# ---------------------------------------------------------------------------
@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(...),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    validate_month(month)
    first, last = month_bounds(month)

    pipeline = [
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
        }},
        # Join employee info
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp",
        }},
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline += [
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        # Window function: dense rank -> standard competition rank
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {
                "rank": {
                    "$rank": {},
                }
            },
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
    ]

    rows = list(db.attendance_logs.aggregate(pipeline))
    items = []
    for r in rows:
        items.append({
            "rank": int(r["rank"]),
            "emp_code": r["_id"],
            "name": r["emp"]["name"],
            "department": r["emp"]["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        })

    return {"month": month, "items": items}


# ---------------------------------------------------------------------------
# Department trend
# ---------------------------------------------------------------------------
@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
):
    if not DATE_RE.match(from_) or not DATE_RE.match(to):
        raise HTTPException(422, detail="from and to must be YYYY-MM-DD")
    if to < from_:
        raise HTTPException(422, detail="to must be >= from")

    from datetime import date as date_type
    d_from = date_type.fromisoformat(from_)
    d_to = date_type.fromisoformat(to)
    if (d_to - d_from).days > 91:
        raise HTTPException(422, detail="Range exceeds 92 days")

    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, detail="Department not found")

    # Build pipeline — group by date string, then fill gaps in Python
    pipeline = [
        {"$match": {"date": {"$gte": from_, "$lte": to}}},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "emp",
        }},
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {
                "$cond": [
                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1.0]},
                    0.0,
                ]
            }},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$sort": {"_id": 1}},
    ]

    raw_rows = {r["_id"]: r for r in db.attendance_logs.aggregate(pipeline)}

    # Fill gaps in Python — generate every calendar day in range
    all_days = []
    cur = d_from
    while cur <= d_to:
        all_days.append(cur.isoformat())
        cur += timedelta(days=1)

    raw = [
        raw_rows.get(day, {"_id": day, "present_count": 0, "late_count": 0})
        for day in all_days
    ]

    # Build headcount per day
    dept_employees = list(db.employees.find(
        {"department": department},
        {"emp_code": 1, "joined_on": 1, "_id": 0}
    ))

    def headcount_on(day_str: str) -> int:
        return sum(1 for e in dept_employees if e["joined_on"] <= day_str)

    # Build result rows with attendance_rate
    result_rows = []
    for row in raw:
        day_str = row["_id"] if isinstance(row["_id"], str) else row["_id"].strftime("%Y-%m-%d")
        d = date_type.fromisoformat(day_str)
        is_wd = d.weekday() < 5
        hc = headcount_on(day_str)
        pc = row["present_count"]
        lc = int(row["late_count"])

        if is_wd and hc > 0:
            att_rate = half_up(pc / hc, 4)
        else:
            att_rate = None

        result_rows.append({
            "date": day_str,
            "is_working_day": is_wd,
            "headcount": hc,
            "present_count": pc,
            "late_count": lc,
            "attendance_rate": att_rate,
            "_rate_for_avg": att_rate,
        })

    # Compute 7-day moving average over non-null attendance_rate values in window
    for i, row in enumerate(result_rows):
        window_rates = [
            r["_rate_for_avg"]
            for r in result_rows[max(0, i - 6): i + 1]
            if r["_rate_for_avg"] is not None
        ]
        if window_rates:
            row["moving_avg_7d"] = half_up(sum(window_rates) / len(window_rates), 4)
        else:
            row["moving_avg_7d"] = None

    # Remove internal field
    for row in result_rows:
        row.pop("_rate_for_avg", None)

    return {"department": department, "items": result_rows}


# ---------------------------------------------------------------------------
# Admin explain
# ---------------------------------------------------------------------------
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(default=None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    VALID = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in VALID:
        raise HTTPException(422, detail=f"endpoint must be one of {sorted(VALID)}")

    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, detail="date_from must be <= date_to")
        q: dict = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        skip = (page - 1) * page_size
        explained = db.attendance_logs.find(q).sort(
            [("date", DESCENDING), ("emp_code", ASCENDING)]
        ).skip(skip).limit(page_size).explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explained}

    if endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(422, detail="emp_code is required for employee_monthly")
        if not month or not MONTH_RE.match(month):
            raise HTTPException(422, detail="month is required (YYYY-MM)")
        first, last = month_bounds(month)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
            {"$group": {"_id": None, "total": {"$sum": 1}}},
        ]
        explained = db.attendance_logs.aggregate(pipeline, explain=True)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explained}

    if endpoint == "department_summary":
        if not month or not MONTH_RE.match(month):
            raise HTTPException(422, detail="month is required (YYYY-MM)")
        first, last = month_bounds(month)
        emp_match: dict = {"joined_on": {"$lte": last}}
        if department:
            emp_match["department"] = department
        pipeline = [
            {"$match": emp_match},
            {"$lookup": {
                "from": "attendance_logs",
                "let": {"ec": "$emp_code"},
                "pipeline": [
                    {"$match": {"$expr": {
                        "$and": [
                            {"$eq": ["$emp_code", "$$ec"]},
                            {"$gte": ["$date", first]},
                            {"$lte": ["$date", last]},
                        ]
                    }}}
                ],
                "as": "logs",
            }},
            {"$group": {"_id": "$department", "headcount": {"$sum": 1}}},
            {"$sort": {"_id": 1}},
        ]
        explained = db.employees.aggregate(pipeline, explain=True)
        return {"endpoint": endpoint, "collection": "employees", "explain": explained}

    if endpoint == "late_leaderboard":
        if not month or not MONTH_RE.match(month):
            raise HTTPException(422, detail="month is required (YYYY-MM)")
        first, last = month_bounds(month)
        pipeline = [
            {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},
            {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
            {"$unwind": "$emp"},
        ]
        if department:
            pipeline.append({"$match": {"emp.department": department}})
        pipeline += [
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {"$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }},
            {"$match": {"rank": {"$lte": limit}}},
        ]
        explained = db.attendance_logs.aggregate(pipeline, explain=True)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explained}

    if endpoint == "department_trend":
        if not from_:
            raise HTTPException(422, detail="from is required for department_trend")
        if not to:
            raise HTTPException(422, detail="to is required for department_trend")
        if not department:
            raise HTTPException(422, detail="department is required for department_trend")
        if to < from_:
            raise HTTPException(422, detail="to must be >= from")
        pipeline = [
            {"$match": {"date": {"$gte": from_, "$lte": to}}},
            {"$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }},
            {"$unwind": "$emp"},
            {"$match": {"emp.department": department}},
            {"$group": {"_id": "$date", "present_count": {"$sum": 1}}},
            {"$sort": {"_id": 1}},
        ]
        explained = db.attendance_logs.aggregate(pipeline, explain=True)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explained}
