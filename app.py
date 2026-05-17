import os
import base64
from calendar import monthrange
from collections import Counter
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import date, datetime, timedelta, timezone
from functools import wraps
import json
import re
import secrets
import threading
import time
import traceback
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from zoneinfo import ZoneInfo

from flask import Flask, flash, g, has_request_context, jsonify, redirect, render_template, request, session, url_for
import httpx
from sqlalchemy import func, inspect, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.pool import NullPool
from werkzeug.middleware.proxy_fix import ProxyFix
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
try:
    from supabase import ClientOptions, create_client
except ImportError:
    ClientOptions = None
    create_client = None
try:
    from pywebpush import WebPushException, webpush
except ImportError:
    WebPushException = Exception
    webpush = None

from ml_service import build_health_assessment, build_weekly_wellness_trend
from models import (
    Appointment,
    CycleLog,
    LifestyleLog,
    Medication,
    MedicationLog,
    MentalLog,
    PushNotificationLog,
    User,
    UserProfile,
    WebPushSubscription,
    db,
)
from security_utils import (
    decrypt_text,
    encryption_available,
    encrypt_text,
    hash_password,
    validate_strong_password,
    verify_password,
)

def load_local_env():
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_local_env()

push_scheduler_lock = threading.Lock()
push_scheduler_started = False
runtime_init_lock = threading.Lock()
runtime_init_started = False
runtime_init_complete = False
try:
    APP_TIMEZONE = ZoneInfo(os.getenv("APP_TIMEZONE", "Asia/Manila"))
except Exception:
    APP_TIMEZONE = None

STATIC_ASSET_VERSION = os.getenv("STATIC_ASSET_VERSION", "20260508-push-test-subscription")


def app_now():
    if APP_TIMEZONE is None:
        return datetime.now()
    return datetime.now(APP_TIMEZONE).replace(tzinfo=None)


def app_today():
    return app_now().date()


def base64url_encode(raw_bytes):
    return base64.urlsafe_b64encode(raw_bytes).rstrip(b"=").decode("ascii")


def derive_vapid_public_key(private_key):
    public_key = private_key.public_key()
    public_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    return base64url_encode(public_bytes)


def ensure_vapid_config(app):
    cached_public_key = app.config.get("HORMONACARE_VAPID_PUBLIC_KEY")
    cached_private_key = app.config.get("HORMONACARE_VAPID_PRIVATE_KEY")
    if cached_public_key and cached_private_key:
        return {
            "enabled": webpush is not None,
            "public_key": cached_public_key,
            "private_key": cached_private_key,
            "subject": app.config.get("HORMONACARE_VAPID_SUBJECT"),
        }

    public_key_env = (os.getenv("VAPID_PUBLIC_KEY") or "").strip()
    private_key_env = (os.getenv("VAPID_PRIVATE_KEY") or "").strip()
    subject = (os.getenv("VAPID_SUBJECT") or "mailto:admin@hormonacare.local").strip()
    os.makedirs(app.instance_path, exist_ok=True)

    if public_key_env and private_key_env:
        app.config["HORMONACARE_VAPID_PUBLIC_KEY"] = public_key_env
        app.config["HORMONACARE_VAPID_PRIVATE_KEY"] = private_key_env
        app.config["HORMONACARE_VAPID_SUBJECT"] = subject
        return {
            "enabled": webpush is not None,
            "public_key": public_key_env,
            "private_key": private_key_env,
            "subject": subject,
        }

    private_key_path = (os.getenv("VAPID_PRIVATE_KEY_FILE") or "").strip()
    if not private_key_path:
        private_key_path = os.path.join(app.instance_path, "vapid_private_key.pem")

    if os.path.exists(private_key_path):
        with open(private_key_path, "rb") as key_file:
            private_key = serialization.load_pem_private_key(key_file.read(), password=None)
    else:
        private_key = ec.generate_private_key(ec.SECP256R1())
        private_bytes = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
        with open(private_key_path, "wb") as key_file:
            key_file.write(private_bytes)

    public_key = public_key_env or derive_vapid_public_key(private_key)
    app.config["HORMONACARE_VAPID_PUBLIC_KEY"] = public_key
    app.config["HORMONACARE_VAPID_PRIVATE_KEY"] = private_key_path
    app.config["HORMONACARE_VAPID_SUBJECT"] = subject
    return {
        "enabled": webpush is not None,
        "public_key": public_key,
        "private_key": private_key_path,
        "subject": subject,
    }


def build_subscription_info(subscription):
    return {
        "endpoint": subscription.endpoint,
        "keys": {
            "p256dh": subscription.p256dh,
            "auth": subscription.auth,
        },
    }


def push_endpoint_headers(endpoint):
    hostname = urlparse(endpoint or "").hostname or ""
    if hostname.endswith("notify.windows.com"):
        return {"X-WNS-Type": "wns/raw"}
    return {}


def send_web_push_message(app, subscription, payload, ttl_seconds=3600):
    if webpush is None:
        return False

    vapid_config = ensure_vapid_config(app)
    if not vapid_config["public_key"] or not vapid_config["private_key"]:
        return False

    try:
        webpush(
            subscription_info=build_subscription_info(subscription),
            data=json.dumps(payload, ensure_ascii=True),
            vapid_private_key=vapid_config["private_key"],
            vapid_claims={"sub": vapid_config["subject"]},
            headers=push_endpoint_headers(subscription.endpoint),
            ttl=ttl_seconds,
            timeout=10,
        )
        return True
    except WebPushException as error:
        response = getattr(error, "response", None)
        status_code = getattr(response, "status_code", None)
        if status_code in {404, 410}:
            subscription.is_active = False
            db.session.commit()
        return False
    except Exception:
        return False


def user_allows_push_category(user, category=None):
    profile = UserProfile.query.filter_by(user_id=user.id).first()
    if not profile:
        return True
    return profile.general_notifications is not False


def claim_push_delivery(user_id, notification_key, notification_type):
    delivery = PushNotificationLog(
        user_id=user_id,
        notification_key=notification_key,
        notification_type=notification_type,
    )
    db.session.add(delivery)
    try:
        db.session.commit()
        return delivery
    except IntegrityError:
        db.session.rollback()
        return None


def release_push_delivery(delivery):
    if not delivery:
        return
    db.session.delete(delivery)
    db.session.commit()


def dispatch_user_push(app, user_id, payload, ttl_seconds=3600):
    subscriptions = WebPushSubscription.query.filter_by(user_id=user_id, is_active=True).all()
    if not subscriptions:
        return 0

    sent_count = 0
    for subscription in subscriptions:
        if send_web_push_message(app, subscription, payload, ttl_seconds=ttl_seconds):
            sent_count += 1
    return sent_count


def unpack_appointment_meta_for_push(notes_blob):
    default_meta = {
        "specialty": "General Checkup",
        "location": "Clinic location",
        "reminder_enabled": False,
        "status": "scheduled",
        "notes_text": "",
    }
    notes_blob = decrypt_text(notes_blob)
    if not notes_blob or not notes_blob.startswith("__META__"):
        return default_meta
    try:
        parsed = json.loads(notes_blob.replace("__META__", "", 1))
        return {**default_meta, **parsed}
    except json.JSONDecodeError:
        return default_meta


def format_push_time(time_value):
    if not time_value:
        return ""
    return datetime.combine(date.today(), time_value).strftime("%I:%M %p").lstrip("0")


def build_push_payload(title, body, tag, url, notification_type):
    return {
        "title": title,
        "body": body,
        "tag": tag,
        "url": url,
        "type": notification_type,
        "requireInteraction": True,
        "icon": "/static/icons/icon-192.png",
        "badge": "/static/icons/icon-192.png",
    }


MEDICATION_EVENT_STATUSES = {"taken", "skipped", "missed"}
MEDICATION_DISPLAY_META = {
    "pending": {
        "label": "Pending",
        "tone": "neutral",
        "message": "Scheduled later today.",
    },
    "due": {
        "label": "Needs confirmation",
        "tone": "info",
        "message": "Confirm whether this medication was taken, skipped, or missed.",
    },
    "overdue": {
        "label": "Needs confirmation",
        "tone": "warning",
        "message": "This scheduled medication has not been confirmed yet.",
    },
    "unconfirmed": {
        "label": "Needs confirmation",
        "tone": "warning",
        "message": "Confirm whether this medication was taken, skipped, or missed.",
    },
    "unconfirmed_missed": {
        "label": "Unconfirmed missed",
        "tone": "warning",
        "message": "This was not confirmed by the cutoff. Please confirm what happened.",
    },
    "taken": {
        "label": "Taken on time",
        "tone": "success",
        "message": "Logged as taken today.",
    },
    "skipped": {
        "label": "Skipped",
        "tone": "muted",
        "message": "Logged as intentionally skipped today.",
    },
    "missed": {
        "label": "Confirmed missed",
        "tone": "danger",
        "message": "You confirmed this medication was missed.",
    },
}


def positive_int_env(name, default):
    try:
        value = int(os.getenv(name, str(default)))
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


def medication_due_window_minutes():
    return positive_int_env("MEDICATION_DUE_WINDOW_MINUTES", 15)


def medication_followup_delay_seconds():
    return positive_int_env("MEDICATION_FOLLOWUP_DELAY_SECONDS", 900)


def medication_missed_notification_window_seconds():
    scheduler_interval = positive_int_env("PUSH_SCHEDULER_INTERVAL_SECONDS", 15)
    return positive_int_env("MEDICATION_MISSED_NOTIFICATION_WINDOW_SECONDS", max(300, scheduler_interval + 60))


def medication_missed_cutoff_time_string():
    raw_value = (os.getenv("MEDICATION_MISSED_CUTOFF_TIME") or "23:59").strip()
    match = re.match(r"^(\d{1,2}):(\d{2})$", raw_value)
    if not match:
        return "23:59"
    hours = max(0, min(23, int(match.group(1))))
    minutes = max(0, min(59, int(match.group(2))))
    return f"{hours:02d}:{minutes:02d}"


def medication_day_window(target_day=None):
    day = target_day or app_today()
    day_start = datetime.combine(day, datetime.min.time())
    return day_start, day_start + timedelta(days=1)


def medication_missed_cutoff_at(target_day=None):
    day = target_day or app_today()
    cutoff_time = datetime.strptime(medication_missed_cutoff_time_string(), "%H:%M").time()
    return datetime.combine(day, cutoff_time)


def medication_scheduled_at(medication, target_day=None):
    if not getattr(medication, "time_of_day", None):
        return None
    day = target_day or app_today()
    return datetime.combine(day, medication.time_of_day)


def medication_log_status(log_entry):
    raw_status = (getattr(log_entry, "status", None) or "taken").strip().lower()
    return raw_status if raw_status in MEDICATION_EVENT_STATUSES else "taken"


def medication_status_meta(status):
    return MEDICATION_DISPLAY_META.get(status, MEDICATION_DISPLAY_META["pending"])


def medication_log_display_label(status):
    return medication_status_meta(status)["label"]


def medication_log_is_late(log_entry):
    if medication_log_status(log_entry) != "taken" or not log_entry.scheduled_time or not log_entry.taken_at:
        return False
    scheduled_at = datetime.combine(log_entry.taken_at.date(), log_entry.scheduled_time)
    return log_entry.taken_at > scheduled_at + timedelta(minutes=medication_due_window_minutes())


def medication_log_display_meta(log_entry):
    event_status = medication_log_status(log_entry)
    meta = dict(medication_status_meta(event_status))
    if event_status == "taken":
        if medication_log_is_late(log_entry):
            meta.update(
                {
                    "label": "Logged late",
                    "tone": "warning",
                    "message": "Confirmed as taken after the scheduled time.",
                }
            )
        else:
            meta.update({"label": "Taken on time"})
    return meta


def query_medication_day_logs(user_id, medication_ids, target_day=None):
    medication_ids = [medication_id for medication_id in medication_ids if medication_id]
    if not medication_ids:
        return {}

    day_start, day_end = medication_day_window(target_day)
    logs = (
        MedicationLog.query.filter(
            MedicationLog.user_id == user_id,
            MedicationLog.medication_id.in_(medication_ids),
            MedicationLog.taken_at >= day_start,
            MedicationLog.taken_at < day_end,
        )
        .order_by(MedicationLog.taken_at.asc(), MedicationLog.id.asc())
        .all()
    )
    latest_by_medication_id = {}
    for log_entry in logs:
        latest_by_medication_id[log_entry.medication_id] = log_entry
    return latest_by_medication_id


def create_medication_event_log(user, medication, status, event_time=None):
    event_status = status if status in MEDICATION_EVENT_STATUSES else "taken"
    return MedicationLog(
        user_id=user.id,
        medication_id=medication.id,
        medication_name=medication.name,
        dosage=medication.dosage,
        scheduled_time=medication.time_of_day,
        notes=medication.notes,
        status=event_status,
        taken_at=event_time or app_now(),
    )


def replace_medication_day_event(user, medication, status=None, event_time=None):
    day_start, day_end = medication_day_window()
    MedicationLog.query.filter(
        MedicationLog.user_id == user.id,
        MedicationLog.medication_id == medication.id,
        MedicationLog.taken_at >= day_start,
        MedicationLog.taken_at < day_end,
    ).delete(synchronize_session=False)

    if status in MEDICATION_EVENT_STATUSES:
        db.session.add(create_medication_event_log(user, medication, status, event_time=event_time))
        medication.status = status
    else:
        medication.status = "pending"


def compute_medication_daily_state(medication, daily_log=None, now=None, target_day=None):
    current_time = now or app_now()
    day = target_day or current_time.date()

    if daily_log:
        status = medication_log_status(daily_log)
        meta = medication_log_display_meta(daily_log)
    elif day < current_time.date():
        status = "unconfirmed_missed"
        meta = medication_status_meta(status)
    else:
        scheduled_at = medication_scheduled_at(medication, day)
        if not scheduled_at:
            status = "pending"
            meta = {
                "label": "Needs time",
                "tone": "neutral",
                "message": "Add a scheduled time to track this medication.",
            }
        elif current_time < scheduled_at:
            status = "pending"
        elif current_time >= medication_missed_cutoff_at(day):
            status = "unconfirmed_missed"
        else:
            status = "unconfirmed"
        meta = medication_status_meta(status)

    return {
        "status": status,
        "label": meta["label"],
        "tone": meta["tone"],
        "message": meta["message"],
        "event_status": medication_log_status(daily_log) if daily_log else None,
        "logged_at": daily_log.taken_at if daily_log else None,
        "has_daily_log": bool(daily_log),
    }


def annotate_medication_daily_state(medication, state):
    medication.daily_status = state["status"]
    medication.daily_status_label = state["label"]
    medication.daily_status_tone = state["tone"]
    medication.daily_status_message = state["message"]
    medication.daily_event_status = state["event_status"]
    medication.daily_logged_at = state["logged_at"]
    medication.has_daily_log = state["has_daily_log"]
    return medication


def build_medication_daily_summary(user, medications=None, target_day=None, auto_mark_missed=True):
    medication_records = list(medications) if medications is not None else Medication.query.filter_by(user_id=user.id).order_by(Medication.time_of_day.asc()).all()
    day = target_day or app_today()
    now = app_now()
    logs_by_medication_id = query_medication_day_logs(user.id, [medication.id for medication in medication_records], day)
    changed = False

    for medication in medication_records:
        daily_log = logs_by_medication_id.get(medication.id)
        state = compute_medication_daily_state(medication, daily_log=daily_log, now=now, target_day=day)
        annotate_medication_daily_state(medication, state)
        legacy_status = state["event_status"] or "pending"
        if medication.status != legacy_status:
            medication.status = legacy_status
            changed = True

    if changed:
        db.session.commit()

    counts = Counter(getattr(medication, "daily_status", "pending") for medication in medication_records)
    confirmed_count = counts.get("taken", 0) + counts.get("skipped", 0) + counts.get("missed", 0)
    unconfirmed_count = counts.get("unconfirmed", 0) + counts.get("unconfirmed_missed", 0) + counts.get("due", 0) + counts.get("overdue", 0)
    return {
        "medications": medication_records,
        "taken_count": counts.get("taken", 0),
        "skipped_count": counts.get("skipped", 0),
        "missed_count": counts.get("missed", 0),
        "unconfirmed_count": unconfirmed_count,
        "confirmed_count": confirmed_count,
        "pending_count": counts.get("pending", 0) + unconfirmed_count,
        "total_count": len(medication_records),
    }


def medication_state_payload(medication):
    return {
        "id": medication.id,
        "name": medication.name,
        "dosage": medication.dosage,
        "time_of_day": medication.time_of_day.strftime("%H:%M:%S") if medication.time_of_day else None,
        "status": medication.status,
        "daily_status": getattr(medication, "daily_status", medication.status or "pending"),
        "daily_status_label": getattr(medication, "daily_status_label", medication_log_display_label(medication.status or "pending")),
        "daily_status_tone": getattr(medication, "daily_status_tone", "neutral"),
        "daily_status_message": getattr(medication, "daily_status_message", ""),
        "event_status": getattr(medication, "daily_event_status", None),
        "reminder_enabled": bool(medication.reminder_enabled),
    }


def annotate_medication_log_display(log_entry):
    event_status = medication_log_status(log_entry)
    meta = medication_log_display_meta(log_entry)
    log_entry.event_status = event_status
    log_entry.event_label = meta["label"]
    log_entry.event_tone = meta["tone"]
    log_entry.event_message = meta["message"]
    return log_entry


def medication_has_day_event(user_id, medication_id, target_day=None):
    return medication_id in query_medication_day_logs(user_id, [medication_id], target_day)


def process_due_push_notifications(app):
    if not runtime_init_complete:
        return
    now = app_now()
    medication_lead_seconds = int(os.getenv("MEDICATION_PUSH_LEAD_SECONDS", "120"))
    appointment_lead_seconds = int(os.getenv("APPOINTMENT_PUSH_LEAD_SECONDS", "1800"))
    scheduler_window_seconds = positive_int_env("PUSH_SCHEDULER_INTERVAL_SECONDS", 15) + 30
    medication_followup_seconds = medication_followup_delay_seconds()
    medication_missed_window_seconds = medication_missed_notification_window_seconds()
    today = now.date()

    medications = Medication.query.filter_by(reminder_enabled=True).all()
    for medication in medications:
        if not medication.user or not medication.time_of_day:
            continue
        if not user_allows_push_category(medication.user, "medication"):
            continue
        if medication_has_day_event(medication.user_id, medication.id, today):
            continue

        scheduled_at = datetime.combine(today, medication.time_of_day)
        reminder_at = scheduled_at - timedelta(seconds=medication_lead_seconds)
        followup_at = scheduled_at + timedelta(seconds=medication_followup_seconds)
        missed_at = medication_missed_cutoff_at(today)

        if reminder_at <= now < scheduled_at:
            notification_key = f"medication:{medication.user_id}:{medication.id}:{today.isoformat()}:{medication.time_of_day.isoformat()}"
            delivery = claim_push_delivery(medication.user_id, notification_key, "medication_reminder")
            if delivery:
                body = f"{medication.name} is scheduled at {format_push_time(medication.time_of_day)}."
                if medication.dosage:
                    body = f"{medication.name} ({medication.dosage}) is scheduled at {format_push_time(medication.time_of_day)}."
                sent_count = dispatch_user_push(
                    app,
                    medication.user_id,
                    build_push_payload("Medication Reminder", body, notification_key, "/medications", "medication_reminder"),
                    ttl_seconds=medication_lead_seconds + 300,
                )
                if sent_count == 0:
                    release_push_delivery(delivery)

        if followup_at <= now < followup_at + timedelta(seconds=scheduler_window_seconds):
            notification_key = f"medication-followup:{medication.user_id}:{medication.id}:{today.isoformat()}:{medication.time_of_day.isoformat()}"
            delivery = claim_push_delivery(medication.user_id, notification_key, "medication_followup")
            if delivery:
                body = (
                    f"{medication.name} has not been logged yet. "
                    "Follow your care instructions or contact your provider if unsure."
                )
                sent_count = dispatch_user_push(
                    app,
                    medication.user_id,
                    build_push_payload("Medication Check-in", body, notification_key, "/medications", "medication_followup"),
                    ttl_seconds=max(medication_followup_seconds, 300),
                )
                if sent_count == 0:
                    release_push_delivery(delivery)

        if scheduled_at <= now and missed_at <= now < missed_at + timedelta(seconds=medication_missed_window_seconds):
            notification_key = f"medication-missed:{medication.user_id}:{medication.id}:{today.isoformat()}:{medication.time_of_day.isoformat()}"
            delivery = claim_push_delivery(medication.user_id, notification_key, "medication_missed")
            if delivery:
                body = (
                    f"{medication.name} was not confirmed by today's cutoff. "
                    "Please confirm whether it was taken, skipped, or missed."
                )
                sent_count = dispatch_user_push(
                    app,
                    medication.user_id,
                    build_push_payload("Medication Needs Confirmation", body, notification_key, "/medications", "medication_missed"),
                    ttl_seconds=max(medication_missed_window_seconds, 300),
                )
                if sent_count == 0:
                    release_push_delivery(delivery)

    appointments = Appointment.query.filter_by(appointment_date=today).all()
    for appointment in appointments:
        if not appointment.user or not appointment.appointment_time:
            continue
        meta = unpack_appointment_meta_for_push(appointment.notes)
        if not meta.get("reminder_enabled") or meta.get("status") != "scheduled":
            continue
        if not user_allows_push_category(appointment.user, "appointment"):
            continue
        scheduled_at = datetime.combine(today, appointment.appointment_time)
        reminder_at = scheduled_at - timedelta(seconds=appointment_lead_seconds)
        if not (reminder_at <= now < scheduled_at):
            continue

        notification_key = f"appointment:{appointment.user_id}:{appointment.id}:{today.isoformat()}:{appointment.appointment_time.isoformat()}"
        delivery = claim_push_delivery(appointment.user_id, notification_key, "appointment_reminder")
        if not delivery:
            continue

        body = f"Appointment with {appointment.doctor_name} at {format_push_time(appointment.appointment_time)}."
        sent_count = dispatch_user_push(
            app,
            appointment.user_id,
            build_push_payload("Appointment Reminder", body, notification_key, "/appointments", "scheduled_task"),
            ttl_seconds=appointment_lead_seconds + 600,
        )
        if sent_count == 0:
            release_push_delivery(delivery)


def push_scheduler_loop(app):
    interval_seconds = int(os.getenv("PUSH_SCHEDULER_INTERVAL_SECONDS", "15"))
    while True:
        try:
            with app.app_context():
                try:
                    process_due_push_notifications(app)
                finally:
                    db.session.remove()
        except Exception:
            app.logger.exception("Push notification scheduler failed.")
        finally:
            time.sleep(max(interval_seconds, 15))


def start_push_notification_scheduler(app):
    global push_scheduler_started
    if os.getenv("DISABLE_PUSH_SCHEDULER") == "1":
        return
    with push_scheduler_lock:
        if push_scheduler_started:
            return
        scheduler_thread = threading.Thread(target=push_scheduler_loop, args=(app,), daemon=True)
        scheduler_thread.start()
        push_scheduler_started = True


def normalize_database_url(database_url):
    database_url = (database_url or "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is required. Set it to your Supabase Postgres connection string.")

    if database_url.startswith("postgres://"):
        database_url = "postgresql://" + database_url[len("postgres://") :]

    parsed_url = urlparse(database_url)
    if parsed_url.scheme != "postgresql":
        raise RuntimeError("DATABASE_URL must be a PostgreSQL/Supabase URL for this project.")

    query_params = dict(parse_qsl(parsed_url.query, keep_blank_values=True))
    if "supabase.co" in (parsed_url.hostname or "") and not query_params.get("sslmode"):
        query_params["sslmode"] = "require"

    return urlunparse(parsed_url._replace(query=urlencode(query_params)))


def normalize_public_base_url(base_url):
    base_url = (base_url or "").strip()
    if not base_url:
        return ""
    if "://" not in base_url:
        base_url = f"https://{base_url.lstrip('/')}"
    return base_url.rstrip("/")


def resolve_database_uri(app):
    try:
        return normalize_database_url(os.getenv("DATABASE_URL"))
    except RuntimeError as error:
        os.makedirs(app.instance_path, exist_ok=True)
        app.config["HORMONACARE_DATABASE_WARNING"] = str(error)
        fallback_path = os.path.join(app.instance_path, "hormonacare.db")
        return "sqlite:///" + fallback_path.replace("\\", "/")


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-key-change-me")
    app.config["SQLALCHEMY_DATABASE_URI"] = resolve_database_uri(app)
    if app.config["SQLALCHEMY_DATABASE_URI"].startswith(("postgresql://", "postgresql+psycopg2://")):
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
            "poolclass": NullPool,
            "pool_pre_ping": True,
        }
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = os.getenv("FLASK_ENV") == "production"
    app.config["PREFERRED_URL_SCHEME"] = "https" if os.getenv("FLASK_ENV") == "production" else "http"

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

    db.init_app(app)

    if os.getenv("ASYNC_RUNTIME_INIT") == "1":
        threading.Thread(target=initialize_runtime, args=(app,), daemon=True).start()
    else:
        initialize_runtime(app)

    register_routes(app)
    if runtime_init_complete and (os.getenv("WERKZEUG_RUN_MAIN") == "true" or os.getenv("FLASK_ENV") == "production" or os.getenv("RENDER")):
        start_push_notification_scheduler(app)
    return app


def initialize_runtime(app):
    global runtime_init_started, runtime_init_complete
    with runtime_init_lock:
        if runtime_init_started:
            return
        runtime_init_started = True

    try:
        with app.app_context():
            db.create_all()
            ensure_runtime_schema()
            ensure_vapid_config(app)
        runtime_init_complete = True
        app.logger.info("Runtime database and notification setup complete.")
    except Exception:
        app.logger.exception("Runtime database and notification setup failed.")


def ensure_runtime_schema():
    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())
    if "users" in tables:
        columns = {column["name"] for column in inspector.get_columns("users")}
        with db.engine.begin() as connection:
            if "username" not in columns:
                connection.execute(text("ALTER TABLE users ADD COLUMN username VARCHAR(80)"))
                columns.add("username")
            if "supabase_user_id" not in columns:
                connection.execute(text("ALTER TABLE users ADD COLUMN supabase_user_id VARCHAR(80)"))
                columns.add("supabase_user_id")
            if "email_verified" not in columns:
                connection.execute(text("ALTER TABLE users ADD COLUMN email_verified BOOLEAN"))
                connection.execute(text("UPDATE users SET email_verified = TRUE WHERE email_verified IS NULL"))
                columns.add("email_verified")
            if "email_verified_at" not in columns:
                connection.execute(text("ALTER TABLE users ADD COLUMN email_verified_at TIMESTAMP"))
                columns.add("email_verified_at")
            connection.execute(
                text(
                    "UPDATE users "
                    "SET username = lower('user' || CAST(id AS VARCHAR) || '@example.local') "
                    "WHERE username IS NULL OR username = ''"
                )
            )
            connection.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(username)"))
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_supabase_user_id "
                    "ON users(supabase_user_id)"
                )
            )

            expected_columns = {
                "id",
                "full_name",
                "username",
                "password_hash",
                "supabase_user_id",
                "email_verified",
                "email_verified_at",
                "created_at",
            }
            preparer = db.engine.dialect.identifier_preparer
            for column in inspector.get_columns("users"):
                column_name = column["name"]
                if column_name in expected_columns or column.get("nullable", True):
                    continue
                if db.engine.dialect.name == "postgresql":
                    connection.execute(
                        text(f"ALTER TABLE users ALTER COLUMN {preparer.quote(column_name)} DROP NOT NULL")
                    )
    user_owned_tables = {
        "medications": {
            "add_column": "ALTER TABLE medications ADD COLUMN user_id INTEGER",
            "index": "CREATE INDEX IF NOT EXISTS idx_medications_user_id ON medications(user_id)",
        },
        "medication_logs": {
            "add_column": "ALTER TABLE medication_logs ADD COLUMN user_id INTEGER",
            "index": "CREATE INDEX IF NOT EXISTS idx_medication_logs_user_id ON medication_logs(user_id)",
        },
        "lifestyle_logs": {
            "add_column": "ALTER TABLE lifestyle_logs ADD COLUMN user_id INTEGER",
            "index": "CREATE INDEX IF NOT EXISTS idx_lifestyle_logs_user_id ON lifestyle_logs(user_id)",
        },
        "mental_logs": {
            "add_column": "ALTER TABLE mental_logs ADD COLUMN user_id INTEGER",
            "index": "CREATE INDEX IF NOT EXISTS idx_mental_logs_user_id ON mental_logs(user_id)",
        },
        "cycle_logs": {
            "add_column": "ALTER TABLE cycle_logs ADD COLUMN user_id INTEGER",
            "index": "CREATE INDEX IF NOT EXISTS idx_cycle_logs_user_id ON cycle_logs(user_id)",
        },
        "appointments": {
            "add_column": "ALTER TABLE appointments ADD COLUMN user_id INTEGER",
            "index": "CREATE INDEX IF NOT EXISTS idx_appointments_user_id ON appointments(user_id)",
        },
        "user_profiles": {
            "add_column": "ALTER TABLE user_profiles ADD COLUMN user_id INTEGER",
            "index": "CREATE UNIQUE INDEX IF NOT EXISTS idx_user_profiles_user_id ON user_profiles(user_id)",
        },
    }
    for table_name, statements in user_owned_tables.items():
        if table_name not in tables:
            continue
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        with db.engine.begin() as connection:
            if "user_id" not in columns:
                connection.execute(text(statements["add_column"]))
            connection.execute(text(statements["index"]))
    offline_sync_tables = {
        "medications",
        "medication_logs",
        "lifestyle_logs",
        "mental_logs",
        "cycle_logs",
        "appointments",
    }
    for table_name in offline_sync_tables:
        if table_name not in tables:
            continue
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        with db.engine.begin() as connection:
            if "client_sync_id" not in columns:
                connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN client_sync_id VARCHAR(80)"))
            if "client_updated_at" not in columns:
                connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN client_updated_at TIMESTAMP"))
            connection.execute(
                text(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_client_sync_id ON {table_name}(client_sync_id)")
            )
    if "user_profiles" in tables:
        columns = {column["name"] for column in inspector.get_columns("user_profiles")}
        required_columns = {
            "age": "ALTER TABLE user_profiles ADD COLUMN age INTEGER",
            "diagnosis_date": "ALTER TABLE user_profiles ADD COLUMN diagnosis_date DATE",
            "general_notifications": "ALTER TABLE user_profiles ADD COLUMN general_notifications BOOLEAN",
            "notify_meds": "ALTER TABLE user_profiles ADD COLUMN notify_meds BOOLEAN",
            "notify_appointments": "ALTER TABLE user_profiles ADD COLUMN notify_appointments BOOLEAN",
            "notify_alerts": "ALTER TABLE user_profiles ADD COLUMN notify_alerts BOOLEAN",
        }
        missing = [ddl for name, ddl in required_columns.items() if name not in columns]
        if missing:
            with db.engine.begin() as connection:
                for ddl in missing:
                    connection.execute(text(ddl))
        with db.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE user_profiles "
                    "SET general_notifications = COALESCE(general_notifications, TRUE), "
                    "notify_meds = COALESCE(notify_meds, TRUE), "
                    "notify_appointments = COALESCE(notify_appointments, TRUE), "
                    "notify_alerts = COALESCE(notify_alerts, TRUE)"
                )
            )
    if "cycle_logs" in tables:
        columns = {column["name"] for column in inspector.get_columns("cycle_logs")}
        required_columns = {
            "period_start": "ALTER TABLE cycle_logs ADD COLUMN period_start BOOLEAN DEFAULT FALSE",
        }
        missing = [ddl for name, ddl in required_columns.items() if name not in columns]
        if missing:
            with db.engine.begin() as connection:
                for ddl in missing:
                    connection.execute(text(ddl))
    if "medication_logs" in tables:
        columns = {column["name"] for column in inspector.get_columns("medication_logs")}
        required_columns = {
            "status": "ALTER TABLE medication_logs ADD COLUMN status VARCHAR(20)",
        }
        missing = [ddl for name, ddl in required_columns.items() if name not in columns]
        with db.engine.begin() as connection:
            for ddl in missing:
                connection.execute(text(ddl))
            connection.execute(text("UPDATE medication_logs SET status = COALESCE(status, 'taken')"))
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_medication_logs_user_med_taken_at "
                    "ON medication_logs(user_id, medication_id, taken_at)"
                )
            )
    try:
        if "web_push_subscriptions" in tables:
            columns = {column["name"] for column in inspector.get_columns("web_push_subscriptions")}
            required_columns = {
                "user_agent": "ALTER TABLE web_push_subscriptions ADD COLUMN user_agent VARCHAR(255)",
                "is_active": "ALTER TABLE web_push_subscriptions ADD COLUMN is_active BOOLEAN",
                "created_at": "ALTER TABLE web_push_subscriptions ADD COLUMN created_at TIMESTAMP",
                "updated_at": "ALTER TABLE web_push_subscriptions ADD COLUMN updated_at TIMESTAMP",
                "last_seen_at": "ALTER TABLE web_push_subscriptions ADD COLUMN last_seen_at TIMESTAMP",
            }
            with db.engine.begin() as connection:
                for name, ddl in required_columns.items():
                    if name not in columns:
                        connection.execute(text(ddl))
                connection.execute(text("UPDATE web_push_subscriptions SET is_active = COALESCE(is_active, TRUE)"))
    except Exception:
        db.session.rollback()


def register_routes(app):
    login_attempts = {}
    api_version = "v1"
    api_gateway_name = "HormonaCare Application API Gateway"
    api_gateway_public_paths = {
        "/api/health",
        "/api/docs",
        "/api/gateway",
        "/api/auth/register",
        "/api/auth/login",
    }
    api_gateway_route_catalog = [
        {"method": "GET", "path": "/api/health", "auth_required": False, "purpose": "Service health and backend identity"},
        {"method": "GET", "path": "/api/docs", "auth_required": False, "purpose": "API capabilities and integration guide"},
        {"method": "GET", "path": "/api/gateway", "auth_required": False, "purpose": "Describe the application-level API gateway behavior"},
        {"method": "POST", "path": "/api/auth/register", "auth_required": False, "purpose": "Create an account through the shared API"},
        {"method": "POST", "path": "/api/auth/login", "auth_required": False, "purpose": "Start an authenticated API session"},
        {"method": "POST", "path": "/api/auth/logout", "auth_required": True, "purpose": "End the current API session"},
        {"method": "GET", "path": "/api/me", "auth_required": True, "purpose": "Return the authenticated user profile"},
        {"method": "GET", "path": "/api/dashboard", "auth_required": True, "purpose": "Return overview data for dashboard clients"},
        {"method": "GET", "path": "/api/profile", "auth_required": True, "purpose": "Return profile summary data"},
        {"method": "GET", "path": "/api/cycle", "auth_required": True, "purpose": "Return cycle insights for a selected date"},
        {"method": "GET", "path": "/api/alerts", "auth_required": True, "purpose": "Return alert and risk indicator data"},
        {"method": "GET", "path": "/api/medications", "auth_required": True, "purpose": "Return medication records"},
        {"method": "GET", "path": "/api/lifestyle", "auth_required": True, "purpose": "Return recent lifestyle logs"},
        {"method": "GET", "path": "/api/mental-health", "auth_required": True, "purpose": "Return recent mental health logs"},
        {"method": "GET", "path": "/api/appointments", "auth_required": True, "purpose": "Return appointment records"},
        {"method": "GET", "path": "/api/ml/health-assessment", "auth_required": True, "purpose": "Return the current health assessment payload"},
        {"method": "POST", "path": "/api/sync/batch", "auth_required": True, "purpose": "Synchronize queued offline PWA changes with the Flask core service"},
        {"method": "GET", "path": "/api/notifications/config", "auth_required": True, "purpose": "Return Web Push browser configuration"},
        {"method": "POST", "path": "/api/notifications/subscribe", "auth_required": True, "purpose": "Save the current browser Web Push subscription"},
        {"method": "POST", "path": "/api/notifications/unsubscribe", "auth_required": True, "purpose": "Deactivate a browser Web Push subscription"},
        {"method": "POST", "path": "/api/notifications/test-push", "auth_required": True, "purpose": "Send a server-originated test push notification"},
    ]
    api_gateway_route_keys = {
        (route["method"], route["path"])
        for route in api_gateway_route_catalog
    }

    @app.before_request
    def ensure_push_scheduler_running():
        if runtime_init_complete:
            start_push_notification_scheduler(app)

    @app.before_request
    def api_gateway_entrypoint():
        if not request.path.startswith("/api/"):
            return None

        g.api_gateway = {
            "name": api_gateway_name,
            "version": api_version,
            "core_service": "Flask",
            "request_path": request.path,
            "request_method": request.method,
        }

        if (request.method, request.path) not in api_gateway_route_keys:
            return None

        if request.path in api_gateway_public_paths:
            return None

        user = current_user()
        if not user:
            return api_error("authentication_required", "Authentication is required for this API endpoint.", status=401)
        if not user.email_verified:
            remember_pending_verification(user.username)
            return api_error("email_verification_required", "Verify your email before using this API.", status=403)
        g.api_user = user
        return None

    @app.after_request
    def apply_security_headers(response):
        vary_header = response.headers.get("Vary", "")
        vary_values = [value.strip().lower() for value in vary_header.split(",") if value.strip()]
        if "cookie" not in vary_values:
            response.headers["Vary"] = f"{vary_header}, Cookie".strip(", ") if vary_header else "Cookie"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        response.headers["Cache-Control"] = "no-store" if session.get("user_id") else "no-cache"
        if session.get("user_id"):
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        if request.path == "/static/service-worker.js":
            response.headers["Service-Worker-Allowed"] = "/"
        if os.getenv("FLASK_ENV") == "production":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                "font-src 'self' https://fonts.gstatic.com data:; "
                "img-src 'self' data:; "
                "script-src 'self' 'unsafe-inline'; "
                "connect-src 'self'; "
                "frame-ancestors 'self'; "
                "base-uri 'self'; "
                "form-action 'self'"
            )
        if request.path.startswith("/api/"):
            response.headers["X-Core-Service"] = "HormonaCare Core Service"
            response.headers["X-API-Gateway"] = api_gateway_name
            response.headers["X-API-Version"] = api_version
        return response

    def current_user():
        user_id = session.get("user_id")
        if not user_id:
            return None
        user = db.session.get(User, user_id)
        if user is None:
            session.clear()
            return None
        return user

    def user_records(model, user):
        return model.query.filter_by(user_id=user.id)

    def owned_record_or_404(model, user, record_id):
        return user_records(model, user).filter_by(id=record_id).first_or_404()

    def request_medication_summary(user):
        cache = getattr(g, "medication_summary_cache", None) if has_request_context() else None
        if cache and cache.get("user_id") == user.id:
            return cache
        try:
            medications = Medication.query.filter_by(user_id=user.id).order_by(Medication.time_of_day.asc()).all()
            summary = build_medication_daily_summary(user, medications)
        except SQLAlchemyError:
            db.session.rollback()
            app.logger.exception("Medication summary unavailable while database schema is preparing.")
            medications = []
            summary = {
                "medications": [],
                "taken_count": 0,
                "skipped_count": 0,
                "missed_count": 0,
                "unconfirmed_count": 0,
                "confirmed_count": 0,
                "pending_count": 0,
                "total_count": 0,
            }
        cache = {"user_id": user.id, "medications": medications, "summary": summary}
        if has_request_context():
            g.medication_summary_cache = cache
        return cache

    full_name_help = "Enter your full name using 2 to 120 characters."
    email_help = "Enter a valid email address."
    email_taken = "That email is already registered."

    def normalize_full_name(value):
        return re.sub(r"\s+", " ", (value or "").strip())

    def normalize_email(value):
        return (value or "").strip().lower()

    def valid_full_name(value):
        normalized_value = normalize_full_name(value)
        if len(normalized_value) < 2 or len(normalized_value) > 120:
            return False
        return sum(character.isalpha() for character in normalized_value) >= 2

    def valid_email(value):
        normalized_value = normalize_email(value)
        email_pattern = (
            r"(?=.{3,80}$)[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
            r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
        )
        return bool(re.fullmatch(email_pattern, normalized_value))

    def find_user_by_auth_identifier(value):
        normalized_value = normalize_email(value)
        if not normalized_value:
            return None
        return User.query.filter(func.lower(User.username) == normalized_value).first()

    def login_required(view):
        @wraps(view)
        def wrapped_view(*args, **kwargs):
            user = current_user()
            if not user:
                flash("Please log in to continue.", "warning")
                return redirect(url_for("login"))
            if not user.email_verified:
                remember_pending_verification(user.username)
                flash("Please verify your email before continuing.", "warning")
                return redirect(url_for("verify_email"))
            return view(*args, **kwargs)

        return wrapped_view

    def api_login_required(view):
        @wraps(view)
        def wrapped_view(*args, **kwargs):
            user = current_user()
            if not user:
                return api_error("authentication_required", "Authentication is required for this API endpoint.", status=401)
            if not user.email_verified:
                remember_pending_verification(user.username)
                return api_error("email_verification_required", "Verify your email before using this API.", status=403)
            return view(*args, **kwargs)

        return wrapped_view

    def api_response(payload, status=200):
        response = jsonify(payload)
        response.status_code = status
        return response

    def api_success(data=None, message=None, status=200, meta=None):
        payload = {"ok": True}
        if message:
            payload["message"] = message
        if data is not None:
            payload["data"] = data
        if meta is not None:
            payload["meta"] = meta
        return api_response(payload, status=status)

    def api_error(error_code, message, status=400, details=None):
        payload = {"ok": False, "error": error_code, "message": message}
        if details is not None:
            payload["details"] = details
        return api_response(payload, status=status)

    def api_json_body():
        if not request.is_json:
            return None
        return request.get_json(silent=True) or {}

    def serialize_auth_user(user):
        profile = get_or_create_profile(user)
        return {
            "id": user.id,
            "full_name": user.full_name,
            "username": user.username,
            "email": user.username,
            "email_verified": safe_bool(user.email_verified),
            "dark_mode": safe_bool(profile.dark_mode),
        }

    def build_api_docs():
        return {
            "service": "HormonaCare Core Service",
            "gateway": api_gateway_name,
            "backend_language": "Python",
            "api_version": api_version,
            "auth_mode": "session_cookie",
            "description": "Shared Flask API layer for the current web interface and future mobile clients.",
            "gateway_scope": "Application-level gateway inside Flask; not a separate AWS API Gateway service.",
            "core_function": "Provide one Python backend so multiple clients can reuse the same data, business logic, and wellness services.",
            "endpoints": api_gateway_route_catalog,
        }

    @app.context_processor
    def inject_globals():
        user = current_user()
        unread_reminders = 0
        dark_mode_enabled = False
        notification_medication_schedules = []

        def notification_opt_in(value, default=True):
            return default if value is None else bool(value)

        notification_preferences = {
            "general": True,
            "medications": True,
            "appointments": True,
            "alerts": True,
        }
        if user:
            medication_cache = request_medication_summary(user)
            medications = medication_cache["medications"]
            medication_summary = medication_cache["summary"]
            unread_reminders = medication_summary["pending_count"]
            notification_medication_schedules = [medication_state_payload(medication) for medication in medications]
            profile = UserProfile.query.filter_by(user_id=user.id).first()
            dark_mode_enabled = bool(profile and profile.dark_mode)
            if profile:
                notification_preferences = {
                    "general": notification_opt_in(profile.general_notifications),
                    "medications": True,
                    "appointments": True,
                    "alerts": True,
                }
        return {
            "current_user": user,
            "today_date": date.today(),
            "unread_reminders": unread_reminders,
            "dark_mode_enabled": dark_mode_enabled,
            "field_encryption_enabled": encryption_available(),
            "notification_preferences": notification_preferences,
            "notification_medication_schedules": notification_medication_schedules,
            "notification_medication_followup_delay_ms": medication_followup_delay_seconds() * 1000,
            "notification_medication_missed_cutoff_time": medication_missed_cutoff_time_string(),
            "static_asset_version": STATIC_ASSET_VERSION,
        }

    def parse_float(value, default=0):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def parse_int(value, default=0):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def parse_profile_age(value):
        raw_value = (value or "").strip()
        if not raw_value:
            return None, None
        if not re.fullmatch(r"\d{1,3}", raw_value):
            return None, "Age must be a whole number."
        age = int(raw_value)
        if age < 1 or age > 120:
            return None, "Age must be between 1 and 120."
        return age, None

    def split_list_text(text):
        return [item.strip() for item in (text or "").split(",") if item.strip()]

    def build_pcos_state(profile):
        return {
            "status": "",
            "symptoms": [],
            "has_symptoms": False,
            "has_info": False,
            "has_irregular_periods": False,
            "has_mood_swings": False,
            "has_weight_gain": False,
            "metabolic_focus": False,
            "has_fatigue": False,
            "adherence_focus": False,
            "support_message": "Your insights are based on your logged cycle, lifestyle, and mood data.",
        }

    def iso_date(value):
        return value.isoformat() if value else None

    def display_date_label(value, fallback=""):
        if not value:
            return fallback
        if hasattr(value, "strftime"):
            return value.strftime("%b %d, %Y")
        try:
            return datetime.fromisoformat(str(value)).strftime("%b %d, %Y")
        except (TypeError, ValueError):
            return str(value)

    def iso_time(value):
        return value.strftime("%H:%M:%S") if value else None

    def safe_bool(value):
        return bool(value)

    def get_or_create_profile(user):
        profile = UserProfile.query.filter_by(user_id=user.id).first()
        if profile:
            return profile
        profile = UserProfile(
            user_id=user.id,
            general_notifications=True,
            notify_meds=True,
            notify_appointments=True,
            notify_alerts=True,
        )
        db.session.add(profile)
        db.session.commit()
        return profile

    def validate_password_strength(password):
        return validate_strong_password(password)

    def build_auth_context(form_type, values=None, errors=None, **extra):
        context = {
            "auth_form_type": form_type,
            "form_values": values or {},
            "form_errors": errors or {},
        }
        context.update(extra)
        return context

    def supabase_public_key():
        return (os.getenv("SUPABASE_ANON_KEY") or os.getenv("SUPABASE_PUBLISHABLE_KEY") or "").strip()

    def supabase_auth_ready():
        return bool((os.getenv("SUPABASE_URL") or "").strip() and supabase_public_key())

    def get_supabase_client():
        if create_client is None:
            raise RuntimeError("Supabase client is not installed yet. Run pip install -r requirements.txt.")
        url = (os.getenv("SUPABASE_URL") or "").strip()
        key = supabase_public_key()
        if not url or not key:
            raise RuntimeError("Supabase auth is not configured. Set SUPABASE_URL and SUPABASE_ANON_KEY.")
        httpx_client = httpx.Client(
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=30.0),
            follow_redirects=True,
            trust_env=False,
        )
        options = ClientOptions(
            auto_refresh_token=False,
            persist_session=False,
            httpx_client=httpx_client,
        )
        return create_client(url, key, options=options)

    def redirect_authenticated_user():
        user = current_user()
        if not user:
            return None
        if not user.email_verified:
            remember_pending_verification(user.username)
            flash("Please verify your email before continuing.", "warning")
            return redirect(url_for("verify_email"))
        return redirect(url_for("dashboard"))

    def clear_pending_verification():
        for key in (
            "pending_verification_email",
            "pending_verification_type",
            "pending_new_account",
            "otp_request_state",
            "otp_email",
        ):
            session.pop(key, None)

    def remember_pending_verification(email, verification_type="signup", new_account=False):
        normalized_email = normalize_email(email)
        clear_authenticated_session()
        session["pending_verification_email"] = normalized_email
        session["pending_verification_type"] = verification_type
        session["otp_email"] = normalized_email
        if new_account:
            session["pending_new_account"] = True
        else:
            session.pop("pending_new_account", None)

    def pending_verification_email():
        return normalize_email(session.get("pending_verification_email") or session.get("otp_email"))

    def clear_authenticated_session():
        for key in ("user_id", "login_at", "new_account", "settings_password_verified"):
            session.pop(key, None)

    def start_authenticated_session(user, *, new_account=False):
        clear_pending_verification()
        clear_authenticated_session()
        session.permanent = True
        session["user_id"] = user.id
        session["login_at"] = datetime.utcnow().isoformat()
        if new_account:
            session["new_account"] = True

    def build_external_url(endpoint, **values):
        configured_base_url = normalize_public_base_url(os.getenv("PUBLIC_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL"))
        if configured_base_url:
            relative_path = url_for(endpoint, _external=False, **values)
            return urljoin(f"{configured_base_url}/", relative_path.lstrip("/"))
        if has_request_context():
            return url_for(endpoint, _external=True, **values)
        return url_for(
            endpoint,
            _external=True,
            _scheme=app.config["PREFERRED_URL_SCHEME"],
            **values,
        )

    def supabase_reset_redirect_url():
        return build_external_url("reset_password")

    def supabase_verify_redirect_url():
        return build_external_url("verify_email")

    def error_message_text(error):
        return str(error or "").strip()

    def error_message_lower(error):
        return error_message_text(error).lower()

    def is_timeout_error(error):
        lowered = error_message_lower(error)
        return isinstance(error, (httpx.TimeoutException, TimeoutError, FutureTimeoutError)) or "timed out" in lowered or "timeout" in lowered

    def is_rate_limited_error(error):
        lowered = error_message_lower(error)
        return (
            "rate limit" in lowered
            or "too many requests" in lowered
            or "429" in lowered
            or "security purposes" in lowered
            or "can only request" in lowered
        )

    def rate_limited_wait_seconds(error):
        match = re.search(r"after\s+(\d+)\s+seconds?", error_message_lower(error))
        return int(match.group(1)) if match else None

    def otp_cooldown_message(error):
        wait_seconds = rate_limited_wait_seconds(error)
        if wait_seconds:
            return f"Please wait {wait_seconds} seconds before requesting another OTP."
        return "Please wait a minute before requesting another OTP."

    def is_network_error(error):
        lowered = error_message_lower(error)
        return "network" in lowered or "connection" in lowered or "connect" in lowered

    def is_invalid_login_error(error):
        lowered = error_message_lower(error)
        return "invalid login credentials" in lowered or "invalid credentials" in lowered or "invalid grant" in lowered

    def is_unverified_email_error(error):
        lowered = error_message_lower(error)
        return "email not confirmed" in lowered or "email not verified" in lowered

    def is_user_already_registered_error(error):
        lowered = error_message_lower(error)
        return "user already registered" in lowered or "already registered" in lowered

    def is_invalid_otp_error(error):
        lowered = error_message_lower(error)
        token_keywords = ("otp", "token", "code")
        invalid_keywords = ("invalid", "expired", "not found", "mismatch")
        return any(keyword in lowered for keyword in token_keywords) and any(keyword in lowered for keyword in invalid_keywords)

    def friendly_supabase_error(error, fallback):
        message = error_message_text(error)
        if not message or "object at 0x" in message:
            return fallback
        lowered = message.lower()
        if is_timeout_error(error) or is_network_error(error):
            return "Service temporarily unavailable. Please try again later."
        if is_user_already_registered_error(error):
            return "That email is already connected to Supabase Auth."
        if is_rate_limited_error(error):
            return otp_cooldown_message(error)
        if is_unverified_email_error(error):
            return "Please verify your email before logging in."
        if is_invalid_login_error(error):
            return "Invalid email or password."
        if is_invalid_otp_error(error):
            return "Invalid OTP."
        return message

    def response_auth_user(response):
        user = getattr(response, "user", None)
        if user:
            return user
        session_data = getattr(response, "session", None)
        return getattr(session_data, "user", None) if session_data else None

    def response_auth_session(response):
        return getattr(response, "session", None)

    def auth_session_value(session_data, field_name):
        if not session_data:
            return None
        if isinstance(session_data, dict):
            return session_data.get(field_name)
        return getattr(session_data, field_name, None)

    def response_user_email(response):
        user = response_auth_user(response)
        if isinstance(user, dict):
            return (user.get("email") or "").strip().lower()
        return (getattr(user, "email", "") or "").strip().lower()

    def supabase_user_verified_at(auth_user):
        if not auth_user:
            return None
        return getattr(auth_user, "email_confirmed_at", None) or getattr(auth_user, "confirmed_at", None)

    def supabase_user_is_verified(auth_user):
        return bool(supabase_user_verified_at(auth_user))

    def fallback_full_name(email):
        local_part = normalize_email(email).split("@")[0]
        candidate = re.sub(r"[._-]+", " ", local_part)
        words = [word.capitalize() for word in candidate.split() if word]
        return " ".join(words) or "HormonaCare User"

    def auth_user_full_name(auth_user, fallback_email=""):
        user_metadata = getattr(auth_user, "user_metadata", {}) or {}
        for key in ("full_name", "name", "display_name"):
            value = normalize_full_name(user_metadata.get(key))
            if value:
                return value
        return fallback_full_name(fallback_email or getattr(auth_user, "email", ""))

    def ensure_local_user(email, password=None, full_name="", auth_user=None):
        normalized_email = normalize_email(email)
        user = User.query.filter_by(username=normalized_email).first()
        resolved_full_name = normalize_full_name(full_name) or auth_user_full_name(auth_user, normalized_email)
        resolved_verified_at = supabase_user_verified_at(auth_user)
        resolved_supabase_user_id = (getattr(auth_user, "id", "") or "").strip() if auth_user else ""

        if not user:
            user = User(
                full_name=resolved_full_name or fallback_full_name(normalized_email),
                username=normalized_email,
                password_hash=hash_password(password or os.urandom(16).hex()),
                supabase_user_id=resolved_supabase_user_id or None,
                email_verified=bool(resolved_verified_at),
                email_verified_at=resolved_verified_at,
            )
            db.session.add(user)
            db.session.commit()
            return user

        changed = False
        if resolved_full_name and user.full_name != resolved_full_name:
            user.full_name = resolved_full_name
            changed = True
        if resolved_supabase_user_id and user.supabase_user_id != resolved_supabase_user_id:
            user.supabase_user_id = resolved_supabase_user_id
            changed = True
        if auth_user is not None:
            resolved_verified = bool(resolved_verified_at)
            if user.email_verified != resolved_verified:
                user.email_verified = resolved_verified
                changed = True
            if resolved_verified_at and user.email_verified_at != resolved_verified_at:
                user.email_verified_at = resolved_verified_at
                changed = True
        if password and not verify_password(password, user.password_hash):
            user.password_hash = hash_password(password)
            changed = True
        if changed:
            db.session.commit()
        return user

    def register_supabase_password_account(email, password, full_name):
        supabase = get_supabase_client()
        return supabase.auth.sign_up(
            {
                "email": email,
                "password": password,
                "options": {
                    "data": {"full_name": full_name},
                    "email_redirect_to": supabase_verify_redirect_url(),
                },
            }
        )

    def send_email_otp(email, should_create_user=False):
        supabase = get_supabase_client()
        supabase.auth.sign_in_with_otp(
            {
                "email": normalize_email(email),
                "options": {
                    "should_create_user": should_create_user,
                },
            }
        )
        remember_otp_request(email)

    def send_password_recovery_otp(email):
        supabase = get_supabase_client()
        supabase.auth.reset_password_for_email(
            normalize_email(email),
            {"redirect_to": supabase_reset_redirect_url()},
        )
        remember_otp_request(email)

    def resend_signup_otp(email):
        supabase = get_supabase_client()
        try:
            supabase.auth.resend(
                {
                    "type": "signup",
                    "email": email,
                    "options": {"email_redirect_to": supabase_verify_redirect_url()},
                }
            )
        except Exception as error:
            if is_rate_limited_error(error) or is_timeout_error(error) or is_network_error(error):
                raise
            send_email_otp(email, should_create_user=False)
        else:
            remember_otp_request(email)

    def otp_retry_seconds(email):
        otp_email = normalize_email(email)
        otp_state = session.get("otp_request_state") or {}
        last_email = normalize_email(otp_state.get("email"))
        last_sent_at = otp_state.get("sent_at")
        if not otp_email or otp_email != last_email or not last_sent_at:
            return 0
        try:
            elapsed = datetime.utcnow() - datetime.fromisoformat(last_sent_at)
        except ValueError:
            session.pop("otp_request_state", None)
            return 0
        cooldown_seconds = 60
        remaining = cooldown_seconds - int(elapsed.total_seconds())
        return remaining if remaining > 0 else 0

    def remember_otp_request(email):
        session["otp_request_state"] = {
            "email": normalize_email(email),
            "sent_at": datetime.utcnow().isoformat(),
        }

    def clear_password_reset_state():
        for key in ("reset_token_hash", "reset_password_email", "reset_password_verified", "local_otp_challenge"):
            session.pop(key, None)

    def remember_password_reset_verification(email, auth_session):
        access_token = auth_session_value(auth_session, "access_token")
        refresh_token = auth_session_value(auth_session, "refresh_token")
        if not access_token or not refresh_token:
            raise RuntimeError("Password reset session is incomplete.")
        session["reset_password_email"] = normalize_email(email)
        session["reset_password_verified"] = {
            "email": normalize_email(email),
            "access_token": access_token,
            "refresh_token": refresh_token,
            "verified_at": datetime.utcnow().isoformat(),
        }

    def password_reset_verification_state():
        state = session.get("reset_password_verified") or {}
        if not isinstance(state, dict):
            return {}
        return state

    def clear_settings_password_state():
        session.pop("settings_password_verified", None)
        challenge = session.get("local_otp_challenge") or {}
        if challenge.get("purpose") == "settings_password":
            session.pop("local_otp_challenge", None)

    def remember_settings_password_verification(email, auth_session, method):
        access_token = auth_session_value(auth_session, "access_token")
        refresh_token = auth_session_value(auth_session, "refresh_token")
        if not access_token or not refresh_token:
            raise RuntimeError("Password change session is incomplete.")
        session["settings_password_verified"] = {
            "email": normalize_email(email),
            "method": (method or "").strip().lower(),
            "access_token": access_token,
            "refresh_token": refresh_token,
            "verified_at": datetime.utcnow().isoformat(),
        }

    def settings_password_verification_state():
        state = session.get("settings_password_verified") or {}
        if not isinstance(state, dict):
            return {}
        verified_at = (state.get("verified_at") or "").strip()
        if verified_at:
            try:
                elapsed = datetime.utcnow() - datetime.fromisoformat(verified_at)
            except ValueError:
                clear_settings_password_state()
                return {}
            if elapsed.total_seconds() > 600:
                clear_settings_password_state()
                return {}
        return state

    def log_supabase_otp_error(email, error):
        normalized_email = normalize_email(email)
        redacted_email = normalized_email
        if "@" in normalized_email:
            local_part, domain = normalized_email.split("@", 1)
            redacted_email = f"{local_part[:2]}***@{domain}"
        print(f"[Supabase Auth Error] email={redacted_email!r} error={error_message_text(error)!r}")
        if os.getenv("AUTH_DEBUG_ERRORS") == "1":
            traceback.print_exc()

    def otp_send_error_message(error):
        if is_timeout_error(error) or is_network_error(error):
            return "Service temporarily unavailable. Please try again later."
        if is_rate_limited_error(error):
            return otp_cooldown_message(error)
        if is_unverified_email_error(error):
            return "Please verify your email before logging in."
        return "OTP sending failed. Please try again after 60 seconds."

    def password_reset_error_message(error, fallback):
        if is_timeout_error(error) or is_network_error(error):
            return "Service temporarily unavailable. Please try again later."
        if is_rate_limited_error(error):
            return "Too many reset requests. Please wait a minute before trying again."
        return friendly_supabase_error(error, fallback)

    def local_auth_fallback_enabled():
        configured = (os.getenv("ALLOW_LOCAL_AUTH_FALLBACK") or "").strip().lower()
        if configured in {"1", "true", "yes", "on"}:
            return True
        return False

    def can_use_local_auth_fallback(error=None):
        if not local_auth_fallback_enabled():
            return False
        if error is None:
            return True
        lowered = error_message_lower(error)
        return (
            is_timeout_error(error)
            or is_network_error(error)
            or "not configured" in lowered
            or "not installed" in lowered
        )

    def local_auth_status_message():
        return "Auth email service is unavailable. Using local demo recovery for now."

    def remember_local_otp_challenge(email, purpose):
        code = f"{secrets.randbelow(1000000):06d}"
        session["local_otp_challenge"] = {
            "email": normalize_email(email),
            "purpose": purpose,
            "code_hash": hash_password(code),
            "expires_at": (datetime.utcnow() + timedelta(minutes=10)).isoformat(),
            "attempts": 0,
        }
        return code

    def verify_local_otp_challenge(email, purpose, otp):
        challenge = session.get("local_otp_challenge") or {}
        if (
            normalize_email(challenge.get("email")) != normalize_email(email)
            or challenge.get("purpose") != purpose
        ):
            return None, None
        try:
            expires_at = datetime.fromisoformat(challenge.get("expires_at") or "")
        except ValueError:
            session.pop("local_otp_challenge", None)
            return False, "Invalid or expired code."
        if datetime.utcnow() > expires_at:
            session.pop("local_otp_challenge", None)
            return False, "Invalid or expired code."
        if not verify_password(otp, challenge.get("code_hash")):
            challenge["attempts"] = int(challenge.get("attempts") or 0) + 1
            if challenge["attempts"] >= 5:
                session.pop("local_otp_challenge", None)
            else:
                session["local_otp_challenge"] = challenge
            return False, "Invalid or expired code."
        session.pop("local_otp_challenge", None)
        return True, None

    def remember_local_password_reset_verification(email):
        session["reset_password_email"] = normalize_email(email)
        session["reset_password_verified"] = {
            "email": normalize_email(email),
            "method": "local_otp",
            "verified_at": datetime.utcnow().isoformat(),
        }

    def remember_local_settings_password_verification(email, method):
        session["settings_password_verified"] = {
            "email": normalize_email(email),
            "method": method,
            "verified_at": datetime.utcnow().isoformat(),
        }

    def check_login_rate_limit(username):
        now = datetime.utcnow()
        entry = login_attempts.get(username)
        if not entry:
            return None
        if entry["locked_until"] and now < entry["locked_until"]:
            remaining = int((entry["locked_until"] - now).total_seconds())
            return f"Too many login attempts. Try again in {remaining} seconds."
        if entry["locked_until"] and now >= entry["locked_until"]:
            login_attempts.pop(username, None)
        return None

    def record_failed_login(username):
        now = datetime.utcnow()
        entry = login_attempts.get(username, {"count": 0, "locked_until": None})
        entry["count"] += 1
        if entry["count"] >= 5:
            entry["locked_until"] = now + timedelta(minutes=5)
        login_attempts[username] = entry

    def clear_failed_login(username):
        login_attempts.pop(username, None)

    def get_or_create_today_lifestyle_log(user):
        today = app_today()
        log = LifestyleLog.query.filter_by(user_id=user.id, log_date=today).first()
        if log:
            return log

        log = LifestyleLog(
            user_id=user.id,
            log_date=today,
            sleep_hours=0,
            water_intake_liters=0,
            diet_quality="Not logged",
            exercise_minutes=0,
            notes="",
        )
        db.session.add(log)
        return log

    LIFESTYLE_EXERCISE_PREFIX = "__EXERCISE__"
    LIFESTYLE_FOOD_PREFIX = "__FOOD__"

    # Simple PCOS-friendly rule checks for exercise logs. These are wellness prompts,
    # not medical diagnoses.
    def evaluate_exercise_entry(activity_type, duration_minutes, intensity):
        raw_input = {
            "activity_type": (activity_type or "").strip(),
            "duration_minutes": duration_minutes,
            "intensity": (intensity or "").strip().lower(),
        }
        errors = []
        valid_intensities = {"light", "moderate", "intense"}

        if not raw_input["activity_type"]:
            errors.append("Please enter an activity type.")
        if not isinstance(duration_minutes, int) or duration_minutes < 0 or duration_minutes > 600:
            errors.append("Duration must be a whole number between 0 and 600 minutes.")
        if raw_input["intensity"] not in valid_intensities:
            errors.append("Select a valid intensity: light, moderate, or intense.")

        if errors:
            return {"ok": False, "raw_input": raw_input, "errors": errors}

        if duration_minutes < 20 or raw_input["intensity"] == "light":
            classification = "Low Activity"
            feedback = "More movement may help support insulin response, energy, and routine balance in PCOS."
            tone = "warning"
        elif 20 <= duration_minutes <= 45 and raw_input["intensity"] == "moderate":
            classification = "PCOS-Supportive Activity"
            feedback = "Nice work. This level of movement can support blood sugar balance and daily PCOS management."
            tone = "success"
        else:
            classification = "High Activity"
            feedback = "Strong activity day. Keep recovery, meals, and hydration steady so the routine stays supportive for PCOS."
            tone = "info"

        return {
            "ok": True,
            "entry": {
                "raw_input": raw_input,
                "classification": classification,
                "feedback": feedback,
                "tone": tone,
                "logged_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
            },
        }

    # Rule-based meal feedback that stays easy to explain during presentations.
    def evaluate_food_entry(food_name, portion_size, food_category):
        raw_input = {
            "food_name": (food_name or "").strip(),
            "portion_size": (portion_size or "").strip().lower(),
            "food_category": (food_category or "").strip().lower(),
        }
        errors = []
        valid_portions = {"small", "medium", "large"}
        valid_categories = {"high sugar", "protein-rich", "balanced", "fast food"}

        if not raw_input["food_name"]:
            errors.append("Please enter a food name.")
        if raw_input["portion_size"] not in valid_portions:
            errors.append("Select a valid portion size: small, medium, or large.")
        if raw_input["food_category"] not in valid_categories:
            errors.append("Select a valid food category.")

        if errors:
            return {"ok": False, "raw_input": raw_input, "errors": errors}

        if raw_input["food_category"] == "high sugar" and raw_input["portion_size"] == "large":
            feedback = "Large high-sugar meals may make blood sugar and insulin support feel less steady in PCOS."
            status = "warning"
            tone = "warning"
        elif raw_input["food_category"] in {"balanced", "protein-rich"}:
            feedback = "Balanced or protein-rich meals can support steadier energy and insulin balance in PCOS."
            status = "positive"
            tone = "success"
        elif raw_input["food_category"] == "fast food":
            feedback = "Frequent processed or fast-food meals may make PCOS symptom support feel less steady."
            status = "caution"
            tone = "warning"
        else:
            feedback = "Balanced portions may help support steadier energy and appetite patterns."
            status = "neutral"
            tone = "info"

        return {
            "ok": True,
            "entry": {
                "raw_input": raw_input,
                "status": status,
                "feedback": feedback,
                "tone": tone,
                "logged_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
            },
        }

    def normalize_exercise_feedback_entry(entry):
        if not isinstance(entry, dict):
            return entry
        normalized = dict(entry)
        classification = (normalized.get("classification") or "").strip().lower()
        if classification == "recommended activity":
            normalized["classification"] = "PCOS-Supportive Activity"
        feedback_map = {
            "Increase activity to support hormone balance.": "More movement may help support insulin response, energy, and routine balance in PCOS.",
            "Good job! This supports metabolic health.": "Nice work. This level of movement can support blood sugar balance and daily PCOS management.",
            "Great work! Maintain balance and avoid overexertion.": "Strong activity day. Keep recovery, meals, and hydration steady so the routine stays supportive for PCOS.",
        }
        normalized["feedback"] = feedback_map.get(normalized.get("feedback"), normalized.get("feedback"))
        return normalized

    def normalize_food_feedback_entry(entry):
        if not isinstance(entry, dict):
            return entry
        normalized = dict(entry)
        feedback_map = {
            "High sugar intake may worsen PCOS symptoms.": "Large high-sugar meals may make blood sugar and insulin support feel less steady in PCOS.",
            "Balanced meals support hormone regulation.": "Balanced or protein-rich meals can support steadier energy and insulin balance in PCOS.",
            "Consider reducing processed or high-sugar foods.": "Frequent processed or fast-food meals may make PCOS symptom support feel less steady.",
            "Keep portions balanced to better support steady energy levels.": "Balanced portions may help support steadier energy and appetite patterns.",
        }
        normalized["feedback"] = feedback_map.get(normalized.get("feedback"), normalized.get("feedback"))
        return normalized

    # Lifestyle notes keep free-form notes plus structured exercise/food JSON lines.
    def parse_lifestyle_notes(note_blob):
        parsed = {
            "general_notes": "",
            "exercise_entries": [],
            "food_entries": [],
        }
        decrypted_blob = decrypt_text(note_blob)
        if not decrypted_blob:
            return parsed

        general_lines = []
        for raw_line in decrypted_blob.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith(LIFESTYLE_EXERCISE_PREFIX):
                try:
                    payload = json.loads(line[len(LIFESTYLE_EXERCISE_PREFIX):])
                    if isinstance(payload, dict):
                        parsed["exercise_entries"].append(normalize_exercise_feedback_entry(payload))
                        continue
                except json.JSONDecodeError:
                    pass
            if line.startswith(LIFESTYLE_FOOD_PREFIX):
                try:
                    payload = json.loads(line[len(LIFESTYLE_FOOD_PREFIX):])
                    if isinstance(payload, dict):
                        parsed["food_entries"].append(normalize_food_feedback_entry(payload))
                        continue
                except json.JSONDecodeError:
                    pass
            if line.startswith("Meal: "):
                parsed["food_entries"].append(
                    normalize_food_feedback_entry(
                        {
                        "raw_input": {
                            "food_name": line.replace("Meal: ", "", 1).strip(),
                            "portion_size": "unknown",
                            "food_category": "legacy",
                        },
                        "status": "legacy",
                        "feedback": "Saved before PCOS-aware meal feedback was enabled.",
                        "tone": "info",
                        "logged_at": "",
                        }
                    )
                )
                continue
            general_lines.append(raw_line)

        parsed["general_notes"] = "\n".join(general_lines).strip()
        return parsed

    def build_lifestyle_feedback_preview(logs, entry_type, limit=3):
        """Collect newest-first preview cards without mutating the stored entry lists."""
        entries = []
        seen = set()

        for log in reversed(logs):
            parsed = parse_lifestyle_notes(log.notes)
            source_entries = parsed["exercise_entries"] if entry_type == "exercise" else parsed["food_entries"]

            for entry in reversed(source_entries):
                raw_input = entry.get("raw_input", {})
                if entry_type == "exercise":
                    activity_type = (raw_input.get("activity_type") or "").strip()
                    intensity = (raw_input.get("intensity") or "").strip().lower()
                    duration_minutes = raw_input.get("duration_minutes")
                    classification = (entry.get("classification") or "").strip()
                    feedback = (entry.get("feedback") or "").strip()
                    if not activity_type or not classification or not feedback:
                        continue
                    signature = (
                        entry_type,
                        activity_type.lower(),
                        duration_minutes,
                        intensity,
                        classification.lower(),
                        feedback.lower(),
                        entry.get("logged_at", ""),
                    )
                    if signature in seen:
                        continue
                    seen.add(signature)
                    entries.append(
                        {
                            "activity_type": activity_type,
                            "duration_minutes": duration_minutes,
                            "intensity": intensity.title(),
                            "classification": classification,
                            "feedback": feedback,
                            "logged_at": entry.get("logged_at", ""),
                            "log_date": log.log_date,
                        }
                    )
                else:
                    food_name = (raw_input.get("food_name") or "").strip()
                    portion_size = (raw_input.get("portion_size") or "").strip().lower()
                    food_category = (raw_input.get("food_category") or "").strip().lower()
                    feedback = (entry.get("feedback") or "").strip()
                    if (
                        not food_name
                        or portion_size in {"", "unknown"}
                        or food_category in {"", "legacy"}
                        or not feedback
                    ):
                        continue
                    signature = (
                        entry_type,
                        food_name.lower(),
                        portion_size,
                        food_category,
                        feedback.lower(),
                        entry.get("logged_at", ""),
                    )
                    if signature in seen:
                        continue
                    seen.add(signature)
                    entries.append(
                        {
                            "food_name": food_name,
                            "portion_size": portion_size.replace("-", " ").title(),
                            "food_category": food_category.replace("-", " ").title(),
                            "feedback": feedback,
                            "logged_at": entry.get("logged_at", ""),
                            "log_date": log.log_date,
                        }
                    )

                if limit and len(entries) >= limit:
                    return entries

        return entries

    def summarize_daily_exercise(exercise_entries, fallback_minutes=None):
        """Collapse same-day exercise entries into one daily activity summary."""
        score_map = {
            "low activity": 0.0,
            "recommended activity": 1.0,
            "pcos-supportive activity": 1.0,
            "high activity": 2.0,
        }
        scores = []

        for entry in exercise_entries or []:
            classification = (entry.get("classification") or "").strip().lower()
            if classification not in score_map:
                continue
            score = score_map[classification]
            try:
                duration_minutes = int(entry.get("raw_input", {}).get("duration_minutes") or 0)
            except (TypeError, ValueError):
                duration_minutes = 0
            if duration_minutes >= 45:
                score = min(2.0, score + 0.15)
            elif duration_minutes < 20:
                score = max(0.0, score - 0.15)
            scores.append(score)

        if scores:
            average_score = round(sum(scores) / len(scores), 2)
        else:
            try:
                fallback_value = int(fallback_minutes or 0)
            except (TypeError, ValueError):
                fallback_value = 0
            average_score = 2.0 if fallback_value > 45 else 1.0 if fallback_value >= 20 else 0.0

        if average_score < 0.75:
            label = "Low Activity"
        elif average_score < 1.6:
            label = "PCOS-Supportive Activity"
        else:
            label = "High Activity"

        return {"label": label, "score": average_score}

    def summarize_daily_food(food_entries, fallback_category=""):
        """Aggregate multiple same-day food logs into one daily meal classification."""
        category_score_map = {
            "high sugar": 0.0,
            "fast food": 0.85,
            "balanced": 2.0,
            "protein-rich": 1.85,
            "protein rich": 1.85,
        }
        portion_adjustment = {
            "small": 0.1,
            "medium": 0.0,
            "large": -0.2,
        }
        status_adjustment = {
            "warning": -0.25,
            "caution": -0.1,
            "neutral": 0.05,
            "positive": 0.15,
        }
        scores = []
        category_counts = {}

        for entry in food_entries or []:
            raw_input = entry.get("raw_input", {})
            category = (raw_input.get("food_category") or "").strip().lower()
            portion_size = (raw_input.get("portion_size") or "").strip().lower()
            status = (entry.get("status") or "").strip().lower()
            if category not in category_score_map:
                continue
            score = category_score_map[category]
            score += portion_adjustment.get(portion_size, 0.0)
            score += status_adjustment.get(status, 0.0)
            score = max(0.0, min(2.0, score))
            scores.append(score)
            category_counts[category] = category_counts.get(category, 0) + 1

        if scores:
            average_score = round(sum(scores) / len(scores), 2)
            protein_count = category_counts.get("protein-rich", 0) + category_counts.get("protein rich", 0)
            balanced_count = category_counts.get("balanced", 0)
            if average_score < 0.75:
                label = "High Sugar"
            elif average_score < 1.4:
                label = "Fast Food"
            elif protein_count > balanced_count:
                label = "Protein-Rich"
            else:
                label = "Balanced"
        else:
            normalized_fallback = (fallback_category or "").strip().lower().replace("_", " ")
            if normalized_fallback in {"protein-rich", "protein rich"}:
                label = "Protein-Rich"
                average_score = 1.85
            elif normalized_fallback == "fast food":
                label = "Fast Food"
                average_score = 0.85
            elif normalized_fallback == "high sugar":
                label = "High Sugar"
                average_score = 0.0
            else:
                label = "Balanced"
                average_score = 2.0 if normalized_fallback == "balanced" else 1.5

        return {"label": label, "score": average_score}

    def build_weekly_wellness_rows(user, days=7):
        """Build a rolling window of summarized wellness rows for trend analysis."""
        end_date = date.today()
        start_date = end_date - timedelta(days=days - 1)
        lifestyle_logs = (
            LifestyleLog.query.filter(
                LifestyleLog.user_id == user.id,
                LifestyleLog.log_date >= start_date,
                LifestyleLog.log_date <= end_date,
            )
            .order_by(LifestyleLog.log_date.asc())
            .all()
        )
        mental_logs = (
            MentalLog.query.filter(
                MentalLog.user_id == user.id,
                MentalLog.log_date >= start_date,
                MentalLog.log_date <= end_date,
            )
            .order_by(MentalLog.log_date.asc())
            .all()
        )

        lifestyle_by_date = {log.log_date: log for log in lifestyle_logs}
        mental_by_date = {log.log_date: log for log in mental_logs}
        rows = []

        for offset in range(days):
            row_date = start_date + timedelta(days=offset)
            lifestyle_log = lifestyle_by_date.get(row_date)
            mental_log = mental_by_date.get(row_date)
            parsed_notes = parse_lifestyle_notes(lifestyle_log.notes) if lifestyle_log else {
                "exercise_entries": [],
                "food_entries": [],
            }
            exercise_summary = summarize_daily_exercise(
                parsed_notes["exercise_entries"],
                fallback_minutes=lifestyle_log.exercise_minutes if lifestyle_log else 0,
            )
            food_summary = summarize_daily_food(
                parsed_notes["food_entries"],
                fallback_category=lifestyle_log.diet_quality if lifestyle_log else "",
            )
            rows.append(
                {
                    "date": row_date,
                    "mood": mental_log.mood if mental_log else "Okay",
                    "stress_level": mental_log.stress_level if mental_log else None,
                    "sleep_hours": lifestyle_log.sleep_hours if lifestyle_log else None,
                    "sleep_duration": lifestyle_log.sleep_hours if lifestyle_log else None,
                    "sleep_quality": getattr(lifestyle_log, "sleep_quality", None) if lifestyle_log else None,
                    "physical_activity": lifestyle_log.exercise_minutes if lifestyle_log else None,
                    "exercise_minutes": lifestyle_log.exercise_minutes if lifestyle_log else None,
                    "water_intake": lifestyle_log.water_intake_liters if lifestyle_log else None,
                    "heart_rate": getattr(lifestyle_log, "heart_rate", None) if lifestyle_log else None,
                    "daily_steps": getattr(lifestyle_log, "daily_steps", None) if lifestyle_log else None,
                    "food_classification": food_summary["label"],
                    "exercise_summary": exercise_summary["label"],
                    "hydration_status": (
                        "Low Hydration"
                        if lifestyle_log and lifestyle_log.water_intake_liters < 1.5
                        else "Hydration Level Supporting PCOS Management"
                        if lifestyle_log
                        else None
                    ),
                    "has_user_data": bool(lifestyle_log or mental_log),
                }
            )

        return rows

    def extract_general_notes(note_blob):
        return parse_lifestyle_notes(note_blob).get("general_notes", "")

    def serialize_lifestyle_notes(general_notes="", exercise_entries=None, food_entries=None):
        parts = []
        if general_notes.strip():
            parts.extend([line.rstrip() for line in general_notes.strip().splitlines() if line.strip()])
        for entry in exercise_entries or []:
            parts.append(LIFESTYLE_EXERCISE_PREFIX + json.dumps(entry, ensure_ascii=True, separators=(",", ":")))
        for entry in food_entries or []:
            parts.append(LIFESTYLE_FOOD_PREFIX + json.dumps(entry, ensure_ascii=True, separators=(",", ":")))
        return "\n".join(parts).strip()

    def pack_appointment_notes(specialty, location, reminder_enabled, status, notes_text):
        meta = {
            "specialty": specialty.strip(),
            "location": location.strip(),
            "reminder_enabled": bool(reminder_enabled),
            "status": status,
            "notes_text": notes_text.strip(),
        }
        return encrypt_text("__META__" + json.dumps(meta))

    def unpack_appointment_notes(notes_blob):
        default_meta = {
            "specialty": "General Checkup",
            "location": "Clinic location",
            "reminder_enabled": False,
            "status": "scheduled",
            "notes_text": notes_blob or "",
        }
        notes_blob = decrypt_text(notes_blob)
        if not notes_blob or not notes_blob.startswith("__META__"):
            return default_meta
        try:
            parsed = json.loads(notes_blob.replace("__META__", "", 1))
            return {**default_meta, **parsed}
        except json.JSONDecodeError:
            return default_meta

    def appointment_status_meta(appointment, meta):
        raw_status = (meta.get("status") or "scheduled").strip().lower()
        if raw_status == "completed":
            return {"key": "completed", "label": "Completed", "tone": "success"}
        if raw_status == "cancelled":
            return {"key": "cancelled", "label": "Cancelled", "tone": "muted"}
        if raw_status == "missed" or appointment.appointment_date < date.today():
            return {"key": "missed", "label": "Missed", "tone": "warning"}
        if appointment.appointment_date == date.today():
            return {"key": "today", "label": "Today", "tone": "info"}
        return {"key": "scheduled", "label": "Scheduled", "tone": "info"}

    def build_appointment_editor_payload(appointment, meta):
        return {
            "id": appointment.id,
            "doctor_name": appointment.doctor_name,
            "appointment_date": appointment.appointment_date.strftime("%Y-%m-%d"),
            "appointment_time": appointment.appointment_time.strftime("%H:%M"),
            "specialty": meta.get("specialty", "General Checkup"),
            "location": meta.get("location", "Clinic location"),
            "notes": meta.get("notes_text", ""),
            "prescription": appointment.prescription or "",
            "follow_up_date": appointment.follow_up_date.strftime("%Y-%m-%d") if appointment.follow_up_date else "",
            "reminder_enabled": bool(meta.get("reminder_enabled")),
        }

    def safe_appointment_display_text(value):
        text_value = (value or "").strip()
        if text_value.startswith("__ENC__"):
            return ""
        return text_value

    def empty_appointment_form_values():
        return {
            "appointment_id": "",
            "doctor_name": "",
            "appointment_date": "",
            "appointment_time": "",
            "specialty": "",
            "location": "",
            "notes": "",
            "prescription": "",
            "follow_up_date": "",
            "reminder_enabled": False,
        }

    def parse_form_date(raw_value, label, required=True):
        value = (raw_value or "").strip()
        if not value:
            if required:
                return None, f"{label} is required."
            return None, None
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return None, f"{label} must use the YYYY-MM-DD format."
        try:
            return datetime.strptime(value, "%Y-%m-%d").date(), None
        except ValueError:
            return None, f"Enter a valid {label.lower()}."

    def parse_form_time(raw_value, label, required=True):
        value = (raw_value or "").strip()
        if not value:
            if required:
                return None, f"{label} is required."
            return None, None
        if not re.fullmatch(r"\d{2}:\d{2}", value):
            return None, f"{label} must use the HH:MM format."
        try:
            return datetime.strptime(value, "%H:%M").time(), None
        except ValueError:
            return None, f"Enter a valid {label.lower()}."

    def normalize_appointment_view(raw_view):
        return raw_view if raw_view in {"overview", "upcoming", "history"} else "overview"

    def appointment_page_context(user, form_values=None, modal_open=False, modal_mode="new", view_mode="overview"):
        all_appointments = Appointment.query.filter(Appointment.user_id == user.id).order_by(Appointment.appointment_date.asc()).all()
        decorated = []
        for appointment in all_appointments:
            appointment.prescription = decrypt_text(appointment.prescription)
            meta = unpack_appointment_notes(appointment.notes)
            prescription_text = safe_appointment_display_text(appointment.prescription)
            notes_text = safe_appointment_display_text(meta.get("notes_text", ""))
            decorated.append(
                {
                    "appointment": appointment,
                    "meta": {**meta, "notes_text": notes_text},
                    "prescription_text": prescription_text,
                    "status": appointment_status_meta(appointment, meta),
                    "editor": build_appointment_editor_payload(appointment, meta),
                }
            )
        for item in decorated:
            if "status" not in item or not item["status"]:
                item["status"] = appointment_status_meta(item["appointment"], item["meta"])

        upcoming = [
            item for item in decorated
            if item["appointment"].appointment_date >= date.today()
            and item["status"]["key"] not in {"completed", "cancelled", "missed"}
        ]
        past = [
            item for item in decorated
            if item["appointment"].appointment_date < date.today()
            or item["status"]["key"] in {"completed", "cancelled", "missed"}
        ]
        past.sort(key=lambda item: item["appointment"].appointment_date, reverse=True)

        appointment_form_values = empty_appointment_form_values()
        if form_values:
            appointment_form_values.update(form_values)

        normalized_view = normalize_appointment_view(view_mode)
        upcoming_preview = upcoming[:2]
        past_preview = past[:2]
        return {
            "upcoming": upcoming,
            "past": past,
            "upcoming_preview": upcoming_preview,
            "past_preview": past_preview,
            "upcoming_hidden_count": max(0, len(upcoming) - len(upcoming_preview)),
            "past_hidden_count": max(0, len(past) - len(past_preview)),
            "appointment_view_mode": normalized_view,
            "appointment_form_values": appointment_form_values,
            "appointment_modal_open": modal_open,
            "appointment_modal_mode": modal_mode,
        }

    def pack_cycle_details(symptoms_text, notes_text):
        payload = {"symptoms": symptoms_text.strip(), "notes": notes_text.strip()}
        if not payload["symptoms"] and not payload["notes"]:
            return ""
        return encrypt_text("__CYCLE__" + json.dumps(payload))

    def unpack_cycle_details(stored_value):
        decrypted_value = decrypt_text(stored_value)
        fallback = {"symptoms": decrypted_value or "", "notes": ""}
        if not decrypted_value or not decrypted_value.startswith("__CYCLE__"):
            return fallback
        try:
            parsed = json.loads(decrypted_value.replace("__CYCLE__", "", 1))
            return {"symptoms": parsed.get("symptoms", ""), "notes": parsed.get("notes", "")}
        except json.JSONDecodeError:
            return fallback

    def decrypt_model_fields(records, field_names):
        for record in records:
            for field_name in field_names:
                raw_value = getattr(record, field_name, None)
                if isinstance(raw_value, str) and raw_value:
                    setattr(record, field_name, decrypt_text(raw_value))
        return records

    def medication_log_window(target_day=None):
        return medication_day_window(target_day)

    def normalize_medication_statuses(user, medications=None):
        if not user:
            return medications or []
        return build_medication_daily_summary(user, medications)["medications"]

    def fetch_medication_history(user, limit=None):
        query = MedicationLog.query.filter_by(user_id=user.id).order_by(MedicationLog.taken_at.desc())
        if limit:
            query = query.limit(limit)
        try:
            history_entries = query.all()
            decrypt_model_fields(history_entries, ["notes"])
        except SQLAlchemyError:
            db.session.rollback()
            app.logger.exception("Medication history unavailable while database schema is preparing.")
            return []
        for entry in history_entries:
            annotate_medication_log_display(entry)
        return history_entries

    def safe_medication_history_count(user):
        try:
            return MedicationLog.query.filter_by(user_id=user.id).count()
        except SQLAlchemyError:
            db.session.rollback()
            app.logger.exception("Medication history count unavailable while database schema is preparing.")
            return 0

    def health_assessment_for_inputs(sleep_hours, water_intake, stress_level, activity_minutes):
        return build_health_assessment(
            sleep_hours=sleep_hours,
            water_intake=water_intake,
            stress_level=stress_level,
            activity_minutes=activity_minutes,
        )

    def normalize_flow_level(flow_level):
        normalized = (flow_level or "").strip().lower()
        if normalized in {"", "select flow"}:
            return ""
        if normalized in {"no flow", "no-flow", "none"}:
            return "No Flow"
        if normalized == "moderate":
            return "Medium"
        if normalized == "medium":
            return "Medium"
        if normalized == "heavy":
            return "Heavy"
        if normalized == "light":
            return "Light"
        return ""

    def has_logged_flow(flow_level):
        return normalize_flow_level(flow_level) in {"Light", "Medium", "Heavy"}

    def is_explicit_no_flow(flow_level):
        return normalize_flow_level(flow_level) == "No Flow"

    def is_period_start_log(log):
        if not log:
            return False
        if bool(getattr(log, "period_start", False)):
            return True
        return bool(log.cycle_day == 1 and has_logged_flow(log.flow_level))

    def observed_period_lengths(sorted_logs, period_starts):
        if not period_starts:
            return []
        logs_by_date = {log.log_date: log for log in sorted_logs}
        lengths = []
        for index, start_date in enumerate(period_starts):
            start_log = logs_by_date.get(start_date)
            if not is_period_start_log(start_log):
                continue
            next_start = period_starts[index + 1] if index + 1 < len(period_starts) else None
            current_length = 1
            cursor = start_date + timedelta(days=1)
            while True:
                if next_start and cursor >= next_start:
                    break
                current_log = logs_by_date.get(cursor)
                if current_log and has_logged_flow(current_log.flow_level):
                    current_length += 1
                    cursor += timedelta(days=1)
                    continue
                break
            lengths.append(current_length)
        return lengths

    def recent_period_tracking(sorted_logs, period_starts, reference_date):
        latest_start = max((start for start in period_starts if start <= reference_date), default=None)
        if not latest_start:
            return {
                "latest_start": None,
                "latest_flow_date": None,
                "no_flow_logged": False,
                "needs_flow_log_prompt": False,
                "current_period_length": None,
            }

        relevant_logs = [
            log for log in sorted_logs
            if latest_start <= log.log_date <= reference_date
        ]
        latest_flow_date = None
        latest_no_flow_date = None
        for log in relevant_logs:
            if is_period_start_log(log) or has_logged_flow(log.flow_level):
                latest_flow_date = log.log_date
            elif is_explicit_no_flow(log.flow_level):
                latest_no_flow_date = log.log_date

        has_log_for_reference = any(log.log_date == reference_date for log in relevant_logs)
        period_still_open = bool(
            latest_flow_date and (not latest_no_flow_date or latest_no_flow_date < latest_flow_date)
        )
        needs_flow_log_prompt = bool(
            latest_flow_date
            and latest_flow_date < reference_date
            and (reference_date - latest_flow_date).days <= 2
            and period_still_open
            and not has_log_for_reference
        )
        current_period_length = (
            (latest_flow_date - latest_start).days + 1
            if latest_flow_date and period_still_open
            else None
        )
        return {
            "latest_start": latest_start,
            "latest_flow_date": latest_flow_date,
            "no_flow_logged": bool(latest_no_flow_date and latest_no_flow_date >= (latest_flow_date or latest_start)),
            "needs_flow_log_prompt": needs_flow_log_prompt,
            "current_period_length": current_period_length,
        }

    def cycle_signal_summary(log):
        details = unpack_cycle_details(log.symptoms if log else "")
        combined = f"{details['symptoms']} {details['notes']}".lower()
        lh_positive = any(term in combined for term in ["lh positive", "positive lh", "positive opk", "lh surge"])
        bbt_shift = any(term in combined for term in ["bbt rise", "temperature rise", "basal body temperature", "temp rise"])
        fertile_mucus = any(
            term in combined
            for term in ["egg white", "ewcm", "cervical mucus", "slippery mucus", "stretchy mucus"]
        )
        ovulation_pain = any(term in combined for term in ["mittelschmerz", "ovulation pain", "one-sided pain"])
        signal_count = sum([lh_positive, bbt_shift, fertile_mucus, ovulation_pain])
        confirmed = lh_positive or bbt_shift
        likely = confirmed or signal_count >= 2
        confidence = "high" if confirmed and signal_count >= 2 else "medium" if likely else "low" if signal_count == 1 else "none"
        return {
            "confirmed": confirmed,
            "likely": likely,
            "confidence": confidence,
            "signal_count": signal_count,
        }

    def average_value(values):
        if not values:
            return None
        return sum(values) / len(values)

    def population_stddev(values):
        if len(values) < 2:
            return 0.0
        average = average_value(values)
        variance = sum((value - average) ** 2 for value in values) / len(values)
        return variance ** 0.5

    def clamp_cycle_value(value, minimum=18, maximum=60):
        return max(minimum, min(maximum, int(round(value))))

    def weighted_recent_average(values):
        if not values:
            return None
        weights = list(range(1, len(values) + 1))
        weighted_total = sum(value * weight for value, weight in zip(values, weights))
        return weighted_total / sum(weights)

    def linear_cycle_projection(values):
        if not values:
            return None, 0.0
        if len(values) == 1:
            return float(values[0]), 0.0
        indexes = list(range(len(values)))
        x_average = sum(indexes) / len(indexes)
        y_average = sum(values) / len(values)
        denominator = sum((index - x_average) ** 2 for index in indexes)
        if not denominator:
            return float(values[-1]), 0.0
        slope = sum((index - x_average) * (value - y_average) for index, value in zip(indexes, values)) / denominator
        intercept = y_average - (slope * x_average)
        return intercept + (slope * len(values)), slope

    def compact_date_range(start_date, end_date):
        if not start_date or not end_date:
            return ""
        if start_date.year == end_date.year:
            if start_date.month == end_date.month:
                return f"{start_date.strftime('%b %d')}-{end_date.strftime('%d')}"
            return f"{start_date.strftime('%b %d')}-{end_date.strftime('%b %d')}"
        return f"{start_date.strftime('%b %d, %Y')}-{end_date.strftime('%b %d, %Y')}"

    def cycle_model(logs):
        sorted_logs = sorted(logs, key=lambda item: item.log_date)
        period_starts = sorted({log.log_date for log in sorted_logs if is_period_start_log(log)})
        anchors = [(log.log_date, log.cycle_day) for log in sorted_logs if log.cycle_day]
        ovulation_signals = {log.log_date: cycle_signal_summary(log) for log in sorted_logs}
        observed_lengths = []
        period_lengths = observed_period_lengths(sorted_logs, period_starts)
        for previous, current in zip(period_starts, period_starts[1:]):
            gap = (current - previous).days
            if 15 <= gap <= 90:
                observed_lengths.append(gap)

        cycle_length = None
        cycle_low = None
        cycle_high = None
        trend_slope = 0.0
        cycle_variability = 0.0
        limited_data = len(observed_lengths) < 3
        recent_lengths = observed_lengths[-min(6, len(observed_lengths)) :]
        weighted_length = weighted_recent_average(recent_lengths)
        projected_length, trend_slope = linear_cycle_projection(recent_lengths)
        if weighted_length is not None:
            blended_projection = weighted_length
            if projected_length is not None and len(recent_lengths) >= 3:
                blended_projection = (weighted_length * 0.7) + (projected_length * 0.3)
            cycle_length = clamp_cycle_value(blended_projection)
            cycle_variability = population_stddev(recent_lengths)
            prediction_margin = max(2, int(round(cycle_variability * 1.5)))
            if len(recent_lengths) < 3:
                prediction_margin = max(prediction_margin, 4)
            if abs(trend_slope) >= 1:
                prediction_margin += 1
            observed_low = min(recent_lengths)
            observed_high = max(recent_lengths)
            cycle_low = clamp_cycle_value(min(observed_low, cycle_length - prediction_margin))
            cycle_high = clamp_cycle_value(max(observed_high, cycle_length + prediction_margin))
            if cycle_low > cycle_high:
                cycle_low, cycle_high = cycle_high, cycle_low

        irregular = bool(observed_lengths) and (
            any(gap < 21 or gap > 38 for gap in recent_lengths) or cycle_variability >= 4.5
        )

        if not observed_lengths:
            pattern_summary = "Start logging your cycle to build PCOS-aware cycle insights."
            confidence_message = "Start logging your cycle to build PCOS-aware cycle insights."
        elif limited_data:
            pattern_summary = "We're still learning how your cycle behaves. PCOS cycles can take longer to map clearly, so keep logging."
            confidence_message = "Confidence is still building. Ovulation timing can be less predictable in PCOS, so more logs help."
        elif trend_slope >= 1.2:
            pattern_summary = "Your recent cycle lengths are trending slightly longer, which can happen with PCOS-related irregularity."
            confidence_message = "We have enough history to estimate a range, and we'll keep refining it as new cycle data comes in."
        elif trend_slope <= -1.2:
            pattern_summary = "Your recent cycle lengths are trending slightly shorter than your usual pattern."
            confidence_message = "We have enough history to estimate a range, and we'll keep refining it as new cycle data comes in."
        elif irregular:
            pattern_summary = "Your cycle pattern shows noticeable irregularity, which is commonly observed in PCOS."
            confidence_message = "Confidence is improving, but your recent cycle timing has been irregular and may shift further."
        elif cycle_variability <= 2.5:
            pattern_summary = "Your recent logs suggest the cycle pattern is becoming a bit more consistent."
            confidence_message = "Confidence is improving as your recent cycle pattern becomes more consistent."
        else:
            pattern_summary = "Your recent cycle pattern looks steadier, though PCOS-related shifts can still happen."
            confidence_message = "We have enough history to keep this estimate responsive to your latest logs."

        observed_signal_days = []
        for signal_date, signal in ovulation_signals.items():
            if not signal["likely"]:
                continue
            latest_start = max((start for start in period_starts if start <= signal_date), default=None)
            if not latest_start:
                continue
            signal_day = (signal_date - latest_start).days + 1
            if 1 <= signal_day <= 60:
                observed_signal_days.append(signal_day)

        typical_ovulation_day = None
        if observed_signal_days:
            recent_signal_days = observed_signal_days[-min(4, len(observed_signal_days)) :]
            typical_ovulation_day = clamp_cycle_value(
                weighted_recent_average(recent_signal_days),
                minimum=10,
                maximum=max(16, cycle_length or 24),
            )
        elif cycle_length:
            typical_ovulation_day = max(10, min(cycle_length - 10, cycle_length - 14))

        fertile_window = None
        fertile_confidence = "early"
        prediction_ready = len(period_starts) >= 2 and bool(cycle_length)
        phase_estimation_enabled = prediction_ready or bool(observed_signal_days)
        smart_mode_enabled = prediction_ready
        if typical_ovulation_day and cycle_length:
            fertile_window = (max(6, typical_ovulation_day - 5), min(cycle_length, typical_ovulation_day + 1))
            if limited_data:
                fertile_confidence = "building"
            elif irregular:
                fertile_confidence = "flexible"
            else:
                fertile_confidence = "steady"

        latest_period_start = period_starts[-1] if period_starts else None
        days_since_last_period = (date.today() - latest_period_start).days if latest_period_start else None
        average_period_length = round(average_value(period_lengths), 1) if period_lengths else None
        period_length_low = min(period_lengths) if period_lengths else None
        period_length_high = max(period_lengths) if period_lengths else None
        long_gap_threshold = (cycle_high + 14) if cycle_high else 60
        possible_anovulatory = bool(days_since_last_period and days_since_last_period >= long_gap_threshold)

        return {
            "cycle_length": cycle_length,
            "cycle_low": cycle_low,
            "cycle_high": cycle_high,
            "period_starts": period_starts,
            "period_lengths": period_lengths,
            "average_period_length": average_period_length,
            "period_length_low": period_length_low,
            "period_length_high": period_length_high,
            "anchors": anchors,
            "observed_lengths": observed_lengths,
            "ovulation_signals": ovulation_signals,
            "observed_signal_days": observed_signal_days,
            "irregular": irregular,
            "prediction_confidence": confidence_message,
            "fertile_window": fertile_window,
            "fertile_confidence": fertile_confidence,
            "smart_mode_enabled": smart_mode_enabled,
            "prediction_ready": prediction_ready,
            "phase_estimation_enabled": phase_estimation_enabled,
            "has_cycle_history": bool(period_starts),
            "latest_period_start": latest_period_start,
            "days_since_last_period": days_since_last_period,
            "possible_anovulatory": possible_anovulatory,
            "cycle_variability": cycle_variability,
            "trend_slope": trend_slope,
            "pattern_summary": pattern_summary,
            "confidence_message": confidence_message,
            "limited_data": limited_data,
            "typical_ovulation_day": typical_ovulation_day,
        }

    def logged_period_day(log, day_number=None):
        return bool(log and (is_period_start_log(log) or has_logged_flow(log.flow_level)))

    def inferred_cycle_day(target_date, model):
        period_starts = model["period_starts"]
        anchors = model.get("anchors", [])
        cycle_length = model.get("cycle_length")

        if period_starts:
            latest_start = None
            for start in period_starts:
                if start <= target_date:
                    latest_start = start
                else:
                    break
            if latest_start:
                diff = (target_date - latest_start).days
                allowed_span = (model.get("cycle_high") + 7) if model.get("cycle_high") else 90
                if 0 <= diff <= allowed_span:
                    return diff + 1, latest_start
                return None, latest_start
            return None, period_starts[0]

        if not anchors:
            return None, None

        prior_anchor = None
        for anchor_date, anchor_day in anchors:
            if anchor_date <= target_date:
                prior_anchor = (anchor_date, anchor_day)
            else:
                break
        if prior_anchor:
            anchor_start = prior_anchor[0] - timedelta(days=max(0, prior_anchor[1] - 1))
            diff = (target_date - anchor_start).days
            if diff >= 0:
                allowed_span = (model.get("cycle_high") + 7) if model.get("cycle_high") else 90
                if cycle_length and diff > allowed_span:
                    return None, anchor_start
                return diff + 1, anchor_start
        return None, None

    def latest_supported_ovulation_date(model, cycle_start, reference_date):
        if not cycle_start:
            return None
        candidates = [
            signal_date
            for signal_date, signal in model["ovulation_signals"].items()
            if cycle_start <= signal_date <= reference_date and signal["likely"]
        ]
        return max(candidates) if candidates else None

    def cycle_phase_name(target_date, day_number, cycle_start, model, log=None):
        if logged_period_day(log, day_number):
            return "Period"

        if not model.get("phase_estimation_enabled"):
            if not model.get("has_cycle_history"):
                return "Start logging your PCOS cycle"
            return "We're still learning your PCOS cycle"

        day_signal = model["ovulation_signals"].get(target_date, {"likely": False, "confidence": "none"})
        if day_signal["likely"]:
            return "Possible Ovulation"

        fertile_window = model.get("fertile_window")
        if fertile_window and day_number and fertile_window[0] <= day_number <= fertile_window[1]:
            return "Possible Fertile Window"

        ovulation_date = latest_supported_ovulation_date(model, cycle_start, target_date)
        if ovulation_date and ovulation_date < target_date:
            return "Luteal Phase"

        return "Cycle in Progress" if model.get("prediction_ready") else "We're still learning your PCOS cycle"

    def cycle_visual_phase(target_date, day_number, cycle_start, model, log=None):
        if logged_period_day(log, day_number):
            return "period"

        if not model.get("phase_estimation_enabled"):
            return "unknown"

        day_signal = model["ovulation_signals"].get(target_date, {"likely": False})
        if day_signal["likely"]:
            return "ovulation"

        fertile_window = model.get("fertile_window")
        if fertile_window and day_number and fertile_window[0] <= day_number <= fertile_window[1]:
            return "fertile"

        ovulation_date = latest_supported_ovulation_date(model, cycle_start, target_date)
        if ovulation_date and ovulation_date < target_date:
            return "luteal"

        return "unknown"

    def cycle_prediction_summary(reference_date, cycle_start, model):
        cycle_length = model.get("cycle_length")
        if not model.get("prediction_ready") or not cycle_length or not cycle_start or len(model["period_starts"]) < 2:
            fallback_message = (
                "Start logging your cycle to build PCOS-aware insights."
                if not model.get("has_cycle_history")
                else "We're still learning your cycle. PCOS-related irregularity can make predictions less certain until more logs are added."
            )
            return {
                "next_period": None,
                "next_period_earliest": None,
                "next_period_latest": None,
                "days_until_next_period": None,
                "prediction_text": fallback_message,
                "prediction_range_text": fallback_message,
                "expected_date_text": fallback_message,
                "confidence": model.get("confidence_message"),
            }

        earliest_date = cycle_start + timedelta(days=model["cycle_low"])
        latest_date = cycle_start + timedelta(days=model["cycle_high"])
        predicted_date = cycle_start + timedelta(days=cycle_length)
        range_text = compact_date_range(earliest_date, latest_date)
        prediction_text = (
            f"Based on your recent PCOS cycle logs, your next period may start around {predicted_date.strftime('%b %d, %Y')}."
            if earliest_date == latest_date
            else f"Based on your recent PCOS cycle logs, your next period may start around {range_text}."
        )
        return {
            "next_period": predicted_date,
            "next_period_earliest": earliest_date,
            "next_period_latest": latest_date,
            "days_until_next_period": max(0, (earliest_date - reference_date).days),
            "prediction_text": prediction_text,
            "prediction_range_text": range_text,
            "expected_date_text": predicted_date.strftime("%b %d, %Y"),
            "confidence": model["confidence_message"],
        }

    def resolve_cycle_day_value(log_date, model, requested_cycle_day=None, mark_period_start=False):
        if mark_period_start:
            return 1
        if requested_cycle_day and requested_cycle_day > 0:
            return requested_cycle_day
        inferred_day, _ = inferred_cycle_day(log_date, model)
        if inferred_day:
            return inferred_day
        return 1

    def get_cycle_info(user, reference_date=None):
        logs = CycleLog.query.filter_by(user_id=user.id).order_by(CycleLog.log_date.asc()).all()
        model = cycle_model(logs)
        reference_date = reference_date or date.today()
        log_for_date = next((log for log in logs if log.log_date == reference_date), None)
        recent_period = recent_period_tracking(logs, model["period_starts"], reference_date)
        day_number, cycle_start = inferred_cycle_day(reference_date, model)
        if log_for_date and log_for_date.cycle_day:
            day_number = log_for_date.cycle_day
            if is_period_start_log(log_for_date):
                cycle_start = log_for_date.log_date

        phase = cycle_phase_name(reference_date, day_number, cycle_start, model, log_for_date)
        visual_phase = cycle_visual_phase(reference_date, day_number, cycle_start, model, log_for_date)
        prediction = cycle_prediction_summary(reference_date, cycle_start, model)
        fertile_window = model.get("fertile_window")
        fertile_window_text = (
            f"Often around days {fertile_window[0]}-{fertile_window[1]} in your recent pattern, though ovulation can be less predictable in PCOS."
            if fertile_window
            else "We'll estimate this after a little more cycle history."
        )
        day_signal = model["ovulation_signals"].get(reference_date, {"likely": False, "confidence": "none"})
        ovulation_status = (
            "Your recent symptom logs suggest ovulation may be close, though timing can be less certain in PCOS."
            if day_signal["likely"] and day_signal.get("signal_count", 0) >= 2
            else "A few recent symptoms may point to ovulation around this time, but PCOS can make the timing less predictable."
            if day_signal["likely"]
            else f"Your logs most often point to ovulation around day {model['typical_ovulation_day']}, but that timing can shift in PCOS."
            if model.get("typical_ovulation_day")
            else "We'll look for ovulation patterns as you log more symptoms."
        )
        if prediction["next_period"]:
            prediction_basis = (
                "We're still learning your cycle. PCOS-related irregularity can make predictions less certain until more logs are added."
                if model["limited_data"]
                else "This estimate adapts to your recent cycle timing and symptom logs while allowing for PCOS-related variation."
                if model["observed_signal_days"]
                else "This estimate adapts to your recent cycle timing as you add new cycle logs."
            )
        else:
            prediction_basis = model["pattern_summary"]
        irregularity_text = model["pattern_summary"]
        anovulation_warning = (
            "A long gap appears in your recent cycle logs. Irregular or absent ovulation can be seen in PCOS, so consider monitoring symptoms closely and checking with your clinician if this feels unusual for you."
            if model["possible_anovulatory"]
            else ""
        )
        if recent_period["needs_flow_log_prompt"]:
            tracking_message = "Please log your flow to keep your PCOS cycle timing up to date."
        elif not model["has_cycle_history"]:
            tracking_message = "Start logging your cycle to build PCOS-aware insights."
        elif model["limited_data"]:
            tracking_message = "We're still learning your cycle. PCOS-related irregularity can take longer to map clearly."
        else:
            tracking_message = model["pattern_summary"]
        cycle_day_label = "Log more cycle data"
        if log_for_date and log_for_date.cycle_day:
            cycle_day_label = (
                "Logged Day 1 (Period start)"
                if is_period_start_log(log_for_date) and log_for_date.cycle_day == 1
                else f"Logged Day {log_for_date.cycle_day}"
            )
        elif model["prediction_ready"] and day_number:
            cycle_day_label = f"Estimated Day {day_number}"
        period_length_label = "Not enough logged flow data"
        if model["average_period_length"]:
            period_length_label = f"{model['average_period_length']} day average"
            if model["period_length_low"] and model["period_length_high"]:
                period_length_label += f" ({model['period_length_low']}-{model['period_length_high']} observed)"
        if recent_period["current_period_length"]:
            period_length_label = f"{recent_period['current_period_length']} logged day(s) in current period"
        insight_summary = (
            "Please log your flow to keep your PCOS cycle timing up to date."
            if recent_period["needs_flow_log_prompt"]
            else ovulation_status
            if day_signal["likely"]
            else prediction["prediction_text"]
            if prediction["next_period"]
            else tracking_message
        )
        today_for_forecast = date.today()
        delayed_by_prediction = bool(prediction["next_period_latest"] and today_for_forecast > prediction["next_period_latest"])
        delayed_by_gap = bool(not prediction["next_period_latest"] and model["days_since_last_period"] and model["days_since_last_period"] >= 45)
        forecast_is_delayed = bool(model["has_cycle_history"] and (delayed_by_prediction or delayed_by_gap))
        cycle_pattern_state = "Irregular" if model["irregular"] else "Regular" if model["prediction_ready"] else "Learning"

        if forecast_is_delayed:
            forecast_state = "delayed"
            forecast_status_label = "Delayed"
            forecast_headline = "Period appears delayed"
            prediction_basis = (
                f"Your last logged period started {model['days_since_last_period']} days ago, which is past the expected cycle window based on your logged pattern."
                if delayed_by_prediction
                else f"Your last logged period started {model['days_since_last_period']} days ago. Add your next period start when it begins so the estimate can update."
            )
            insight_summary = "Your period appears delayed based on your logged cycle history. This is a tracking reminder, not a diagnosis."
        else:
            forecast_state = "ready" if prediction["next_period"] else "learning"
            forecast_status_label = f"{cycle_pattern_state} estimate" if prediction["next_period"] else "Learning pattern"
            forecast_headline = prediction["prediction_text"] if prediction["next_period"] else "More cycle logs needed"

        next_period_metric_label = "Past expected window" if delayed_by_prediction else "Delayed since" if forecast_is_delayed else "Next period"
        next_period_label = prediction["prediction_range_text"] if prediction["next_period"] else "Not available yet"
        average_cycle_label = f"{model['cycle_length']} days" if model["cycle_length"] else "Add another period start"
        cycle_range_label = (
            f"Recent range {model['cycle_low']}-{model['cycle_high']} days"
            if model["cycle_low"] and model["cycle_high"] and model["cycle_length"]
            else ""
        )
        period_length_range_label = (
            f"Observed range {model['period_length_low']}-{model['period_length_high']} days"
            if model["period_length_low"] and model["period_length_high"] and model["period_length_low"] != model["period_length_high"]
            else ""
        )
        pattern_label = "Delayed cycle" if forecast_is_delayed else "Irregular pattern" if model["irregular"] else "Regular pattern" if model["prediction_ready"] else "Learning pattern"
        confidence_level = (
            "Limited"
            if forecast_is_delayed or model["irregular"]
            else "Building"
            if model["limited_data"]
            else "Moderate"
            if model["prediction_ready"]
            else "Not enough data"
        )
        return {
            "phase": phase,
            "phase_visual": visual_phase,
            "day_number": day_number,
            "cycle_day_label": cycle_day_label,
            "next_period": prediction["next_period"],
            "next_period_earliest": prediction["next_period_earliest"],
            "next_period_latest": prediction["next_period_latest"],
            "days_until_next_period": prediction["days_until_next_period"],
            "prediction_text": prediction["prediction_text"],
            "prediction_range_text": prediction["prediction_range_text"],
            "expected_date_text": prediction["expected_date_text"],
            "prediction_confidence": prediction["confidence"],
            "cycle_length": model["cycle_length"],
            "average_cycle_length": model["cycle_length"],
            "min_cycle_length": model["cycle_low"] if model["observed_lengths"] else None,
            "max_cycle_length": model["cycle_high"] if model["observed_lengths"] else None,
            "average_period_length": model["average_period_length"],
            "min_period_length": model["period_length_low"],
            "max_period_length": model["period_length_high"],
            "period_length_label": period_length_label,
            "cycle_start": cycle_start,
            "last_period_start": model["latest_period_start"],
            "days_since_last_period": model["days_since_last_period"],
            "fertile_window_text": fertile_window_text,
            "ovulation_status": ovulation_status,
            "prediction_basis": prediction_basis,
            "irregularity_text": irregularity_text,
            "anovulation_warning": anovulation_warning,
            "tracking_message": tracking_message,
            "needs_flow_log_prompt": recent_period["needs_flow_log_prompt"],
            "uncertainty_note": (
                "Confidence is still building. In PCOS, ovulation and cycle timing can be less predictable, so this estimate may shift as more logs are recorded."
                if model["limited_data"]
                else "We'll keep adjusting this PCOS cycle estimate as new entries are added."
                if not model["irregular"]
                else "This estimate may shift because your recent cycle timing has been irregular."
            ),
            "smart_mode_enabled": model["smart_mode_enabled"],
            "prediction_ready": model["prediction_ready"],
            "phase_estimation_enabled": model["phase_estimation_enabled"],
            "has_cycle_history": model["has_cycle_history"],
            "confidence": model["confidence_message"],
            "confidence_message": model["confidence_message"],
            "pattern_summary": model["pattern_summary"],
            "limited_data": model["limited_data"],
            "insight_summary": insight_summary,
            "irregular": model["irregular"],
            "forecast_state": forecast_state,
            "forecast_status_label": forecast_status_label,
            "forecast_headline": forecast_headline,
            "next_period_metric_label": next_period_metric_label,
            "next_period_label": next_period_label,
            "average_cycle_label": average_cycle_label,
            "cycle_range_label": cycle_range_label,
            "period_length_range_label": period_length_range_label,
            "pattern_label": pattern_label,
            "confidence_level": confidence_level,
        }

    def build_mental_tip(mood, stress_level):
        tip = "A short breathing or reset break may help support stress recovery today."
        if stress_level >= 8:
            tip = "Your recent stress log is elevated. High stress can affect mood and overall hormonal balance, so a lighter schedule and a support check-in may help today."
        elif mood.lower() in {"sad", "anxious", "awful", "bad"}:
            tip = "Your mood log suggests a gentler day may help. Rest, hydration, and journaling can support overall PCOS self-management."
        return tip

    def build_sleep_mood_insight(mood, stress_slider, sleep_hours):
        """Return a deterministic mood-sleep insight based only on user-entered values."""
        normalized_mood = (mood or "").strip()
        if sleep_hours is None or sleep_hours <= 0:
            return None

        if normalized_mood in {"Awful", "Bad"} and sleep_hours < 6:
            return {
                "title": "PCOS Sleep & Mood Insight",
                "message": "Your recent entries show low mood with reduced sleep. In PCOS support, this pattern can add pressure to stress recovery and hormonal balance.",
                "tone": "warning",
            }

        if normalized_mood in {"Good", "Great"} and 7 <= sleep_hours <= 9:
            return {
                "title": "PCOS Sleep & Mood Insight",
                "message": "Your recent mood and sleep pattern looks supportive of steadier energy and overall hormonal health.",
                "tone": "success",
            }

        if stress_slider >= 4 and sleep_hours < 6:
            return {
                "title": "PCOS Sleep & Mood Insight",
                "message": "Higher stress with low sleep may affect mood, energy, and overall hormonal balance. A calmer routine tonight may help.",
                "tone": "warning",
            }

        return None

    def get_today_lifestyle_log(user):
        return LifestyleLog.query.filter_by(user_id=user.id, log_date=date.today()).first()

    def get_today_mental_log(user):
        return MentalLog.query.filter_by(user_id=user.id, log_date=date.today()).first()

    def get_latest_cycle_log(user):
        return CycleLog.query.filter_by(user_id=user.id).order_by(CycleLog.log_date.desc()).first()

    def logging_consistency_profile(user, days=7):
        start_date = date.today() - timedelta(days=days - 1)
        lifestyle_days = {
            log.log_date for log in LifestyleLog.query.filter(
                LifestyleLog.user_id == user.id,
                LifestyleLog.log_date >= start_date,
            ).all()
        }
        mental_days = {
            log.log_date for log in MentalLog.query.filter(
                MentalLog.user_id == user.id,
                MentalLog.log_date >= start_date,
            ).all()
        }
        cycle_days = {
            log.log_date for log in CycleLog.query.filter(
                CycleLog.user_id == user.id,
                CycleLog.log_date >= start_date,
            ).all()
        }
        completion_points = len(lifestyle_days) + len(mental_days) + min(len(cycle_days), 2)
        max_points = (days * 2) + 2
        ratio = completion_points / max_points if max_points else 0
        if ratio >= 0.75:
            intensity = "low"
        elif ratio >= 0.4:
            intensity = "medium"
        else:
            intensity = "high"
        return {"ratio": round(ratio, 2), "intensity": intensity}

    def build_smart_prompts(user):
        now = app_now()
        today = now.date()
        prompts = []
        consistency = logging_consistency_profile(user)
        lifestyle_log = get_today_lifestyle_log(user)
        mental_log = get_today_mental_log(user)
        cycle_log = get_latest_cycle_log(user)
        medication_cache = request_medication_summary(user)
        medications = medication_cache["medications"]
        pcos_state = build_pcos_state(get_or_create_profile(user))

        due_medications = []
        for medication in medications:
            scheduled_at = datetime.combine(today, medication.time_of_day)
            if medication.status == "pending" and now >= scheduled_at:
                overdue_hours = (now - scheduled_at).total_seconds() / 3600
                due_medications.append({"medication": medication, "overdue_hours": overdue_hours})

        if due_medications:
            most_due = due_medications[0]
            if pcos_state["adherence_focus"]:
                message = (
                    "A medication check-in may help keep your PCOS support routine steady."
                    if most_due["overdue_hours"] >= 2
                    else "It may be a good time to log your medication so your PCOS routine stays consistent."
                )
            else:
                message = (
                    "Please log today's medication to keep your PCOS support plan current."
                    if most_due["overdue_hours"] >= 2
                    else "This medication has not been logged yet. Follow your care instructions or contact your provider if unsure."
                )
            prompts.append(
                {
                    "category": "medication",
                    "priority": "high" if most_due["overdue_hours"] >= 2 else "medium",
                    "message": message,
                    "items": due_medications,
                }
            )

        if not mental_log:
            prompts.append(
                {
                    "category": "mood",
                    "priority": "medium" if now.hour < 19 else "high",
                    "message": (
                        "A quick mood and stress check-in can help track patterns that may affect your PCOS symptoms."
                        if pcos_state["has_mood_swings"]
                        else "A short mood check-in can help connect stress patterns with your PCOS support routine."
                        if now.hour >= 19
                        else "How is your mood today? Logging it can help connect mood and hormone-related patterns over time."
                    ),
                }
            )

        if not lifestyle_log or lifestyle_log.water_intake_liters <= 0:
            prompts.append(
                {
                    "category": "water",
                    "priority": "medium",
                    "message": (
                        "Hydration can support energy and routine consistency in PCOS. Have you logged water today?"
                        if pcos_state["has_fatigue"]
                        else "Have you had enough water today? Hydration can help support your PCOS routine."
                    ),
                    "current_glasses": int(((lifestyle_log.water_intake_liters if lifestyle_log else 0) / 0.25) or 0),
                }
            )

        if not lifestyle_log or lifestyle_log.sleep_hours <= 0:
            prompts.append(
                {
                    "category": "sleep",
                    "priority": "medium" if now.hour < 15 else "low",
                    "message": "How much sleep did you get last night? Sleep consistency can support hormonal health in PCOS.",
                }
            )

        if not lifestyle_log or lifestyle_log.exercise_minutes <= 0:
            prompts.append(
                {
                    "category": "exercise",
                    "priority": "low" if now.hour < 17 else "medium",
                    "message": "Did you do any movement today? Regular activity can support insulin response and energy in PCOS.",
                }
            )

        if not cycle_log or (today - cycle_log.log_date).days >= 4:
            prompts.append(
                {
                    "category": "cycle",
                    "priority": "medium",
                    "message": (
                        "Any cycle changes to log? More entries help map irregular PCOS patterns and ovulation uncertainty."
                        if pcos_state["has_irregular_periods"]
                        else "Any new cycle symptoms to log? Keeping this updated helps your PCOS cycle insights stay current."
                    ),
                }
            )

        for prompt in prompts:
            prompt["intensity"] = consistency["intensity"]
        prompts.sort(key=lambda item: {"high": 0, "medium": 1, "low": 2}.get(item["priority"], 3))
        return prompts

    def cycle_phase_for_day(target_date, day_number, cycle_start, model, log=None):
        return cycle_visual_phase(target_date, day_number, cycle_start, model, log)

    def dashboard_metrics(user):
        profile = get_or_create_profile(user)
        pcos_state = build_pcos_state(profile)
        latest_lifestyle = LifestyleLog.query.filter_by(user_id=user.id).order_by(LifestyleLog.log_date.desc()).first()
        latest_mental = MentalLog.query.filter_by(user_id=user.id).order_by(MentalLog.log_date.desc()).first()
        cycle_info = get_cycle_info(user)
        weekly_wellness_rows = build_weekly_wellness_rows(user)
        latest_lifestyle_details = parse_lifestyle_notes(latest_lifestyle.notes) if latest_lifestyle else {
            "exercise_entries": [],
            "food_entries": [],
        }
        latest_exercise_entry = (
            latest_lifestyle_details["exercise_entries"][-1] if latest_lifestyle_details["exercise_entries"] else None
        )
        latest_food_entry = latest_lifestyle_details["food_entries"][-1] if latest_lifestyle_details["food_entries"] else None
        medication_cache = request_medication_summary(user)
        medications_today = medication_cache["medications"]
        medication_summary = medication_cache["summary"]
        upcoming_appointments = (
            Appointment.query.filter(Appointment.user_id == user.id, Appointment.appointment_date >= date.today())
            .order_by(Appointment.appointment_date.asc())
            .limit(2)
            .all()
        )
        assessment = health_assessment_for_inputs(
            sleep_hours=latest_lifestyle.sleep_hours if latest_lifestyle else 7,
            water_intake=latest_lifestyle.water_intake_liters if latest_lifestyle else 2,
            stress_level=latest_mental.stress_level if latest_mental else 5,
            activity_minutes=latest_lifestyle.exercise_minutes if latest_lifestyle else 30,
        )
        weekly_wellness_trend = build_weekly_wellness_trend(weekly_wellness_rows)
        weekly_wellness_trend["support_note"] = (
            "PCOS Insight: personalized from your logged cycle, mood, sleep, nutrition, and activity data. For educational support only."
            if pcos_state["has_info"]
            else "PCOS Insight: this trend is based on your recent lifestyle logs."
        )
        reminders = [f"{med.name} at {med.time_of_day.strftime('%H:%M')}" for med in medications_today]
        reminders.extend(
            f"Appointment with {appt.doctor_name} on {appt.appointment_date:%b %d}" for appt in upcoming_appointments
        )
        taken_count = medication_summary["taken_count"]
        skipped_count = medication_summary["skipped_count"]
        missed_count = medication_summary["missed_count"]
        unconfirmed_count = medication_summary["unconfirmed_count"]
        confirmed_count = medication_summary["confirmed_count"]
        total_count = medication_summary["total_count"]
        has_user_activity = bool(
            latest_lifestyle
            or latest_mental
            or cycle_info.get("has_cycle_history")
            or medications_today
            or upcoming_appointments
        )
        return {
            "pcos_state": pcos_state,
            "dashboard_intro": (
                "Here is your daily PCOS support summary, personalized from your logged habits and reported symptoms. For educational support only."
                if pcos_state["has_info"]
                else "Here is your daily PCOS support summary based on your logged habits."
            ),
            "latest_lifestyle": latest_lifestyle,
            "latest_lifestyle_date_label": display_date_label(latest_lifestyle.log_date) if latest_lifestyle else "",
            "latest_mental": latest_mental,
            "latest_mental_date_label": display_date_label(latest_mental.log_date) if latest_mental else "",
            "cycle_info": cycle_info,
            "upcoming_appointments": upcoming_appointments,
            "reminders": reminders,
            "health_score": assessment["score"],
            "weekly_wellness_trend": weekly_wellness_trend,
            "smart_prompts": build_smart_prompts(user),
            "taken_count": taken_count,
            "skipped_count": skipped_count,
            "missed_count": missed_count,
            "unconfirmed_count": unconfirmed_count,
            "confirmed_count": confirmed_count,
            "total_count": total_count,
            "has_user_activity": has_user_activity,
        }

    def profile_summary(user):
        profile = get_or_create_profile(user)
        pcos_state = build_pcos_state(profile)
        cycle_info = get_cycle_info(user)
        cycle_logs = CycleLog.query.filter_by(user_id=user.id).order_by(CycleLog.log_date.asc()).all()
        lifestyle_logs = LifestyleLog.query.filter_by(user_id=user.id).order_by(LifestyleLog.log_date.desc()).all()
        mental_logs = MentalLog.query.filter_by(user_id=user.id).order_by(MentalLog.log_date.desc()).all()
        medications = Medication.query.filter_by(user_id=user.id).order_by(Medication.time_of_day.asc()).limit(3).all()
        normalize_medication_statuses(user, medications)
        latest_period_start = cycle_info.get("last_period_start")
        avg_cycle_length = cycle_info.get("average_cycle_length")
        cycle_regularity = (
            "Irregular PCOS Pattern"
            if cycle_info.get("irregular")
            else "More Regular Pattern"
            if avg_cycle_length
            else "Tracking in progress"
        )
        latest_lifestyle = lifestyle_logs[0] if lifestyle_logs else None
        latest_mental = mental_logs[0] if mental_logs else None
        lifestyle_details = parse_lifestyle_notes(latest_lifestyle.notes) if latest_lifestyle else {
            "exercise_entries": [],
            "food_entries": [],
        }
        latest_exercise_entry = lifestyle_details["exercise_entries"][-1] if lifestyle_details["exercise_entries"] else None
        latest_food_entry = lifestyle_details["food_entries"][-1] if lifestyle_details["food_entries"] else None
        logged_days = sorted({log.log_date for log in cycle_logs}, reverse=True)
        streak = 0
        cursor = date.today()
        for log_day in logged_days:
            if log_day == cursor:
                streak += 1
                cursor -= timedelta(days=1)
            elif log_day < cursor:
                break
        symptom_counts = Counter()
        latest_symptom_date = None
        for log in sorted(cycle_logs, key=lambda item: item.log_date, reverse=True):
            details = unpack_cycle_details(log.symptoms)
            symptoms = split_list_text(details["symptoms"])
            if symptoms and latest_symptom_date is None:
                latest_symptom_date = log.log_date
            symptom_counts.update(symptoms)
        wellness_trend = build_weekly_wellness_trend(build_weekly_wellness_rows(user))
        return {
            "profile": profile,
            "pcos_state": pcos_state,
            "cycle_info": cycle_info,
            "personal_info": {
                "full_name": user.full_name,
                "email": user.username,
                "age": profile.age,
                "age_label": f"{profile.age} years old" if profile.age else "Not set",
                "diagnosis_date": profile.diagnosis_date.strftime("%Y-%m-%d") if profile.diagnosis_date else "",
                "diagnosis_date_label": (
                    profile.diagnosis_date.strftime("%b %d, %Y")
                    if profile.diagnosis_date
                    else "Not set"
                ),
            },
            "health_overview": [
                {
                    "label": "Last Logged Period",
                    "value": latest_period_start.strftime("%b %d, %Y") if latest_period_start else "No period logged yet",
                },
                {
                    "label": "PCOS Cycle Pattern",
                    "value": cycle_regularity if avg_cycle_length else "Start logging your cycle to monitor PCOS-related irregularity",
                },
                {
                    "label": "Average Cycle Length",
                    "value": f"{avg_cycle_length} days" if avg_cycle_length else "Will be calculated after multiple cycle entries",
                },
            ],
            "medications": medications,
            "lifestyle_summary": {
                "latest_date": latest_lifestyle.log_date.strftime("%b %d, %Y") if latest_lifestyle else "No lifestyle logs yet",
                "water": f"{latest_lifestyle.water_intake_liters:.1f} L" if latest_lifestyle else "Not logged",
                "sleep": f"{latest_lifestyle.sleep_hours:.1f} hours" if latest_lifestyle else "Not logged",
                "exercise": (
                    f"{latest_exercise_entry['raw_input']['duration_minutes']} min {latest_exercise_entry['raw_input']['activity_type']}"
                    if latest_exercise_entry
                    else f"{latest_lifestyle.exercise_minutes} min" if latest_lifestyle and latest_lifestyle.exercise_minutes else "Not logged"
                ),
                "diet": (
                    latest_food_entry["raw_input"]["food_category"].replace("-", " ").title()
                    if latest_food_entry
                    else latest_lifestyle.diet_quality if latest_lifestyle else "Not logged"
                ),
            },
            "mood_summary": {
                "latest_date": latest_mental.log_date.strftime("%b %d, %Y") if latest_mental else "No mood logs yet",
                "mood": latest_mental.mood if latest_mental else "Not logged",
                "stress": f"{latest_mental.stress_level}/10" if latest_mental else "Not logged",
                "checkins": len(mental_logs),
            },
            "symptom_overview": {
                "symptoms": [
                    {"name": symptom, "count": count}
                    for symptom, count in symptom_counts.most_common(6)
                ],
                "latest_date": latest_symptom_date.strftime("%b %d, %Y") if latest_symptom_date else "No symptoms logged yet",
            },
            "progress_insights": [
                {
                    "label": "Cycle logs",
                    "value": f"{len(cycle_logs)} day{'s' if len(cycle_logs) != 1 else ''}",
                    "helper": "Helps map irregular PCOS timing and ovulation uncertainty.",
                },
                {
                    "label": "Lifestyle logs",
                    "value": f"{len(lifestyle_logs)} {'entries' if len(lifestyle_logs) != 1 else 'entry'}",
                    "helper": latest_lifestyle.log_date.strftime("Latest on %b %d") if latest_lifestyle else "Start with sleep, hydration, or meals.",
                },
                {
                    "label": "Mood check-ins",
                    "value": f"{len(mental_logs)} {'entries' if len(mental_logs) != 1 else 'entry'}",
                    "helper": latest_mental.log_date.strftime("Latest on %b %d") if latest_mental else "Save your first mood log for PCOS stress tracking.",
                },
                {
                    "label": "PCOS wellness trend",
                    "value": wellness_trend["predicted_label"],
                    "helper": wellness_trend["explanation"],
                },
            ],
            "quick_stats": [
                {
                    "label": "Last period",
                    "value": latest_period_start.strftime("%b %d, %Y") if latest_period_start else "No record yet",
                    "href": url_for("calendar"),
                    "helper": "",
                },
                {
                    "label": "Cycle regularity",
                    "value": cycle_regularity if avg_cycle_length else "Tracking in progress",
                    "href": url_for("calendar"),
                    "helper": "",
                },
                {
                    "label": "Avg cycle length",
                    "value": f"{avg_cycle_length} days" if avg_cycle_length else "Not available yet",
                    "href": url_for("calendar"),
                    "helper": "",
                },
                {
                    "label": "Current streak",
                    "value": f"{streak} day{'s' if streak != 1 else ''}",
                    "href": url_for("dashboard"),
                    "helper": "Start logging to build consistency" if streak == 0 else "",
                },
            ],
        }

    def serialize_cycle_info(cycle_info):
        return {
            "phase": cycle_info.get("phase"),
            "phase_visual": cycle_info.get("phase_visual"),
            "day_number": cycle_info.get("day_number"),
            "cycle_day_label": cycle_info.get("cycle_day_label"),
            "next_period": iso_date(cycle_info.get("next_period")),
            "next_period_earliest": iso_date(cycle_info.get("next_period_earliest")),
            "next_period_latest": iso_date(cycle_info.get("next_period_latest")),
            "days_until_next_period": cycle_info.get("days_until_next_period"),
            "prediction_text": cycle_info.get("prediction_text"),
            "prediction_range_text": cycle_info.get("prediction_range_text"),
            "expected_date_text": cycle_info.get("expected_date_text"),
            "prediction_confidence": cycle_info.get("prediction_confidence"),
            "average_cycle_length": cycle_info.get("average_cycle_length"),
            "min_cycle_length": cycle_info.get("min_cycle_length"),
            "max_cycle_length": cycle_info.get("max_cycle_length"),
            "average_period_length": cycle_info.get("average_period_length"),
            "min_period_length": cycle_info.get("min_period_length"),
            "max_period_length": cycle_info.get("max_period_length"),
            "period_length_label": cycle_info.get("period_length_label"),
            "last_period_start": iso_date(cycle_info.get("last_period_start")),
            "days_since_last_period": cycle_info.get("days_since_last_period"),
            "fertile_window_text": cycle_info.get("fertile_window_text"),
            "ovulation_status": cycle_info.get("ovulation_status"),
            "prediction_basis": cycle_info.get("prediction_basis"),
            "irregularity_text": cycle_info.get("irregularity_text"),
            "anovulation_warning": cycle_info.get("anovulation_warning"),
            "tracking_message": cycle_info.get("tracking_message"),
            "needs_flow_log_prompt": safe_bool(cycle_info.get("needs_flow_log_prompt")),
            "uncertainty_note": cycle_info.get("uncertainty_note"),
            "smart_mode_enabled": safe_bool(cycle_info.get("smart_mode_enabled")),
            "prediction_ready": safe_bool(cycle_info.get("prediction_ready")),
            "phase_estimation_enabled": safe_bool(cycle_info.get("phase_estimation_enabled")),
            "has_cycle_history": safe_bool(cycle_info.get("has_cycle_history")),
            "confidence": cycle_info.get("confidence"),
            "confidence_message": cycle_info.get("confidence_message"),
            "pattern_summary": cycle_info.get("pattern_summary"),
            "limited_data": safe_bool(cycle_info.get("limited_data")),
            "insight_summary": cycle_info.get("insight_summary"),
            "irregular": safe_bool(cycle_info.get("irregular")),
        }

    def serialize_profile_summary(summary):
        profile = summary["profile"]
        return {
            "identity": {
                "full_name": summary["profile"].user.full_name,
                "username": summary["profile"].user.full_name,
                "email": summary["profile"].user.username,
                "age": profile.age,
                "diagnosis_date": iso_date(profile.diagnosis_date),
            },
            "health_overview": summary["health_overview"],
            "quick_stats": summary["quick_stats"],
            "lifestyle_summary": summary["lifestyle_summary"],
            "mood_summary": summary["mood_summary"],
            "symptom_overview": summary["symptom_overview"],
            "progress_insights": summary["progress_insights"],
            "cycle_info": serialize_cycle_info(summary["cycle_info"]),
            "medications_preview": [
                {
                    "id": medication.id,
                    "name": medication.name,
                    "dosage": medication.dosage,
                    "time_of_day": iso_time(medication.time_of_day),
                    "status": medication.status,
                    "daily_status": getattr(medication, "daily_status", medication.status),
                }
                for medication in summary["medications"]
            ],
        }

    def serialize_dashboard_metrics(metrics):
        return {
            "pcos_state": {
                "status": metrics["pcos_state"]["status"],
                "symptoms": metrics["pcos_state"]["symptoms"],
                "has_info": safe_bool(metrics["pcos_state"]["has_info"]),
            },
            "dashboard_intro": metrics["dashboard_intro"],
            "taken_count": metrics["taken_count"],
            "skipped_count": metrics["skipped_count"],
            "missed_count": metrics["missed_count"],
            "total_count": metrics["total_count"],
            "health_score": metrics["health_score"],
            "reminders": metrics["reminders"],
            "cycle_info": serialize_cycle_info(metrics["cycle_info"]),
            "latest_lifestyle": (
                {
                    "log_date": iso_date(metrics["latest_lifestyle"].log_date),
                    "sleep_hours": metrics["latest_lifestyle"].sleep_hours,
                    "water_intake_liters": metrics["latest_lifestyle"].water_intake_liters,
                    "diet_quality": metrics["latest_lifestyle"].diet_quality,
                    "exercise_minutes": metrics["latest_lifestyle"].exercise_minutes,
                    "notes": metrics["latest_lifestyle"].notes,
                }
                if metrics["latest_lifestyle"]
                else None
            ),
            "latest_mental": (
                {
                    "log_date": iso_date(metrics["latest_mental"].log_date),
                    "mood": metrics["latest_mental"].mood,
                    "stress_level": metrics["latest_mental"].stress_level,
                    "wellness_tip": metrics["latest_mental"].wellness_tip,
                }
                if metrics["latest_mental"]
                else None
            ),
            "upcoming_appointments": [
                {
                    "id": appointment.id,
                    "doctor_name": appointment.doctor_name,
                    "appointment_date": iso_date(appointment.appointment_date),
                    "appointment_time": iso_time(appointment.appointment_time),
                    "notes": appointment.notes,
                    "follow_up_date": iso_date(appointment.follow_up_date),
                }
                for appointment in metrics["upcoming_appointments"]
            ],
            "smart_prompts": metrics["smart_prompts"],
        }

    def serialize_alerts_payload(score, recent_patterns, active_alerts, risk_indicator, related_insight=None):
        return {
            "health_score": score,
            "risk_indicator": risk_indicator,
            "recent_patterns": recent_patterns,
            "active_alerts": active_alerts,
            "related_insight": related_insight,
        }

    def build_alerts_context(user):
        pcos_state = build_pcos_state(get_or_create_profile(user))
        lifestyle_logs = LifestyleLog.query.filter_by(user_id=user.id).order_by(LifestyleLog.log_date.asc()).all()
        mental_logs = MentalLog.query.filter_by(user_id=user.id).order_by(MentalLog.log_date.asc()).all()
        latest_lifestyle = lifestyle_logs[-1] if lifestyle_logs else None
        latest_mental = mental_logs[-1] if mental_logs else None
        sleep_hours_for_insight = latest_lifestyle.sleep_hours if latest_lifestyle else None
        sleep_mood_insight = build_sleep_mood_insight(
            latest_mental.mood if latest_mental else "",
            max(1, min(5, round((latest_mental.stress_level if latest_mental else 6) / 2))),
            sleep_hours_for_insight,
        )
        cycle_info = get_cycle_info(user)
        smart_prompts = build_smart_prompts(user)
        assessment = health_assessment_for_inputs(
            sleep_hours=latest_lifestyle.sleep_hours if latest_lifestyle else 7,
            water_intake=latest_lifestyle.water_intake_liters if latest_lifestyle else 2,
            stress_level=latest_mental.stress_level if latest_mental else 5,
            activity_minutes=latest_lifestyle.exercise_minutes if latest_lifestyle else 30,
        )
        score = assessment["score"]
        insights = assessment["recommendations"]
        sleep_evaluation = assessment["sleep_evaluation"]
        stress_evaluation = assessment["stress_evaluation"]
        hydration_evaluation = assessment["hydration_evaluation"]
        trends = []
        for log in lifestyle_logs[-7:]:
            trend_assessment = health_assessment_for_inputs(
                sleep_hours=log.sleep_hours,
                water_intake=log.water_intake_liters,
                stress_level=latest_mental.stress_level if latest_mental else 5,
                activity_minutes=log.exercise_minutes,
            )
            trends.append(
                {
                    "label": log.log_date.strftime("%b %d"),
                    "score": round(trend_assessment["score"], 1),
                }
            )
        recent_lifestyle = lifestyle_logs[-7:]
        recent_mental = mental_logs[-7:]
        avg_sleep = round(sum(log.sleep_hours for log in recent_lifestyle) / len(recent_lifestyle), 1) if recent_lifestyle else 5.5
        avg_water = round(sum(log.water_intake_liters for log in recent_lifestyle) / len(recent_lifestyle), 1) if recent_lifestyle else 1.5
        avg_exercise = round(sum(log.exercise_minutes for log in recent_lifestyle) / len(recent_lifestyle)) if recent_lifestyle else 20
        avg_stress = round(sum(log.stress_level for log in recent_mental) / len(recent_mental), 1) if recent_mental else 3.5
        sleep_days_under_target = sum(1 for log in recent_lifestyle if log.sleep_hours < 6.5)
        high_stress_days = sum(1 for log in recent_mental if log.stress_level >= 7)
        hydration_goal_days = sum(1 for log in recent_lifestyle if log.water_intake_liters >= 2.0)
        low_activity_days = sum(1 for log in recent_lifestyle if log.exercise_minutes < 30)
        if recent_lifestyle:
            recent_patterns = [
                {
                    "title": sleep_evaluation["title"],
                    "meta": f"Recent sleep average is {avg_sleep}h. Sleep consistency can influence energy, mood, and cycle regularity in PCOS.",
                    "tone": "indigo" if sleep_evaluation["status"] == "low" else "green",
                },
                {
                    "title": stress_evaluation["title"],
                    "meta": f"Recent stress average is {avg_stress}/10. Stress may influence overall hormonal balance in PCOS.",
                    "tone": "purple" if stress_evaluation["status"] in {"high", "moderate"} else "green",
                },
                {
                    "title": hydration_evaluation["title"],
                    "meta": f"Recent water average is {avg_water}L. Hydration supports steadier routines and day-to-day energy while managing PCOS.",
                    "tone": "orange" if hydration_evaluation["status"] == "low" else "green",
                },
            ]
            if pcos_state["has_mood_swings"]:
                recent_patterns.append(
                    {
                        "title": "Mood & Stress Pattern Tracking",
                        "meta": "Consistent mood logs can help connect stress changes with day-to-day PCOS symptom patterns.",
                        "tone": "purple",
                    }
                )
            if pcos_state["metabolic_focus"]:
                recent_patterns.append(
                    {
                        "title": "Metabolic Support Routine",
                        "meta": "Steady meals and regular movement can support insulin balance in PCOS.",
                        "tone": "green",
                    }
                )
            if low_activity_days >= 4:
                recent_patterns.append(
                    {
                        "title": "Lower Activity Pattern",
                        "meta": f"Average movement is {avg_exercise} minutes. Regular activity can support insulin response in PCOS.",
                        "tone": "orange",
                    }
                )
        else:
            recent_patterns = [
                {"title": "Sleep insights start here", "meta": "Start logging your sleep to build PCOS-aware recovery insights.", "tone": "indigo"},
                {"title": "Stress insights start here", "meta": "Mood and stress logs help this page reflect your own PCOS routine.", "tone": "purple"},
                {"title": "Hydration insights start here", "meta": "Track your water intake to support steadier PCOS self-management insights.", "tone": "orange"},
            ]
        active_alerts = list(assessment["combined_alerts"])
        if pcos_state["has_irregular_periods"]:
            active_alerts.insert(
                0,
                {
                    "tone": "neutral",
                    "title": "PCOS Cycle Insight Building",
                    "description": "Cycle dates and symptom logs help map irregular PCOS patterns more clearly over time.",
                    "recommendation": "Log period starts, flow, and symptom changes when you notice them.",
                },
            )
        elif not pcos_state["has_symptoms"]:
            active_alerts.insert(
                0,
                {
                    "tone": "neutral",
                    "title": "PCOS Support Guidance",
                    "description": "Your support alerts are based on daily logs across cycle, mood, sleep, meals, and medication.",
                    "recommendation": "Keep logging your cycle, mood, meals, sleep, and water for more relevant PCOS support.",
                },
            )
        if pcos_state["has_fatigue"]:
            active_alerts.append(
                {
                    "tone": "neutral",
                    "title": "Energy Support Reminder (PCOS)",
                    "description": "Recent sleep and hydration patterns may be contributing to lower energy, which can make PCOS self-management feel harder.",
                    "recommendation": "Try logging sleep and water at the same time each day to spot energy patterns more easily.",
                }
            )
        if pcos_state["has_mood_swings"]:
            active_alerts.append(
                {
                    "tone": "neutral",
                    "title": "Stress Pattern Reminder (PCOS)",
                    "description": "Regular stress check-ins can help show whether mood changes may be influencing your PCOS support routine.",
                    "recommendation": "Try a quick mood and stress log once a day to build a steadier picture over time.",
                }
            )
        if cycle_info["needs_flow_log_prompt"]:
            active_alerts.append(
                {
                    "tone": "warning",
                    "title": "Flow Log Needed for PCOS Insight",
                    "description": "A recent period was logged, but today's flow is missing. That makes it harder to track cycle irregularity accurately in PCOS.",
                    "recommendation": "Log Light, Medium, Heavy, or No Flow today to keep PCOS cycle insights grounded in your records.",
                }
            )
        elif cycle_info["next_period_earliest"] and cycle_info["prediction_ready"]:
            days_to_next_period = (cycle_info["next_period_earliest"] - date.today()).days
            if 0 <= days_to_next_period <= 5:
                active_alerts.append(
                    {
                        "tone": "danger",
                        "title": "Possible Period Window Approaching",
                        "description": f"Based on your recent PCOS cycle logs, the next period window currently falls between {cycle_info['prediction_range_text']}. This range may shift as more data is added.",
                        "recommendation": "Prepare supplies, monitor symptoms, and keep logging daily changes.",
                    }
                )
        elif cycle_info["phase_estimation_enabled"] and (
            "Possible Ovulation" in cycle_info["phase"] or "Possible Fertile Window" in cycle_info["phase"]
        ):
            active_alerts.append(
                {
                    "tone": "neutral",
                    "title": "Possible Ovulation Window (PCOS Estimate)",
                    "description": "Recent symptoms suggest a possible fertile or ovulation window, though ovulation timing can be less predictable in PCOS.",
                    "recommendation": "Continue logging symptoms and flow so the estimate stays grounded in your actual data.",
                }
            )
        elif not cycle_info["has_cycle_history"]:
            active_alerts.append(
                {
                    "tone": "neutral",
                    "title": "Cycle Tracking Needed",
                    "description": "Log more cycle data so PCOS-related cycle insights can stay grounded in your records.",
                    "recommendation": "Keep logging period starts and daily symptoms for more reliable PCOS support guidance.",
                }
            )
        if hydration_goal_days >= 5:
            active_alerts.append(
                {
                    "tone": "success",
                    "title": "Hydration Pattern Supporting PCOS Management",
                    "description": f"Your recent logs show {hydration_goal_days} day(s) with steady hydration, which can support energy and routine consistency in PCOS.",
                    "recommendation": "Keep following a steady hydration routine.",
                }
            )
        elif latest_lifestyle and latest_lifestyle.water_intake_liters < 1.5:
            active_alerts.append(
                {
                    "tone": "warning",
                    "title": "Low Hydration Pattern",
                    "description": f"Your latest log shows {latest_lifestyle.water_intake_liters:.1f}L of water. Lower hydration may make energy and daily PCOS support feel less steady.",
                    "recommendation": "Aim for a more consistent fluid routine across the day.",
                }
            )
        if low_activity_days >= 4:
            active_alerts.append(
                {
                    "tone": "warning",
                    "title": "Lower Activity Pattern Affecting PCOS Support",
                    "description": f"You've logged under 30 minutes of activity on {low_activity_days} recent day(s). Lower movement may reduce insulin-supportive routine consistency.",
                    "recommendation": "Try a 10- to 20-minute walk after meals this week.",
                }
            )
        risk_indicator = "low"
        if score < 55 or sleep_evaluation["status"] == "low" and stress_evaluation["status"] == "high" or any(prompt["priority"] == "high" for prompt in smart_prompts):
            risk_indicator = "high"
        elif score < 75 or hydration_evaluation["status"] == "low" or stress_evaluation["status"] == "moderate" or any(prompt["priority"] == "medium" for prompt in smart_prompts):
            risk_indicator = "medium"
        return {
            "score": score,
            "insights": insights,
            "trends": trends,
            "recent_patterns": recent_patterns,
            "active_alerts": active_alerts,
            "sleep_mood_insight": sleep_mood_insight,
            "sleep_hours_for_insight": sleep_hours_for_insight,
            "smart_prompts": smart_prompts,
            "risk_indicator": risk_indicator,
            "pcos_state": pcos_state,
            "alerts_personalization_note": (
                "These PCOS support alerts adjust to the symptoms and routines you log. They are for educational support only."
                if pcos_state["has_info"]
                else "PCOS support alerts are based on your daily logs."
            ),
        }

    @app.route("/")
    def index():
        return redirect(url_for("dashboard" if current_user() else "login"))

    @app.route("/register", methods=["GET", "POST"])
    def register():
        redirect_response = redirect_authenticated_user()
        if redirect_response:
            return redirect_response

        form_values = {"full_name": "", "email": ""}
        form_errors = {}
        if request.method == "POST":
            full_name = normalize_full_name(request.form.get("full_name"))
            email = normalize_email(request.form.get("email") or request.form.get("username"))
            password = request.form.get("password") or ""
            confirm_password = request.form.get("confirm_password", "")
            form_values = {"full_name": full_name, "email": email}
            existing_user = User.query.filter_by(username=email).first() if email else None

            if not valid_full_name(full_name):
                form_errors["full_name"] = full_name_help
            if not valid_email(email):
                form_errors["email"] = email_help
            if not password:
                form_errors["password"] = "Password is required."
            if password != confirm_password:
                form_errors["confirm_password"] = "Passwords do not match."
            password_errors = validate_password_strength(password)
            if password_errors:
                form_errors["password"] = " ".join(password_errors)
            if existing_user:
                if existing_user.email_verified:
                    form_errors["email"] = email_taken
                else:
                    db.session.delete(existing_user)
                    db.session.commit()
                    existing_user = None
            if form_errors:
                return render_template("auth/register.html", **build_auth_context("register", form_values, form_errors))

            try:
                auth_response = register_supabase_password_account(email, password, full_name)
            except Exception as error:
                if is_user_already_registered_error(error):
                    try:
                        send_email_otp(email, should_create_user=False)
                    except Exception as otp_error:
                        log_supabase_otp_error(email, otp_error)
                        form_errors["email"] = otp_send_error_message(otp_error)
                        return render_template("auth/register.html", **build_auth_context("register", form_values, form_errors))

                    ensure_local_user(email, full_name=full_name)
                    remember_pending_verification(email, verification_type="email", new_account=True)
                    flash("OTP sent to your email.", "success")
                    return redirect(url_for("verify_email"))

                form_errors["email"] = friendly_supabase_error(
                    error,
                    "We could not create your account right now. Please try again.",
                )
                return render_template("auth/register.html", **build_auth_context("register", form_values, form_errors))

            ensure_local_user(
                email,
                password=password,
                full_name=full_name,
                auth_user=response_auth_user(auth_response),
            )
            remember_pending_verification(email, verification_type="email", new_account=True)
            remember_otp_request(email)
            flash("OTP sent to your email.", "success")
            return redirect(url_for("verify_email"))
        return render_template("auth/register.html", **build_auth_context("register", form_values))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        redirect_response = redirect_authenticated_user()
        if redirect_response:
            return redirect_response

        form_values = {"email": ""}
        form_errors = {}
        if request.method == "POST":
            email = normalize_email(request.form.get("email") or request.form.get("username"))
            password = request.form.get("password") or ""
            form_values = {"email": email}
            if not valid_email(email):
                form_errors["email"] = email_help
                return render_template("auth/login.html", **build_auth_context("login", form_values, form_errors))
            if not password:
                form_errors["password"] = "Password is required."
                return render_template("auth/login.html", **build_auth_context("login", form_values, form_errors))
            lock_message = check_login_rate_limit(email)
            if lock_message:
                form_errors["email"] = lock_message
                form_errors["password"] = "Login temporarily locked."
                return render_template("auth/login.html", **build_auth_context("login", form_values, form_errors))

            local_user = User.query.filter_by(username=email).first()
            try:
                supabase = get_supabase_client()
                auth_response = supabase.auth.sign_in_with_password({"email": email, "password": password})
            except Exception as error:
                if is_unverified_email_error(error):
                    clear_failed_login(email)
                    remember_pending_verification(email, verification_type="email")
                    if otp_retry_seconds(email) == 0:
                        try:
                            resend_signup_otp(email)
                            flash("OTP sent to your email.", "success")
                        except Exception as resend_error:
                            log_supabase_otp_error(email, resend_error)
                            flash(otp_send_error_message(resend_error), "warning")
                    flash("Please verify your email before logging in.", "warning")
                    return redirect(url_for("verify_email"))
                if is_rate_limited_error(error):
                    form_errors["email"] = friendly_supabase_error(error, "Too many requests. Please try again shortly.")
                elif is_timeout_error(error) or is_network_error(error) or can_use_local_auth_fallback(error):
                    if (
                        can_use_local_auth_fallback(error)
                        and local_user
                        and local_user.email_verified
                        and verify_password(password, local_user.password_hash)
                    ):
                        clear_failed_login(email)
                        start_authenticated_session(local_user)
                        flash("Signed in using local account while auth service is unavailable.", "warning")
                        return redirect(url_for("dashboard"))
                    form_errors["password"] = friendly_supabase_error(
                        error,
                        "Service temporarily unavailable. Please try again later.",
                    )
                else:
                    record_failed_login(email)
                    if local_user and not local_user.supabase_user_id and verify_password(password, local_user.password_hash):
                        try:
                            bootstrap_response = register_supabase_password_account(email, password, local_user.full_name)
                        except Exception:
                            form_errors["password"] = "Invalid email or password."
                        else:
                            ensure_local_user(
                                email,
                                password=password,
                                full_name=local_user.full_name,
                                auth_user=response_auth_user(bootstrap_response),
                            )
                            remember_pending_verification(email, verification_type="email")
                            remember_otp_request(email)
                            flash("OTP sent to your email.", "success")
                            flash("Please verify your email before logging in.", "warning")
                            return redirect(url_for("verify_email"))
                    else:
                        form_errors["password"] = "Invalid email or password."
                return render_template("auth/login.html", **build_auth_context("login", form_values, form_errors))

            clear_failed_login(email)
            user = ensure_local_user(
                email,
                password=password,
                full_name=local_user.full_name if local_user else "",
                auth_user=response_auth_user(auth_response),
            )
            if not user.email_verified:
                remember_pending_verification(email, verification_type="email")
                flash("Please verify your email before logging in.", "warning")
                return redirect(url_for("verify_email"))

            start_authenticated_session(user)
            return redirect(url_for("dashboard"))
        return render_template("auth/login.html", **build_auth_context("login", form_values, form_errors))

    @app.route("/login/otp", methods=["GET", "POST"])
    @app.route("/auth/request-otp", methods=["GET", "POST"])
    def request_otp():
        email = normalize_email(request.values.get("email") or request.values.get("username") or pending_verification_email())
        if email:
            remember_pending_verification(email)
        flash("Enter your email and verify the OTP we sent.", "info")
        return redirect(url_for("verify_email"))

    @app.post("/send-otp")
    def send_otp():
        payload = request.get_json(silent=True) if request.is_json else {}
        email = normalize_email(
            request.form.get("email")
            or request.values.get("email")
            or (payload or {}).get("email")
        )
        if not valid_email(email):
            return {"message": email_help}, 422

        retry_in = otp_retry_seconds(email)
        if retry_in > 0:
            return {"message": f"Please wait {retry_in} seconds before requesting another OTP."}, 429

        existing_user = User.query.filter_by(username=email).first()
        is_new_account = not existing_user or not existing_user.email_verified
        try:
            supabase = get_supabase_client()
            supabase.auth.sign_in_with_otp(
                {
                    "email": email,
                    "options": {
                        "should_create_user": True,
                    },
                }
            )
        except Exception as error:
            log_supabase_otp_error(email, error)
            status_code = 429 if is_rate_limited_error(error) else 503 if (is_timeout_error(error) or is_network_error(error)) else 400
            return {"message": otp_send_error_message(error)}, status_code

        if not existing_user:
            ensure_local_user(email)
        remember_pending_verification(email, verification_type="email", new_account=is_new_account)
        remember_otp_request(email)
        return {"message": "OTP sent"}

    @app.route("/verify-otp", methods=["GET", "POST"])
    def verify_otp():
        if request.method == "GET":
            return redirect(url_for("verify_email"))

        payload = request.get_json(silent=True) if request.is_json else {}
        email = normalize_email(
            request.form.get("email")
            or request.values.get("email")
            or (payload or {}).get("email")
        )
        otp = (
            request.form.get("otp")
            or request.values.get("otp")
            or (payload or {}).get("otp")
            or (payload or {}).get("token")
            or ""
        ).strip()

        if not valid_email(email):
            return {"error": email_help}, 422
        if not otp:
            return {"error": "Enter the 6-digit OTP from your email."}, 422

        existing_user = User.query.filter_by(username=email).first()
        is_new_account = not existing_user or not existing_user.email_verified
        try:
            supabase = get_supabase_client()
            auth_response = supabase.auth.verify_otp(
                {
                    "email": email,
                    "token": otp,
                    "type": "email",
                }
            )
        except Exception as error:
            return {"error": friendly_supabase_error(error, "Invalid OTP.")}, 401

        auth_user = response_auth_user(auth_response)
        if not auth_user:
            return {"error": "Invalid OTP"}, 401

        user = ensure_local_user(email, auth_user=auth_user)
        clear_failed_login(email)
        start_authenticated_session(user, new_account=is_new_account)
        return {"message": "Login successful"}

    @app.route("/verify-email", methods=["GET", "POST"])
    @app.route("/login/otp/verify", methods=["GET", "POST"])
    def verify_email():
        active_user = current_user()
        if active_user and active_user.email_verified:
            return redirect(url_for("dashboard"))
        if active_user and not active_user.email_verified:
            remember_pending_verification(active_user.username, verification_type="email")

        verification_type = (request.args.get("type") or session.get("pending_verification_type") or "email").strip().lower()
        if verification_type not in {"signup", "email"}:
            verification_type = "email"
        effective_verification_type = "email"

        form_values = {
            "email": normalize_email(request.args.get("email") or pending_verification_email()),
            "token": "",
        }
        form_errors = {}

        query_token_hash = (request.args.get("token_hash") or "").strip()
        if query_token_hash:
            try:
                supabase = get_supabase_client()
                auth_response = supabase.auth.verify_otp({"token_hash": query_token_hash, "type": effective_verification_type})
            except Exception as error:
                flash(
                    password_reset_error_message(
                        error,
                        "We could not verify this email link. Please enter the OTP from your email instead.",
                    ),
                    "danger",
                )
            else:
                email = response_user_email(auth_response) or form_values["email"]
                user = ensure_local_user(email, auth_user=response_auth_user(auth_response))
                new_account = bool(session.get("pending_new_account"))
                clear_failed_login(email)
                start_authenticated_session(user, new_account=new_account)
                flash("Email verified successfully.", "success")
                return redirect(url_for("dashboard"))

        if request.method == "POST":
            action = (request.form.get("action") or "verify").strip().lower()
            email = normalize_email(request.form.get("email") or pending_verification_email())
            form_values["email"] = email
            local_user = User.query.filter_by(username=email).first() if email else None

            if action == "resend":
                if not valid_email(email):
                    form_errors["email"] = email_help
                else:
                    retry_in = otp_retry_seconds(email)
                    if retry_in > 0:
                        flash(f"Please wait {retry_in} seconds before requesting another code.", "warning")
                    else:
                        try:
                            resend_signup_otp(email)
                        except Exception as error:
                            log_supabase_otp_error(email, error)
                            form_errors["email"] = otp_send_error_message(error)
                        else:
                            remember_pending_verification(
                                email,
                                verification_type=verification_type,
                                new_account=bool(session.get("pending_new_account")),
                            )
                            flash("OTP sent to your email.", "success")
                            return redirect(url_for("verify_email"))
            else:
                token = (request.form.get("token") or "").strip()
                form_values["token"] = token
                if not valid_email(email):
                    form_errors["email"] = email_help
                if not token:
                    form_errors["token"] = "Enter the 6-digit OTP from your email."
                if not form_errors:
                    try:
                        supabase = get_supabase_client()
                        auth_response = supabase.auth.verify_otp(
                            {
                                "email": email,
                                "token": token,
                                "type": effective_verification_type,
                            }
                        )
                    except Exception as error:
                        form_errors["token"] = friendly_supabase_error(error, "Invalid OTP.")
                    else:
                        user = ensure_local_user(email, auth_user=response_auth_user(auth_response))
                        new_account = bool(session.get("pending_new_account"))
                        clear_failed_login(email)
                        start_authenticated_session(user, new_account=new_account)
                        flash("Email verified successfully.", "success")
                        return redirect(url_for("dashboard"))

        retry_in = otp_retry_seconds(form_values["email"])
        return render_template(
            "auth/verify_otp.html",
            **build_auth_context(
                "otp_verify",
                form_values,
                form_errors,
                otp_retry_seconds=retry_in,
                verification_email=form_values["email"],
            ),
        )

    @app.post("/auth/password-reset/request-otp")
    def password_reset_request_otp():
        payload = request.get_json(silent=True) if request.is_json else {}
        identifier = normalize_email(
            request.form.get("identifier")
            or request.form.get("email")
            or request.values.get("identifier")
            or request.values.get("email")
            or (payload or {}).get("identifier")
            or (payload or {}).get("email")
            or (payload or {}).get("username")
        )
        user = find_user_by_auth_identifier(identifier)
        if not user or not user.email_verified:
            clear_password_reset_state()
            return {"field": "identifier", "message": "This account is not registered. Please sign up."}, 404

        retry_in = otp_retry_seconds(user.username)
        if retry_in > 0:
            return {"field": "identifier", "message": f"Please wait {retry_in} seconds before requesting another code."}, 429

        try:
            send_password_recovery_otp(user.username)
        except Exception as error:
            log_supabase_otp_error(user.username, error)
            if can_use_local_auth_fallback(error):
                reset_code = remember_local_otp_challenge(user.username, "password_reset")
                session["reset_password_email"] = user.username
                session.pop("reset_password_verified", None)
                remember_otp_request(user.username)
                return {
                    "message": f"{local_auth_status_message()} Local reset code: {reset_code}",
                    "identifier": user.username,
                    "local_code": reset_code,
                }
            return {
                "field": "identifier",
                "message": password_reset_error_message(
                    error,
                    "We could not send a reset OTP right now. Please try again later.",
                ),
            }, 503 if is_timeout_error(error) or is_network_error(error) else 400

        session["reset_password_email"] = user.username
        session.pop("reset_password_verified", None)
        session.pop("local_otp_challenge", None)
        return {"message": "OTP sent.", "identifier": user.username}

    @app.post("/auth/password-reset/verify-otp")
    def password_reset_verify_otp():
        payload = request.get_json(silent=True) if request.is_json else {}
        identifier = normalize_email(
            request.form.get("identifier")
            or request.form.get("email")
            or request.values.get("identifier")
            or request.values.get("email")
            or (payload or {}).get("identifier")
            or (payload or {}).get("email")
            or session.get("reset_password_email")
        )
        otp = (
            request.form.get("otp")
            or request.form.get("token")
            or request.values.get("otp")
            or request.values.get("token")
            or (payload or {}).get("otp")
            or (payload or {}).get("token")
            or ""
        ).strip()
        user = find_user_by_auth_identifier(identifier)
        if not user or not user.email_verified:
            clear_password_reset_state()
            return {"field": "identifier", "message": "This account is not registered. Please sign up."}, 404
        if not re.fullmatch(r"\d{6}", otp):
            return {"field": "otp", "message": "Enter the 6-digit code."}, 422

        if local_auth_fallback_enabled():
            local_verified, local_error = verify_local_otp_challenge(user.username, "password_reset", otp)
            if local_verified is True:
                remember_local_password_reset_verification(user.username)
                return {"message": "Code verified.", "identifier": user.username}
            if local_verified is False:
                return {"field": "otp", "message": local_error or "Invalid or expired code."}, 401

        try:
            supabase = get_supabase_client()
            auth_response = supabase.auth.verify_otp(
                {
                    "email": user.username,
                    "token": otp,
                    "type": "recovery",
                }
            )
            auth_user = response_auth_user(auth_response)
            auth_session = response_auth_session(auth_response)
            if not auth_user or not auth_session:
                return {"field": "otp", "message": "Invalid or expired code."}, 401
            remember_password_reset_verification(user.username, auth_session)
        except Exception as error:
            log_supabase_otp_error(user.username, error)
            message = "Invalid or expired code." if is_invalid_otp_error(error) else password_reset_error_message(
                error,
                "We could not verify your code right now. Please try again later.",
            )
            status_code = 401 if is_invalid_otp_error(error) else 503 if is_timeout_error(error) or is_network_error(error) else 400
            return {"field": "otp", "message": message}, status_code

        return {"message": "OTP verified.", "identifier": user.username}

    @app.post("/auth/password-reset/update")
    def password_reset_update():
        payload = request.get_json(silent=True) if request.is_json else {}
        identifier = normalize_email(
            request.form.get("identifier")
            or request.form.get("email")
            or request.values.get("identifier")
            or request.values.get("email")
            or (payload or {}).get("identifier")
            or (payload or {}).get("email")
            or session.get("reset_password_email")
        )
        password = (
            request.form.get("password")
            or request.values.get("password")
            or (payload or {}).get("password")
            or ""
        )
        confirm_password = (
            request.form.get("confirm_password")
            or request.values.get("confirm_password")
            or (payload or {}).get("confirm_password")
            or ""
        )
        user = find_user_by_auth_identifier(identifier)
        if not user or not user.email_verified:
            clear_password_reset_state()
            return {"field": "identifier", "message": "This account is not registered. Please sign up."}, 404

        verified_state = password_reset_verification_state()
        if normalize_email(verified_state.get("email")) != user.username:
            return {"field": "otp", "message": "Verify your code first."}, 409
        if password != confirm_password:
            return {"field": "confirm_password", "message": "Passwords do not match."}, 422

        password_errors = validate_password_strength(password)
        if password_errors:
            return {"field": "password", "message": " ".join(password_errors)}, 422

        if verified_state.get("method") == "local_otp":
            ensure_local_user(user.username, password=password, full_name=user.full_name)
            clear_password_reset_state()
            return {
                "message": "Password updated successfully.",
                "redirect_url": url_for("login"),
            }

        try:
            supabase = get_supabase_client()
            auth_response = supabase.auth.set_session(
                verified_state.get("access_token"),
                verified_state.get("refresh_token"),
            )
            auth_user = response_auth_user(auth_response)
            update_response = supabase.auth.update_user({"password": password})
            auth_user = response_auth_user(update_response) or auth_user
        except Exception as error:
            log_supabase_otp_error(user.username, error)
            return {
                "field": "password",
                "message": password_reset_error_message(
                    error,
                    "We could not update your password right now. Please try again later.",
                ),
            }, 503 if is_timeout_error(error) or is_network_error(error) else 400

        ensure_local_user(user.username, password=password, auth_user=auth_user)
        clear_password_reset_state()
        return {
            "message": "Password updated successfully.",
            "redirect_url": url_for("login"),
        }

    @app.route("/forgot-password", methods=["GET", "POST"])
    def forgot_password():
        redirect_response = redirect_authenticated_user()
        if redirect_response:
            return redirect_response

        form_values = {"identifier": normalize_email(session.get("reset_password_email") or pending_verification_email())}
        if request.method == "POST":
            return redirect(url_for("forgot_password"))

        return render_template("auth/forgot_password.html", **build_auth_context("forgot_password", form_values))

    @app.route("/reset-password", methods=["GET", "POST"])
    def reset_password():
        redirect_response = redirect_authenticated_user()
        if redirect_response:
            return redirect_response

        query_token_hash = (request.args.get("token_hash") or "").strip()
        if request.method == "GET" and not query_token_hash:
            return redirect(url_for("forgot_password"))
        if query_token_hash:
            session["reset_token_hash"] = query_token_hash

        form_values = {
            "token_hash": session.get("reset_token_hash", ""),
            "email": normalize_email(session.get("reset_password_email", "")),
            "otp": "",
        }
        form_errors = {}

        if request.method == "POST":
            token_hash = (request.form.get("token_hash") or session.get("reset_token_hash") or "").strip()
            email = normalize_email(request.form.get("email") or session.get("reset_password_email"))
            otp = (request.form.get("otp") or "").strip()
            password = request.form.get("password") or ""
            confirm_password = request.form.get("confirm_password") or ""
            form_values["token_hash"] = token_hash
            form_values["email"] = email
            form_values["otp"] = otp

            if password != confirm_password:
                form_errors["confirm_password"] = "Passwords do not match."
            password_errors = validate_password_strength(password)
            if password_errors:
                form_errors["password"] = " ".join(password_errors)
            if not token_hash:
                if not valid_email(email):
                    form_errors["email"] = email_help
                elif not User.query.filter_by(username=email).first():
                    form_errors["email"] = "No HormonaCare account was found for that email."
                if not otp:
                    form_errors["otp"] = "Enter the 6-digit OTP from your email."
            if form_errors:
                return render_template("auth/reset_password.html", **build_auth_context("reset_password", form_values, form_errors))

            try:
                supabase = get_supabase_client()
                if token_hash:
                    recovery_response = supabase.auth.verify_otp(
                        {
                            "token_hash": token_hash,
                            "type": "recovery",
                        }
                    )
                    auth_user = response_auth_user(recovery_response)
                    email = response_user_email(recovery_response)
                else:
                    recovery_response = supabase.auth.verify_otp(
                        {
                            "email": email,
                            "token": otp,
                            "type": "recovery",
                        }
                    )
                    auth_user = response_auth_user(recovery_response)
                supabase.auth.update_user({"password": password})
            except Exception as error:
                log_supabase_otp_error(email or "password-reset", error)
                error_message = password_reset_error_message(
                    error,
                    "We could not reset your password with that OTP. Please request a new reset code.",
                )
                if token_hash:
                    form_errors["password"] = error_message
                else:
                    form_errors["otp"] = error_message
                return render_template("auth/reset_password.html", **build_auth_context("reset_password", form_values, form_errors))

            if email:
                ensure_local_user(email, password=password, auth_user=auth_user)

            session.pop("reset_token_hash", None)
            session.pop("reset_password_email", None)
            flash("Your password has been updated. Please sign in.", "success")
            return redirect(url_for("login"))

        return render_template("auth/reset_password.html", **build_auth_context("reset_password", form_values, form_errors))

    @app.route("/logout")
    def logout():
        session.clear()
        flash("You have been logged out.", "info")
        return redirect(url_for("login"))

    @app.route("/dashboard")
    @login_required
    def dashboard():
        return render_template(
            "dashboard.html",
            metrics=dashboard_metrics(current_user()),
            is_new_account=bool(session.pop("new_account", False)),
        )

    @app.get("/api/health")
    def api_health():
        return api_success(
            data={
                "service": "HormonaCare Core Service",
                "backend_language": "Python",
                "api_version": api_version,
                "auth_mode": "session_cookie",
                "web_push_available": webpush is not None,
                "runtime_ready": runtime_init_complete,
                "database_configured": not bool(app.config.get("HORMONACARE_DATABASE_WARNING")),
                "database_warning": app.config.get("HORMONACARE_DATABASE_WARNING", ""),
            }
        )

    @app.get("/api/docs")
    def api_docs():
        return api_success(data=build_api_docs())

    @app.get("/api/gateway")
    def api_gateway_status():
        return api_success(
            data={
                "gateway": api_gateway_name,
                "type": "application_level_gateway",
                "implementation": "Flask before_request gateway entrypoint plus centralized API route catalog",
                "separate_managed_gateway": False,
                "core_service": "Python Flask",
                "responsibilities": [
                    "central API request entrypoint",
                    "route catalog and API documentation",
                    "session authentication checks for protected API routes",
                    "email verification enforcement for protected API routes",
                    "standard JSON success and error responses",
                    "API identity headers",
                    "communication with Supabase PostgreSQL through the Flask core service",
                ],
                "deployment_note": "This is not AWS API Gateway. Amazon EC2 hosts the Flask core service, and Render may be used only as a backup or testing deployment.",
            }
        )

    @app.post("/api/auth/register")
    def api_register():
        payload = api_json_body()
        if payload is None:
            return api_error("invalid_request", "Expected a JSON request body.", status=415)

        full_name = normalize_full_name(payload.get("full_name"))
        email = normalize_email(payload.get("email") or payload.get("username"))
        password = payload.get("password") or ""
        confirm_password = payload.get("confirm_password") or ""
        field_errors = {}

        if not valid_full_name(full_name):
            field_errors["full_name"] = full_name_help
        if not valid_email(email):
            field_errors["email"] = email_help
        if not password:
            field_errors["password"] = "Password is required."
        if password != confirm_password:
            field_errors["confirm_password"] = "Passwords do not match."
        password_errors = validate_password_strength(password)
        if password_errors:
            field_errors["password"] = " ".join(password_errors)
        existing_user = User.query.filter_by(username=email).first() if email else None
        if existing_user:
            if existing_user.email_verified:
                field_errors["email"] = email_taken
            else:
                db.session.delete(existing_user)
                db.session.commit()
                existing_user = None
        if field_errors:
            return api_error("validation_error", "Account registration failed.", status=422, details=field_errors)

        try:
            auth_response = register_supabase_password_account(email, password, full_name)
        except Exception as error:
            return api_error(
                "supabase_auth_error",
                "Supabase Auth registration failed.",
                status=502,
                details={"email": friendly_supabase_error(error, "Could not create the Supabase Auth user.")},
            )

        user = ensure_local_user(
            email,
            password=password,
            full_name=full_name,
            auth_user=response_auth_user(auth_response),
        )
        remember_pending_verification(email, new_account=True)
        remember_otp_request(email)
        return api_success(
            data={"user": serialize_auth_user(user), "verification_required": True},
            message="Account created. OTP sent to your email.",
            status=201,
        )

    @app.post("/api/auth/login")
    def api_login():
        payload = api_json_body()
        if payload is None:
            return api_error("invalid_request", "Expected a JSON request body.", status=415)

        email = normalize_email(payload.get("email") or payload.get("username"))
        password = payload.get("password") or ""
        field_errors = {}
        if not email:
            field_errors["email"] = "Email is required."
        elif not valid_email(email):
            field_errors["email"] = email_help
        if not password:
            field_errors["password"] = "Password is required."
        if field_errors:
            return api_error("validation_error", "Email and password are required.", status=422, details=field_errors)

        lock_message = check_login_rate_limit(email)
        if lock_message:
            return api_error("rate_limited_login", lock_message, status=429)

        local_user = User.query.filter_by(username=email).first()
        try:
            supabase = get_supabase_client()
            auth_response = supabase.auth.sign_in_with_password({"email": email, "password": password})
        except Exception as error:
            if is_unverified_email_error(error):
                clear_failed_login(email)
                remember_pending_verification(email)
                if otp_retry_seconds(email) == 0:
                    try:
                        resend_signup_otp(email)
                    except Exception:
                        pass
                return api_error("email_verification_required", "Please verify your email before logging in.", status=403)
            if is_rate_limited_error(error):
                return api_error("rate_limited_login", friendly_supabase_error(error, lock_message), status=429)
            if is_timeout_error(error) or is_network_error(error) or can_use_local_auth_fallback(error):
                if (
                    can_use_local_auth_fallback(error)
                    and local_user
                    and local_user.email_verified
                    and verify_password(password, local_user.password_hash)
                ):
                    clear_failed_login(email)
                    start_authenticated_session(local_user)
                    return api_success(
                        data={"user": serialize_auth_user(local_user), "local_auth_fallback": True},
                        message="Login successful.",
                    )
                return api_error("service_unavailable", "Service temporarily unavailable. Please try again later.", status=503)
            record_failed_login(email)
            if local_user and not local_user.supabase_user_id and verify_password(password, local_user.password_hash):
                try:
                    bootstrap_response = register_supabase_password_account(email, password, local_user.full_name)
                except Exception:
                    return api_error("invalid_credentials", "Invalid email or password.", status=401)
                ensure_local_user(
                    email,
                    password=password,
                    full_name=local_user.full_name,
                    auth_user=response_auth_user(bootstrap_response),
                )
                remember_pending_verification(email)
                remember_otp_request(email)
                return api_error("email_verification_required", "OTP sent to your email. Verify it before logging in.", status=403)
            return api_error("invalid_credentials", "Invalid email or password.", status=401)

        clear_failed_login(email)
        user = ensure_local_user(
            email,
            password=password,
            full_name=local_user.full_name if local_user else "",
            auth_user=response_auth_user(auth_response),
        )
        if not user.email_verified:
            remember_pending_verification(email)
            return api_error("email_verification_required", "Please verify your email before logging in.", status=403)

        start_authenticated_session(user)
        return api_success(
            data={"user": serialize_auth_user(user)},
            message="Login successful.",
        )

    @app.post("/api/auth/logout")
    @api_login_required
    def api_logout():
        session.clear()
        return api_success(message="Logged out successfully.")

    @app.get("/api/me")
    @api_login_required
    def api_me():
        user = current_user()
        return api_success(data=serialize_auth_user(user))

    def sync_field(fields, name, default=""):
        value = fields.get(name, default)
        if isinstance(value, list):
            return value[-1] if value else default
        return default if value is None else value

    def sync_bool(fields, name):
        value = sync_field(fields, name, "")
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def sync_client_timestamp(value):
        raw_value = str(value or "").strip()
        if not raw_value:
            return datetime.utcnow()
        if raw_value.endswith("Z"):
            raw_value = raw_value[:-1] + "+00:00"
        try:
            parsed_value = datetime.fromisoformat(raw_value)
        except ValueError:
            return datetime.utcnow()
        if parsed_value.tzinfo:
            parsed_value = parsed_value.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed_value

    def sync_record_is_stale(record, client_updated_at):
        stored_updated_at = getattr(record, "client_updated_at", None)
        return bool(stored_updated_at and client_updated_at and client_updated_at < stored_updated_at)

    def sync_success(sync_id, record_type, record_id=None, status="synced"):
        payload = {"client_id": sync_id, "type": record_type, "status": status}
        if record_id is not None:
            payload["server_id"] = record_id
        return payload

    def apply_offline_sync_item(user, item):
        sync_id = str(item.get("client_id") or item.get("id") or "").strip()[:80]
        request_path = urlparse(str(item.get("path") or "")).path
        fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
        client_updated_at = sync_client_timestamp(item.get("updated_at") or item.get("queued_at"))

        if not sync_id:
            return {"status": "failed", "error": "missing_client_id"}
        if not request_path:
            return {"client_id": sync_id, "status": "failed", "error": "missing_path"}
        if any(marker in request_path for marker in ("/delete", "/logout")) or request_path == "/settings/password":
            return {"client_id": sync_id, "status": "failed", "error": "unsupported_destructive_or_security_flow"}

        if request_path == "/lifestyle/water":
            log = get_or_create_today_lifestyle_log(user)
            if sync_record_is_stale(log, client_updated_at):
                return sync_success(sync_id, "lifestyle_water", log.id, status="stale_ignored")
            glasses = max(0, min(8, parse_int(sync_field(fields, "glasses"), 0)))
            log.water_intake_liters = round(glasses * 0.25, 2)
            log.client_sync_id = sync_id
            log.client_updated_at = client_updated_at
            return sync_success(sync_id, "lifestyle_water", log.id)

        if request_path == "/lifestyle/sleep":
            log = get_or_create_today_lifestyle_log(user)
            if sync_record_is_stale(log, client_updated_at):
                return sync_success(sync_id, "lifestyle_sleep", log.id, status="stale_ignored")
            log.sleep_hours = max(0, min(24, parse_float(sync_field(fields, "sleep_hours"), 0)))
            log.client_sync_id = sync_id
            log.client_updated_at = client_updated_at
            return sync_success(sync_id, "lifestyle_sleep", log.id)

        if request_path == "/lifestyle/exercise":
            log = get_or_create_today_lifestyle_log(user)
            if sync_record_is_stale(log, client_updated_at):
                return sync_success(sync_id, "lifestyle_exercise", log.id, status="stale_ignored")
            evaluation = evaluate_exercise_entry(
                sync_field(fields, "activity_type", "").strip(),
                parse_int(sync_field(fields, "duration_minutes") or sync_field(fields, "exercise_minutes"), -1),
                sync_field(fields, "intensity", "").strip().lower(),
            )
            if not evaluation["ok"]:
                return {"client_id": sync_id, "status": "failed", "error": "validation_error", "details": evaluation["errors"]}
            parsed_notes = parse_lifestyle_notes(log.notes)
            parsed_notes["exercise_entries"].append(evaluation["entry"])
            log.exercise_minutes = evaluation["entry"]["raw_input"]["duration_minutes"]
            log.notes = encrypt_text(
                serialize_lifestyle_notes(
                    general_notes=parsed_notes["general_notes"],
                    exercise_entries=parsed_notes["exercise_entries"],
                    food_entries=parsed_notes["food_entries"],
                )
            )
            log.client_sync_id = sync_id
            log.client_updated_at = client_updated_at
            return sync_success(sync_id, "lifestyle_exercise", log.id)

        if request_path == "/lifestyle/quick-food":
            log = get_or_create_today_lifestyle_log(user)
            if sync_record_is_stale(log, client_updated_at):
                return sync_success(sync_id, "lifestyle_food", log.id, status="stale_ignored")
            evaluation = evaluate_food_entry(
                sync_field(fields, "food_name", "").strip() or sync_field(fields, "meal_text", "").strip(),
                sync_field(fields, "portion_size", "").strip().lower() or ("medium" if sync_field(fields, "meal_text", "").strip() else ""),
                sync_field(fields, "food_category", "").strip().lower() or ("balanced" if sync_field(fields, "meal_text", "").strip() else ""),
            )
            if not evaluation["ok"]:
                return {"client_id": sync_id, "status": "failed", "error": "validation_error", "details": evaluation["errors"]}
            parsed_notes = parse_lifestyle_notes(log.notes)
            parsed_notes["food_entries"].append(evaluation["entry"])
            category_labels = {
                "high sugar": "High Sugar",
                "protein-rich": "Protein-Rich",
                "balanced": "Balanced",
                "fast food": "Fast Food",
            }
            log.diet_quality = category_labels.get(evaluation["entry"]["raw_input"]["food_category"], log.diet_quality)
            log.notes = encrypt_text(
                serialize_lifestyle_notes(
                    general_notes=parsed_notes["general_notes"],
                    exercise_entries=parsed_notes["exercise_entries"],
                    food_entries=parsed_notes["food_entries"],
                )
            )
            log.client_sync_id = sync_id
            log.client_updated_at = client_updated_at
            return sync_success(sync_id, "lifestyle_food", log.id)

        if request_path == "/mental-health":
            log_date, log_date_error = parse_form_date(sync_field(fields, "log_date"), "Log date")
            if log_date_error:
                return {"client_id": sync_id, "status": "failed", "error": "validation_error", "details": [log_date_error]}
            log = MentalLog.query.filter_by(user_id=user.id, log_date=log_date).first()
            if not log:
                log = MentalLog(user_id=user.id, log_date=log_date)
                db.session.add(log)
            if sync_record_is_stale(log, client_updated_at):
                return sync_success(sync_id, "mental_health", log.id, status="stale_ignored")
            mood = sync_field(fields, "mood", "Okay")
            slider_stress = max(1, min(5, parse_int(sync_field(fields, "stress_level"), 3)))
            stress_level = min(10, slider_stress * 2)
            log.mood = mood
            log.stress_level = stress_level
            log.wellness_tip = build_mental_tip(mood, stress_level)
            log.client_sync_id = sync_id
            log.client_updated_at = client_updated_at
            return sync_success(sync_id, "mental_health", log.id)

        if request_path == "/calendar":
            log_date, log_date_error = parse_form_date(sync_field(fields, "log_date"), "Log date")
            if log_date_error:
                return {"client_id": sync_id, "status": "failed", "error": "validation_error", "details": [log_date_error]}
            if sync_bool(fields, "delete_day_log"):
                return {"client_id": sync_id, "status": "failed", "error": "offline_delete_not_supported"}
            existing_logs = CycleLog.query.filter_by(user_id=user.id).order_by(CycleLog.log_date.asc()).all()
            period_start = sync_bool(fields, "period_start") or sync_bool(fields, "mark_period_start")
            end_period = sync_bool(fields, "end_period")
            save_day_details = sync_bool(fields, "save_day_details") or end_period
            cycle_day_value = resolve_cycle_day_value(
                log_date,
                cycle_model(existing_logs),
                requested_cycle_day=parse_int(sync_field(fields, "cycle_day"), default=0),
                mark_period_start=period_start,
            )
            cycle_log = CycleLog.query.filter_by(user_id=user.id, log_date=log_date).first()
            if not cycle_log:
                cycle_log = CycleLog(user_id=user.id, log_date=log_date)
                db.session.add(cycle_log)
            if sync_record_is_stale(cycle_log, client_updated_at):
                return sync_success(sync_id, "cycle_log", cycle_log.id, status="stale_ignored")
            cycle_log.cycle_day = cycle_day_value
            cycle_log.period_start = period_start
            if save_day_details:
                cycle_log.symptoms = pack_cycle_details(sync_field(fields, "symptoms", ""), sync_field(fields, "notes", ""))
                cycle_log.flow_level = "No Flow" if end_period else normalize_flow_level(sync_field(fields, "flow_level", ""))
            elif is_explicit_no_flow(cycle_log.flow_level):
                cycle_log.flow_level = ""
            cycle_log.client_sync_id = sync_id
            cycle_log.client_updated_at = client_updated_at
            return sync_success(sync_id, "cycle_log", cycle_log.id)

        if request_path == "/medications":
            medication = Medication.query.filter_by(user_id=user.id, client_sync_id=sync_id).first()
            if not medication:
                medication = Medication(user_id=user.id, client_sync_id=sync_id)
                db.session.add(medication)
            if sync_record_is_stale(medication, client_updated_at):
                return sync_success(sync_id, "medication", medication.id, status="stale_ignored")
            medication.name = sync_field(fields, "name", "").strip()
            medication.dosage = sync_field(fields, "dosage", "").strip()
            medication.time_of_day = datetime.strptime(sync_field(fields, "time_of_day"), "%H:%M").time()
            medication.notes = encrypt_text(sync_field(fields, "notes", "").strip())
            medication.status = "pending"
            medication.reminder_enabled = sync_bool(fields, "reminder_enabled")
            medication.client_updated_at = client_updated_at
            return sync_success(sync_id, "medication", medication.id)

        medication_status_match = re.fullmatch(r"/medications/(\d+)/status", request_path)
        if medication_status_match:
            medication = owned_record_or_404(Medication, user, int(medication_status_match.group(1)))
            status_value = sync_field(fields, "status", "").strip().lower()
            if status_value not in {"pending", "taken", "skipped", "missed"}:
                return {"client_id": sync_id, "status": "failed", "error": "invalid_medication_status"}
            existing_log = MedicationLog.query.filter_by(user_id=user.id, client_sync_id=sync_id).first()
            if existing_log and sync_record_is_stale(existing_log, client_updated_at):
                return sync_success(sync_id, "medication_status", existing_log.id, status="stale_ignored")
            replace_medication_day_event(
                user,
                medication,
                status_value if status_value in MEDICATION_EVENT_STATUSES else None,
                event_time=client_updated_at or app_now(),
            )
            latest_log = (
                MedicationLog.query.filter_by(user_id=user.id, medication_id=medication.id)
                .order_by(MedicationLog.taken_at.desc())
                .first()
            )
            if latest_log:
                latest_log.client_sync_id = sync_id
                latest_log.client_updated_at = client_updated_at
            return sync_success(sync_id, "medication_status", latest_log.id if latest_log else medication.id)

        if request_path == "/appointments":
            appointment_id = parse_int(sync_field(fields, "appointment_id"), default=0)
            appointment = None
            if appointment_id:
                appointment = user_records(Appointment, user).filter_by(id=appointment_id).first()
            if not appointment:
                appointment = Appointment.query.filter_by(user_id=user.id, client_sync_id=sync_id).first()
            if not appointment:
                appointment = Appointment(user_id=user.id, client_sync_id=sync_id)
                db.session.add(appointment)
            if sync_record_is_stale(appointment, client_updated_at):
                return sync_success(sync_id, "appointment", appointment.id, status="stale_ignored")
            appointment_date, appointment_date_error = parse_form_date(sync_field(fields, "appointment_date"), "Appointment date")
            appointment_time, appointment_time_error = parse_form_time(sync_field(fields, "appointment_time"), "Appointment time")
            follow_up_date, follow_up_date_error = parse_form_date(sync_field(fields, "follow_up_date"), "Follow-up date", required=False)
            validation_errors = [
                error for error in [
                    "Doctor name is required." if not sync_field(fields, "doctor_name", "").strip() else None,
                    "Specialty is required." if not sync_field(fields, "specialty", "").strip() else None,
                    "Location is required." if not sync_field(fields, "location", "").strip() else None,
                    appointment_date_error,
                    appointment_time_error,
                    follow_up_date_error,
                ]
                if error
            ]
            if validation_errors:
                return {"client_id": sync_id, "status": "failed", "error": "validation_error", "details": validation_errors}
            existing_status = unpack_appointment_notes(appointment.notes).get("status", "scheduled") if appointment.notes else "scheduled"
            appointment.doctor_name = sync_field(fields, "doctor_name", "").strip()
            appointment.appointment_date = appointment_date
            appointment.appointment_time = appointment_time
            appointment.notes = pack_appointment_notes(
                specialty=sync_field(fields, "specialty", "").strip(),
                location=sync_field(fields, "location", "").strip(),
                reminder_enabled=sync_bool(fields, "reminder_enabled"),
                status=existing_status,
                notes_text=sync_field(fields, "notes", "").strip(),
            )
            appointment.prescription = encrypt_text(sync_field(fields, "prescription", "").strip())
            appointment.follow_up_date = follow_up_date
            appointment.client_updated_at = client_updated_at
            return sync_success(sync_id, "appointment", appointment.id)

        if request_path == "/profile/personal-info":
            profile = get_or_create_profile(user)
            age = parse_profile_age(sync_field(fields, "age", ""))
            diagnosis_date, diagnosis_error = parse_form_date(sync_field(fields, "diagnosis_date"), "Diagnosed date", required=False)
            if diagnosis_error:
                return {"client_id": sync_id, "status": "failed", "error": "validation_error", "details": [diagnosis_error]}
            user.full_name = normalize_full_name(sync_field(fields, "full_name", user.full_name))
            profile.age = age
            profile.diagnosis_date = diagnosis_date
            return sync_success(sync_id, "profile", profile.id)

        if request_path == "/settings":
            profile = get_or_create_profile(user)
            user.full_name = normalize_full_name(sync_field(fields, "full_name", user.full_name))
            profile.dark_mode = sync_field(fields, "dark_mode", "0") == "1"
            profile.general_notifications = sync_bool(fields, "general_notifications")
            return sync_success(sync_id, "settings", profile.id)

        return {"client_id": sync_id, "status": "failed", "error": "unsupported_sync_path", "path": request_path}

    @app.post("/api/sync/batch")
    @api_login_required
    def api_sync_batch():
        payload = api_json_body()
        if payload is None:
            return api_error("invalid_request", "Expected a JSON request body.", status=415)
        records = payload.get("records")
        if not isinstance(records, list):
            return api_error("validation_error", "The sync request must include a records array.", status=422)
        if len(records) > 50:
            return api_error("validation_error", "A sync batch may contain up to 50 records.", status=422)

        user = current_user()
        results = []
        for item in records:
            if not isinstance(item, dict):
                results.append({"status": "failed", "error": "invalid_record"})
                continue
            try:
                results.append(apply_offline_sync_item(user, item))
                db.session.flush()
                db.session.commit()
            except Exception as error:
                db.session.rollback()
                app.logger.exception("Offline sync item failed.")
                results.append(
                    {
                        "client_id": str(item.get("client_id") or item.get("id") or ""),
                        "status": "failed",
                        "error": "sync_exception",
                        "message": str(error),
                    }
                )
        synced_count = sum(1 for result in results if result.get("status") in {"synced", "stale_ignored"})
        failed_count = sum(1 for result in results if result.get("status") == "failed")
        return api_success(
            data={"results": results},
            message="Offline sync processed.",
            meta={
                "received": len(records),
                "synced": synced_count,
                "failed": failed_count,
                "conflict_policy": "latest_client_update_wins; older queued records are ignored when a newer server/client timestamp exists",
            },
        )

    @app.get("/api/dashboard")
    @api_login_required
    def api_dashboard():
        return api_success(data=serialize_dashboard_metrics(dashboard_metrics(current_user())))

    @app.route("/profile")
    @login_required
    def profile():
        return render_template(
            "profile.html",
            **profile_summary(current_user()),
        )

    @app.post("/profile/personal-info")
    @login_required
    def profile_personal_info():
        user = current_user()
        profile = get_or_create_profile(user)
        full_name = (request.form.get("full_name") or "").strip()
        age, age_error = parse_profile_age(request.form.get("age"))
        diagnosis_date, diagnosis_error = parse_form_date(request.form.get("diagnosis_date"), "Diagnosed date", required=False)
        form_errors = []

        if not full_name:
            form_errors.append("Full name is required.")
        elif len(full_name) > 120:
            form_errors.append("Full name must be 120 characters or fewer.")
        if age_error:
            form_errors.append(age_error)
        if diagnosis_error:
            form_errors.append(diagnosis_error)
        elif diagnosis_date and diagnosis_date > date.today():
            form_errors.append("Diagnosed date cannot be in the future.")

        if form_errors:
            for error in form_errors:
                flash(error, "danger")
            return redirect(url_for("profile"))

        user.full_name = full_name
        profile.age = age
        profile.diagnosis_date = diagnosis_date
        db.session.commit()
        flash("Personal information updated.", "success")
        return redirect(url_for("profile"))

    @app.get("/api/profile")
    @api_login_required
    def api_profile():
        return api_success(data=serialize_profile_summary(profile_summary(current_user())))

    @app.post("/settings/password/verify-current")
    @login_required
    def settings_password_verify_current():
        user = current_user()
        payload = request.get_json(silent=True) if request.is_json else {}
        current_password = (
            request.form.get("current_password")
            or request.values.get("current_password")
            or (payload or {}).get("current_password")
            or ""
        )
        if not current_password:
            clear_settings_password_state()
            return {"field": "current_password", "message": "Enter your current password."}, 422

        try:
            supabase = get_supabase_client()
            auth_response = supabase.auth.sign_in_with_password({"email": user.username, "password": current_password})
            auth_session = response_auth_session(auth_response)
            auth_user = response_auth_user(auth_response)
            if not auth_session:
                clear_settings_password_state()
                return {"field": "current_password", "message": "Current password is incorrect."}, 401
            remember_settings_password_verification(user.username, auth_session, "current_password")
            ensure_local_user(user.username, full_name=user.full_name, auth_user=auth_user)
        except Exception as error:
            log_supabase_otp_error(user.username, error)
            if (
                can_use_local_auth_fallback(error)
                and verify_password(current_password, user.password_hash)
            ):
                remember_local_settings_password_verification(user.username, "local_current_password")
                return {"message": "Current password verified."}
            clear_settings_password_state()
            message = (
                "Current password is incorrect."
                if is_invalid_login_error(error)
                else friendly_supabase_error(error, "We could not verify your current password right now.")
            )
            return {"field": "current_password", "message": message}, 401 if is_invalid_login_error(error) else 503 if is_timeout_error(error) or is_network_error(error) else 400

        return {"message": "Current password verified."}

    @app.post("/settings/password/send-otp")
    @login_required
    def settings_password_send_otp():
        user = current_user()
        retry_in = otp_retry_seconds(user.username)
        if retry_in > 0:
            return {"field": "otp", "message": f"Please wait {retry_in} seconds before requesting another code."}, 429

        clear_settings_password_state()
        try:
            send_password_recovery_otp(user.username)
        except Exception as error:
            log_supabase_otp_error(user.username, error)
            if can_use_local_auth_fallback(error):
                reset_code = remember_local_otp_challenge(user.username, "settings_password")
                remember_otp_request(user.username)
                return {
                    "message": f"{local_auth_status_message()} Local password code: {reset_code}",
                    "email": user.username,
                    "local_code": reset_code,
                }
            return {
                "field": "otp",
                "message": password_reset_error_message(
                    error,
                    "We could not send an OTP right now. Please try again later.",
                ),
            }, 503 if is_timeout_error(error) or is_network_error(error) else 400

        session.pop("local_otp_challenge", None)
        return {"message": "A 6-digit code has been sent to your email.", "email": user.username}

    @app.post("/settings/password/verify-otp")
    @login_required
    def settings_password_verify_otp():
        user = current_user()
        payload = request.get_json(silent=True) if request.is_json else {}
        otp = (
            request.form.get("otp")
            or request.form.get("token")
            or request.values.get("otp")
            or request.values.get("token")
            or (payload or {}).get("otp")
            or (payload or {}).get("token")
            or ""
        ).strip()
        if not re.fullmatch(r"\d{6}", otp):
            clear_settings_password_state()
            return {"field": "otp", "message": "Enter the 6-digit code."}, 422

        if local_auth_fallback_enabled():
            local_verified, local_error = verify_local_otp_challenge(user.username, "settings_password", otp)
            if local_verified is True:
                remember_local_settings_password_verification(user.username, "local_otp")
                return {"message": "OTP verified."}
            if local_verified is False:
                clear_settings_password_state()
                return {"field": "otp", "message": local_error or "Invalid or expired code."}, 401

        try:
            supabase = get_supabase_client()
            auth_response = supabase.auth.verify_otp(
                {
                    "email": user.username,
                    "token": otp,
                    "type": "recovery",
                }
            )
            auth_user = response_auth_user(auth_response)
            auth_session = response_auth_session(auth_response)
            if not auth_user or not auth_session:
                clear_settings_password_state()
                return {"field": "otp", "message": "Invalid or expired code."}, 401
            remember_settings_password_verification(user.username, auth_session, "otp")
            ensure_local_user(user.username, full_name=user.full_name, auth_user=auth_user)
        except Exception as error:
            clear_settings_password_state()
            log_supabase_otp_error(user.username, error)
            message = (
                "Invalid or expired code."
                if is_invalid_otp_error(error)
                else password_reset_error_message(
                    error,
                    "We could not verify your code right now. Please try again later.",
                )
            )
            return {"field": "otp", "message": message}, 401 if is_invalid_otp_error(error) else 503 if is_timeout_error(error) or is_network_error(error) else 400

        return {"message": "OTP verified."}

    @app.post("/settings/password/update")
    @login_required
    def settings_password_update():
        user = current_user()
        payload = request.get_json(silent=True) if request.is_json else {}
        password = (
            request.form.get("password")
            or request.values.get("password")
            or (payload or {}).get("password")
            or ""
        )
        confirm_password = (
            request.form.get("confirm_password")
            or request.values.get("confirm_password")
            or (payload or {}).get("confirm_password")
            or ""
        )
        verified_state = settings_password_verification_state()
        if normalize_email(verified_state.get("email")) != user.username or verified_state.get("method") not in {"current_password", "otp", "local_current_password", "local_otp"}:
            clear_settings_password_state()
            return {"field": "otp", "message": "Verify your chosen method first."}, 409
        if password != confirm_password:
            return {"field": "confirm_password", "message": "Passwords do not match."}, 422

        password_errors = validate_password_strength(password)
        if password_errors:
            return {"field": "password", "message": " ".join(password_errors)}, 422
        if verify_password(password, user.password_hash):
            return {"field": "password", "message": "New password must be different from your current password."}, 422

        if verified_state.get("method") in {"local_current_password", "local_otp"}:
            ensure_local_user(
                user.username,
                password=password,
                full_name=user.full_name,
            )
            clear_settings_password_state()
            return {"message": "Password changed successfully."}

        try:
            supabase = get_supabase_client()
            auth_response = supabase.auth.set_session(
                verified_state.get("access_token"),
                verified_state.get("refresh_token"),
            )
            auth_user = response_auth_user(auth_response)
            update_response = supabase.auth.update_user({"password": password})
            auth_user = response_auth_user(update_response) or auth_user
        except Exception as error:
            log_supabase_otp_error(user.username, error)
            return {
                "field": "password",
                "message": password_reset_error_message(
                    error,
                    "We could not update your password right now. Please try again later.",
                ),
            }, 503 if is_timeout_error(error) or is_network_error(error) else 400

        ensure_local_user(
            user.username,
            password=password,
            full_name=user.full_name,
            auth_user=auth_user,
        )
        clear_settings_password_state()
        return {"message": "Password changed successfully."}

    @app.route("/settings", methods=["GET", "POST"])
    @login_required
    def settings():
        user = current_user()
        profile = get_or_create_profile(user)
        if request.method == "POST":
            requested_full_name = normalize_full_name(request.form.get("full_name"))
            requested_email = normalize_email(request.form.get("username"))
            if not valid_full_name(requested_full_name):
                flash(full_name_help, "danger")
                return redirect(url_for("settings"))
            if not valid_email(requested_email):
                flash(email_help, "danger")
                return redirect(url_for("settings"))
            if requested_email != user.username:
                flash("Email changes are disabled in this demo so verification stays consistent.", "warning")
                return redirect(url_for("settings"))
            existing_user = User.query.filter(User.username == requested_email, User.id != user.id).first()
            if existing_user:
                flash(email_taken, "danger")
                return redirect(url_for("settings"))

            user.full_name = requested_full_name
            user.username = requested_email
            profile.dark_mode = request.form.get("dark_mode", "0") == "1"
            profile.general_notifications = request.form.get("general_notifications") == "1"
            profile.notify_meds = True
            profile.notify_appointments = True
            profile.notify_alerts = True
            db.session.commit()
            flash("Settings updated.", "success")
            return redirect(url_for("settings"))
        clear_settings_password_state()
        return render_template(
            "settings.html",
            profile=profile,
        )

    @app.get("/api/cycle")
    @api_login_required
    def api_cycle():
        user = current_user()
        selected_raw = request.args.get("date", "").strip()
        reference_date = datetime.strptime(selected_raw, "%Y-%m-%d").date() if selected_raw else date.today()
        return api_success(data=serialize_cycle_info(get_cycle_info(user, reference_date)))

    @app.get("/api/alerts")
    @api_login_required
    def api_alerts():
        payload = build_alerts_context(current_user())
        related_insight = None
        if payload.get("sleep_mood_insight"):
            related_insight = {
                **payload["sleep_mood_insight"],
                "sleep_hours": payload.get("sleep_hours_for_insight"),
            }
        return api_success(data=serialize_alerts_payload(payload["score"], payload["recent_patterns"], payload["active_alerts"], payload["risk_indicator"], related_insight=related_insight))

    @app.get("/api/medications")
    @api_login_required
    def api_medications():
        user = current_user()
        medications = Medication.query.filter_by(user_id=user.id).order_by(Medication.time_of_day.asc()).all()
        normalize_medication_statuses(user, medications)
        decrypt_model_fields(medications, ["notes"])
        return api_success(
            data=[
                {
                    "id": medication.id,
                    "name": medication.name,
                    "dosage": medication.dosage,
                    "time_of_day": iso_time(medication.time_of_day),
                    "notes": medication.notes,
                    "status": medication.status,
                    "daily_status": getattr(medication, "daily_status", medication.status),
                    "daily_status_label": getattr(medication, "daily_status_label", medication.status.title()),
                    "daily_status_tone": getattr(medication, "daily_status_tone", "neutral"),
                    "daily_status_message": getattr(medication, "daily_status_message", ""),
                    "event_status": getattr(medication, "daily_event_status", None),
                    "reminder_enabled": safe_bool(medication.reminder_enabled),
                }
                for medication in medications
            ]
        )

    @app.get("/api/lifestyle")
    @api_login_required
    def api_lifestyle():
        user = current_user()
        logs = LifestyleLog.query.filter_by(user_id=user.id).order_by(LifestyleLog.log_date.desc()).limit(14).all()
        decrypt_model_fields(logs, ["notes"])
        return api_success(
            data=[
                {
                    "id": log.id,
                    "log_date": iso_date(log.log_date),
                    "sleep_hours": log.sleep_hours,
                    "water_intake_liters": log.water_intake_liters,
                    "diet_quality": log.diet_quality,
                    "exercise_minutes": log.exercise_minutes,
                    "notes": extract_general_notes(log.notes),
                    "exercise_feedback": parse_lifestyle_notes(log.notes)["exercise_entries"][-1] if parse_lifestyle_notes(log.notes)["exercise_entries"] else None,
                    "food_feedback": parse_lifestyle_notes(log.notes)["food_entries"][-1] if parse_lifestyle_notes(log.notes)["food_entries"] else None,
                }
                for log in logs
            ]
        )

    @app.get("/api/mental-health")
    @api_login_required
    def api_mental_health():
        user = current_user()
        logs = MentalLog.query.filter_by(user_id=user.id).order_by(MentalLog.log_date.desc()).limit(14).all()
        return api_success(
            data=[
                {
                    "id": log.id,
                    "log_date": iso_date(log.log_date),
                    "mood": log.mood,
                    "stress_level": log.stress_level,
                    "wellness_tip": log.wellness_tip,
                }
                for log in logs
            ]
        )

    @app.get("/api/appointments")
    @api_login_required
    def api_appointments():
        user = current_user()
        appointments = Appointment.query.filter(Appointment.user_id == user.id).order_by(Appointment.appointment_date.asc()).all()
        decrypt_model_fields(appointments, ["prescription"])
        return api_success(
            data=[
                {
                    "id": appointment.id,
                    "doctor_name": appointment.doctor_name,
                    "appointment_date": iso_date(appointment.appointment_date),
                    "appointment_time": iso_time(appointment.appointment_time),
                    "notes": unpack_appointment_notes(appointment.notes),
                    "prescription": appointment.prescription,
                    "follow_up_date": iso_date(appointment.follow_up_date),
                }
                for appointment in appointments
            ]
        )

    @app.get("/api/ml/health-assessment")
    @api_login_required
    def api_ml_health_assessment():
        user = current_user()
        latest_lifestyle = LifestyleLog.query.filter_by(user_id=user.id).order_by(LifestyleLog.log_date.desc()).first()
        latest_mental = MentalLog.query.filter_by(user_id=user.id).order_by(MentalLog.log_date.desc()).first()
        assessment = health_assessment_for_inputs(
            sleep_hours=latest_lifestyle.sleep_hours if latest_lifestyle else 7,
            water_intake=latest_lifestyle.water_intake_liters if latest_lifestyle else 2,
            stress_level=latest_mental.stress_level if latest_mental else 5,
            activity_minutes=latest_lifestyle.exercise_minutes if latest_lifestyle else 30,
        )
        return api_success(
            data={
                "inputs": assessment["inputs"],
                "score": assessment["score"],
                "recommendations": assessment["recommendations"],
                "service": "ml_service",
                "model_source": assessment["model_source"],
            }
        )

    @app.get("/api/notifications/config")
    @api_login_required
    def api_notifications_config():
        user = current_user()
        vapid_config = ensure_vapid_config(app)
        active_subscription_count = WebPushSubscription.query.filter_by(user_id=user.id, is_active=True).count()
        return api_success(
            data={
                "web_push_enabled": bool(vapid_config["enabled"]),
                "vapid_public_key": vapid_config["public_key"],
                "active_subscription_count": active_subscription_count,
                "scheduler_interval_seconds": int(os.getenv("PUSH_SCHEDULER_INTERVAL_SECONDS", "15")),
                "medication_lead_seconds": int(os.getenv("MEDICATION_PUSH_LEAD_SECONDS", "120")),
                "medication_followup_delay_seconds": medication_followup_delay_seconds(),
                "medication_missed_cutoff_time": medication_missed_cutoff_time_string(),
                "appointment_lead_seconds": int(os.getenv("APPOINTMENT_PUSH_LEAD_SECONDS", "1800")),
            }
        )

    def parse_push_subscription(payload):
        subscription = (payload or {}).get("subscription") or payload or {}
        endpoint = str(subscription.get("endpoint") or "").strip()
        keys = subscription.get("keys") or {}
        p256dh = str(keys.get("p256dh") or "").strip()
        auth = str(keys.get("auth") or "").strip()
        if not endpoint or not p256dh or not auth:
            return None
        return {
            "endpoint": endpoint,
            "p256dh": p256dh,
            "auth": auth,
        }

    def save_browser_push_subscription(user, parsed_subscription):
        subscription = WebPushSubscription.query.filter_by(endpoint=parsed_subscription["endpoint"]).first()
        if not subscription:
            subscription = WebPushSubscription(endpoint=parsed_subscription["endpoint"])
            db.session.add(subscription)

        subscription.user_id = user.id
        subscription.p256dh = parsed_subscription["p256dh"]
        subscription.auth = parsed_subscription["auth"]
        subscription.user_agent = (request.headers.get("User-Agent") or "")[:255]
        subscription.is_active = True
        subscription.last_seen_at = datetime.now()
        db.session.commit()
        return subscription

    @app.post("/api/notifications/subscribe")
    @api_login_required
    def api_notifications_subscribe():
        user = current_user()
        parsed_subscription = parse_push_subscription(api_json_body())
        if not parsed_subscription:
            return api_error("invalid_subscription", "A valid push subscription endpoint and keys are required.", status=422)

        subscription = save_browser_push_subscription(user, parsed_subscription)
        return api_success(
            data={
                "id": subscription.id,
                "active": safe_bool(subscription.is_active),
            },
            message="Push subscription saved.",
        )

    @app.post("/api/notifications/unsubscribe")
    @api_login_required
    def api_notifications_unsubscribe():
        user = current_user()
        parsed_subscription = parse_push_subscription(api_json_body())
        endpoint = parsed_subscription["endpoint"] if parsed_subscription else str((api_json_body() or {}).get("endpoint") or "").strip()
        if not endpoint:
            return api_error("invalid_subscription", "A subscription endpoint is required.", status=422)
        subscription = WebPushSubscription.query.filter_by(user_id=user.id, endpoint=endpoint).first()
        if subscription:
            subscription.is_active = False
            db.session.commit()
        return api_success(message="Push subscription disabled.")

    @app.post("/api/notifications/test-push")
    @api_login_required
    def api_notifications_test_push():
        user = current_user()
        if webpush is None:
            return api_error(
                "web_push_unavailable",
                "Install pywebpush to send server-originated push notifications.",
                status=503,
            )

        parsed_subscription = parse_push_subscription(api_json_body())
        if parsed_subscription:
            save_browser_push_subscription(user, parsed_subscription)

        payload = build_push_payload(
            "Server Push Test",
            "This came from the Flask backend, so reminders can work even when the page is closed.",
            f"server-test-{user.id}-{int(time.time())}",
            "/settings",
            "important_update",
        )
        sent_count = dispatch_user_push(app, user.id, payload, ttl_seconds=600)
        if sent_count == 0:
            return api_error(
                "no_active_push_subscriptions",
                "No active browser push subscriptions could receive the test notification.",
                status=409,
            )
        return api_success(data={"sent_count": sent_count}, message="Server push notification sent.")

    @app.post("/account/delete")
    @login_required
    def delete_account():
        user = current_user()
        db.session.delete(user)
        db.session.commit()
        session.clear()
        flash("Your account has been deleted.", "info")
        return redirect(url_for("register"))

    @app.post("/quick-checkin/mood")
    @login_required
    def quick_checkin_mood():
        user = current_user()
        mood = request.form.get("mood", "Okay")
        stress_slider = max(1, min(5, parse_int(request.form.get("stress_level", 3))))
        stress_level = min(10, stress_slider * 2)
        log = MentalLog.query.filter_by(user_id=user.id, log_date=date.today()).first()
        if not log:
            log = MentalLog(user_id=user.id, log_date=date.today())
            db.session.add(log)
        log.mood = mood
        log.stress_level = stress_level
        log.wellness_tip = build_mental_tip(mood, stress_level)
        db.session.commit()
        flash("Mood check-in saved.", "success")
        return redirect(request.form.get("next") or url_for("dashboard"))

    @app.route("/alerts")
    @login_required
    def alerts():
        context = build_alerts_context(current_user())
        return render_template(
            "alerts.html",
            score=context["score"],
            insights=context["insights"],
            trends=context["trends"],
            recent_patterns=context["recent_patterns"],
            active_alerts=context["active_alerts"],
            sleep_mood_insight=context["sleep_mood_insight"],
            sleep_hours_for_insight=context["sleep_hours_for_insight"],
            risk_indicator=context["risk_indicator"],
        )

    @app.route("/medications", methods=["GET", "POST"])
    @login_required
    def medications():
        user = current_user()
        if request.method == "POST":
            medication = Medication(
                user_id=user.id,
                name=request.form["name"].strip(),
                dosage=request.form["dosage"].strip(),
                time_of_day=datetime.strptime(request.form["time_of_day"], "%H:%M").time(),
                notes=encrypt_text(request.form.get("notes", "").strip()),
                status="pending",
                reminder_enabled=bool(request.form.get("reminder_enabled")),
            )
            db.session.add(medication)
            db.session.commit()
            flash("Medication added.", "success")
            return redirect(url_for("medications"))
        try:
            meds = Medication.query.filter_by(user_id=user.id).order_by(Medication.time_of_day.asc()).all()
            normalize_medication_statuses(user, meds)
            decrypt_model_fields(meds, ["notes"])
        except SQLAlchemyError:
            db.session.rollback()
            app.logger.exception("Medication page unavailable while database schema is preparing.")
            meds = []
            flash("Medication data is still loading. Please refresh in a moment.", "warning")
        recent_history = fetch_medication_history(user, limit=5)
        return render_template(
            "medications.html",
            medications=meds,
            recent_history=recent_history,
            medication_history_count=safe_medication_history_count(user),
        )

    @app.get("/medications/history")
    @login_required
    def medication_history():
        user = current_user()
        history_entries = fetch_medication_history(user)
        try:
            medications = Medication.query.filter_by(user_id=user.id).order_by(Medication.time_of_day.asc()).all()
            build_medication_daily_summary(user, medications)
        except SQLAlchemyError:
            db.session.rollback()
            app.logger.exception("Medication history confirmation list unavailable while database schema is preparing.")
            medications = []
        unconfirmed_entries = [
            medication
            for medication in medications
            if getattr(medication, "daily_status", "") in {"unconfirmed", "unconfirmed_missed"}
        ]
        return render_template(
            "medication_history.html",
            history_entries=history_entries,
            history_count=len(history_entries),
            unconfirmed_entries=unconfirmed_entries,
        )

    @app.post("/medications/<int:medication_id>/status")
    @login_required
    def medication_status(medication_id):
        user = current_user()
        medication = owned_record_or_404(Medication, user, medication_id)
        normalize_medication_statuses(user, [medication])
        requested_status = (request.form.get("status") or "").strip().lower()
        if requested_status not in {"pending", "taken", "skipped", "missed"}:
            flash("Invalid medication status.", "danger")
            return redirect(url_for("medications"))

        replace_medication_day_event(
            user,
            medication,
            requested_status if requested_status in MEDICATION_EVENT_STATUSES else None,
            event_time=app_now(),
        )
        db.session.commit()
        status_messages = {
            "taken": f"{medication.name} logged as taken.",
            "skipped": f"{medication.name} logged as skipped.",
            "missed": f"{medication.name} logged as missed.",
            "pending": f"{medication.name} reset for today's tracking.",
        }
        flash(status_messages[requested_status], "success")
        return redirect(url_for("medications"))

    @app.post("/medications/<int:medication_id>/delete")
    @login_required
    def medication_delete(medication_id):
        user = current_user()
        medication = owned_record_or_404(Medication, user, medication_id)
        medication_name = medication.name
        db.session.delete(medication)
        db.session.commit()
        flash(f"{medication_name} deleted.", "success")
        return redirect(url_for("medications"))

    @app.route("/lifestyle", methods=["GET", "POST"])
    @login_required
    def lifestyle():
        user = current_user()
        logs = LifestyleLog.query.filter_by(user_id=user.id).order_by(LifestyleLog.log_date.desc()).all()
        today = app_today()
        latest_log = next((log for log in logs if log.log_date == today), None)
        suggestions = []
        today_summary = None
        if latest_log:
            assessment = health_assessment_for_inputs(
                sleep_hours=latest_log.sleep_hours,
                water_intake=latest_log.water_intake_liters,
                stress_level=5,
                activity_minutes=latest_log.exercise_minutes,
            )
            suggestions = assessment["recommendations"]
            today_summary = {
                "hydration": {
                    "title": "Hydration",
                    "result": assessment["hydration_evaluation"]["title"],
                    "feedback": (
                        "Increase fluids gradually to support energy and PCOS self-management"
                        if assessment["hydration_evaluation"]["status"] == "low"
                        else "Hydration is supporting your PCOS routine"
                    ),
                    "tone": assessment["hydration_evaluation"]["tone"],
                },
                "sleep": {
                    "title": "Sleep",
                    "result": assessment["sleep_evaluation"]["title"],
                    "feedback": (
                        "Try a steadier sleep routine to support hormonal balance"
                        if assessment["sleep_evaluation"]["status"] == "low"
                        else "Sleep is supporting hormonal balance"
                        if assessment["sleep_evaluation"]["status"] == "normal"
                        else "Keep rest steady to support recovery and mood"
                    ),
                    "tone": assessment["sleep_evaluation"]["tone"],
                },
            }
        decrypt_model_fields(logs, ["notes"])
        latest_log_details = parse_lifestyle_notes(latest_log.notes if latest_log else "")
        latest_exercise_entry = latest_log_details["exercise_entries"][-1] if latest_log_details["exercise_entries"] else None
        latest_food_entry = latest_log_details["food_entries"][-1] if latest_log_details["food_entries"] else None
        summary_cards = [
            today_summary["hydration"] if today_summary else {
                "title": "Hydration",
                "result": "No water log yet",
                "feedback": "Log water intake to view a PCOS support summary",
                "tone": "info",
            },
            today_summary["sleep"] if today_summary else {
                "title": "Sleep",
                "result": "No sleep log yet",
                "feedback": "Log sleep hours to view a PCOS support summary",
                "tone": "info",
            },
            {
                "title": "Activity Level",
                "result": latest_exercise_entry["classification"] if latest_exercise_entry else "No activity logged yet",
                "feedback": (
                    "Increase movement to support insulin response and routine balance"
                    if latest_exercise_entry and latest_exercise_entry["classification"] == "Low Activity"
                    else "Movement is supporting blood sugar balance"
                    if latest_exercise_entry and latest_exercise_entry["classification"] == "PCOS-Supportive Activity"
                    else "Maintain balance, hydration, and recovery"
                    if latest_exercise_entry
                    else "Log exercise to view a PCOS support summary"
                ),
                "tone": latest_exercise_entry["tone"] if latest_exercise_entry else "info",
            },
            {
                "title": "Meal Classification",
                "result": (
                    latest_food_entry["raw_input"]["food_category"].replace("-", " ").title()
                    if latest_food_entry
                    else "No meal logged yet"
                ),
                "feedback": (
                    "Watch high-sugar portions to support steadier insulin balance"
                    if latest_food_entry and latest_food_entry["status"] == "warning"
                    else "Meals are supporting steadier energy and insulin patterns"
                    if latest_food_entry and latest_food_entry["status"] == "positive"
                    else "Reduce processed foods when possible"
                    if latest_food_entry and latest_food_entry["status"] == "caution"
                    else "Keep portions balanced for steadier energy"
                    if latest_food_entry
                    else "Log a meal to view a PCOS support summary"
                ),
                "tone": latest_food_entry["tone"] if latest_food_entry else "info",
            },
        ]
        return render_template(
            "lifestyle.html",
            logs=logs,
            suggestions=suggestions,
            latest_log=latest_log,
            latest_exercise_entry=latest_exercise_entry,
            latest_food_entry=latest_food_entry,
            summary_cards=summary_cards,
        )

    @app.get("/lifestyle/history")
    @login_required
    def lifestyle_history():
        user = current_user()
        history_type = (request.args.get("type") or "exercise").strip().lower()
        if history_type not in {"exercise", "food"}:
            history_type = "exercise"

        logs = LifestyleLog.query.filter_by(user_id=user.id).order_by(LifestyleLog.log_date.desc()).all()
        decrypt_model_fields(logs, ["notes"])
        history_entries = build_lifestyle_feedback_preview(logs, history_type, limit=None)

        return render_template(
            "lifestyle_history.html",
            history_type=history_type,
            history_entries=history_entries,
        )

    @app.post("/lifestyle/water")
    @login_required
    def lifestyle_water():
        user = current_user()
        glasses = max(0, min(8, parse_int(request.form.get("glasses"), 0)))
        log = get_or_create_today_lifestyle_log(user)
        log.water_intake_liters = round(glasses * 0.25, 2)
        db.session.commit()
        flash("Water intake updated.", "success")
        return redirect(url_for("lifestyle"))

    @app.post("/lifestyle/sleep")
    @login_required
    def lifestyle_sleep():
        user = current_user()
        sleep_hours = max(0, min(24, parse_float(request.form.get("sleep_hours"), 0)))
        log = get_or_create_today_lifestyle_log(user)
        log.sleep_hours = sleep_hours
        db.session.commit()
        flash("Sleep hours updated.", "success")
        return redirect(url_for("lifestyle"))

    @app.post("/lifestyle/exercise")
    @login_required
    def lifestyle_exercise():
        user = current_user()
        activity_type = request.form.get("activity_type", "").strip()
        duration_minutes = parse_int(request.form.get("duration_minutes") or request.form.get("exercise_minutes"), -1)
        intensity = request.form.get("intensity", "").strip().lower()
        evaluation = evaluate_exercise_entry(activity_type, duration_minutes, intensity)
        if not evaluation["ok"]:
            for error in evaluation["errors"]:
                flash(error, "warning")
            return redirect(url_for("lifestyle"))

        log = get_or_create_today_lifestyle_log(user)
        parsed_notes = parse_lifestyle_notes(log.notes)
        parsed_notes["exercise_entries"].append(evaluation["entry"])
        log.exercise_minutes = evaluation["entry"]["raw_input"]["duration_minutes"]
        log.notes = encrypt_text(
            serialize_lifestyle_notes(
                general_notes=parsed_notes["general_notes"],
                exercise_entries=parsed_notes["exercise_entries"],
                food_entries=parsed_notes["food_entries"],
            )
        )
        db.session.commit()
        flash(evaluation["entry"]["feedback"], "success")
        return redirect(url_for("lifestyle"))

    @app.post("/lifestyle/quick-food")
    @login_required
    def quick_food():
        user = current_user()
        food_name = request.form.get("food_name", "").strip() or request.form.get("meal_text", "").strip()
        portion_size = request.form.get("portion_size", "").strip().lower() or ("medium" if request.form.get("meal_text", "").strip() else "")
        food_category = request.form.get("food_category", "").strip().lower() or ("balanced" if request.form.get("meal_text", "").strip() else "")
        evaluation = evaluate_food_entry(food_name, portion_size, food_category)
        if not evaluation["ok"]:
            for error in evaluation["errors"]:
                flash(error, "warning")
            return redirect(url_for("lifestyle"))

        log = get_or_create_today_lifestyle_log(user)
        parsed_notes = parse_lifestyle_notes(log.notes)
        parsed_notes["food_entries"].append(evaluation["entry"])
        category_labels = {
            "high sugar": "High Sugar",
            "protein-rich": "Protein-Rich",
            "balanced": "Balanced",
            "fast food": "Fast Food",
        }
        log.diet_quality = category_labels.get(evaluation["entry"]["raw_input"]["food_category"], log.diet_quality)
        log.notes = encrypt_text(
            serialize_lifestyle_notes(
                general_notes=parsed_notes["general_notes"],
                exercise_entries=parsed_notes["exercise_entries"],
                food_entries=parsed_notes["food_entries"],
            )
        )

        db.session.commit()
        flash(evaluation["entry"]["feedback"], "success")
        return redirect(url_for("lifestyle"))

    @app.route("/mental-health", methods=["GET", "POST"])
    @login_required
    def mental_health():
        user = current_user()
        mood_scale = {
            "Awful": 1,
            "Bad": 2,
            "Okay": 3,
            "Good": 4,
            "Great": 5,
            "Sad": 2,
            "Anxious": 2,
            "Calm": 4,
            "Happy": 5,
            "Overwhelmed": 1,
        }
        if request.method == "POST":
            mood = request.form["mood"]
            slider_stress = max(1, min(5, parse_int(request.form["stress_level"])))
            stress_level = min(10, slider_stress * 2)
            log_date = datetime.strptime(request.form["log_date"], "%Y-%m-%d").date()
            tip = build_mental_tip(mood, stress_level)
            log = MentalLog.query.filter_by(user_id=user.id, log_date=log_date).first()
            if not log:
                log = MentalLog(user_id=user.id, log_date=log_date)
                db.session.add(log)
            log.mood = mood
            log.stress_level = stress_level
            log.wellness_tip = tip
            db.session.commit()
            flash("Mood and stress entry saved for your PCOS support log.", "success")
            return redirect(url_for("mental_health"))
        logs = MentalLog.query.filter_by(user_id=user.id).order_by(MentalLog.log_date.desc()).all()
        average_stress = db.session.query(func.avg(MentalLog.stress_level)).filter(MentalLog.user_id == user.id).scalar()
        today_log = MentalLog.query.filter_by(user_id=user.id, log_date=date.today()).first()
        latest_log = logs[0] if logs else None
        form_log = today_log or latest_log
        recent_logs = list(reversed(logs[:7]))
        mood_options = ["Awful", "Bad", "Okay", "Good", "Great"]
        trend_points = []
        for log in recent_logs:
            trend_points.append(
                {
                    "label": log.log_date.strftime("%a"),
                    "mood": max(1, min(5, mood_scale.get(log.mood, 3))),
                    "stress": max(1, min(5, round(log.stress_level / 2))),
                }
            )
        selected_mood = form_log.mood if form_log and form_log.mood in mood_options else "Okay"
        selected_stress = max(1, min(5, round((form_log.stress_level if form_log else 6) / 2)))
        latest_tip = (
            form_log.wellness_tip
            if form_log and form_log.wellness_tip
            else "Save your first mood log to receive PCOS-aware guidance based on your own entries."
        )
        related_lifestyle_log = None
        if form_log:
            related_lifestyle_log = LifestyleLog.query.filter_by(user_id=user.id, log_date=form_log.log_date).first()
        if not related_lifestyle_log:
            related_lifestyle_log = LifestyleLog.query.filter_by(user_id=user.id).order_by(LifestyleLog.log_date.desc()).first()
        sleep_hours_for_insight = related_lifestyle_log.sleep_hours if related_lifestyle_log else None
        sleep_mood_insight = build_sleep_mood_insight(selected_mood, selected_stress, sleep_hours_for_insight)
        return render_template(
            "mental_health.html",
            logs=logs,
            average_stress=round(average_stress or 0, 1),
            latest_log=form_log,
            mood_options=mood_options,
            selected_mood=selected_mood,
            selected_stress=selected_stress,
            trend_points=trend_points,
            latest_tip=latest_tip,
            sleep_hours_for_insight=sleep_hours_for_insight,
            sleep_mood_insight=sleep_mood_insight,
            has_mental_history=bool(logs),
            today_date=date.today().strftime("%Y-%m-%d"),
        )

    @app.route("/calendar", methods=["GET", "POST"])
    @login_required
    def calendar():
        user = current_user()
        if request.method == "POST":
            log_date = datetime.strptime(request.form["log_date"], "%Y-%m-%d").date()
            delete_day_log = bool(request.form.get("delete_day_log"))
            if delete_day_log:
                cycle_log = CycleLog.query.filter_by(user_id=user.id, log_date=log_date).first()
                if cycle_log:
                    db.session.delete(cycle_log)
                    db.session.commit()
                    flash(f"Cycle log removed for {log_date.strftime('%b %d, %Y')}.", "success")
                else:
                    flash("No cycle log found for that day.", "warning")
                return redirect(
                    url_for(
                        "calendar",
                        selected=log_date.strftime("%Y-%m-%d"),
                        view_month=log_date.strftime("%Y-%m"),
                    )
                )
            mark_period_start = bool(request.form.get("mark_period_start"))
            period_start = bool(request.form.get("period_start")) or mark_period_start
            end_period = bool(request.form.get("end_period"))
            save_day_details = bool(request.form.get("save_day_details")) or end_period
            requested_cycle_day = parse_int(request.form.get("cycle_day"), default=0)
            existing_logs = CycleLog.query.filter_by(user_id=user.id).order_by(CycleLog.log_date.asc()).all()
            cycle_day_value = resolve_cycle_day_value(
                log_date,
                cycle_model(existing_logs),
                requested_cycle_day=requested_cycle_day,
                mark_period_start=period_start,
            )
            flow_level = "No Flow" if end_period else normalize_flow_level(request.form.get("flow_level", ""))
            cycle_log = CycleLog.query.filter_by(user_id=user.id, log_date=log_date).first()
            if cycle_log:
                cycle_log.cycle_day = cycle_day_value
                cycle_log.period_start = period_start
                if save_day_details:
                    cycle_log.symptoms = pack_cycle_details(
                        request.form.get("symptoms", ""),
                        request.form.get("notes", ""),
                    )
                    cycle_log.flow_level = flow_level
                elif is_explicit_no_flow(cycle_log.flow_level):
                    cycle_log.flow_level = ""
            else:
                cycle_log = CycleLog(
                    user_id=user.id,
                    log_date=log_date,
                    period_start=period_start,
                    cycle_day=cycle_day_value,
                    symptoms=pack_cycle_details(
                        request.form.get("symptoms", ""),
                        request.form.get("notes", ""),
                    ) if save_day_details else "",
                    flow_level=flow_level if save_day_details else "",
                )
                db.session.add(cycle_log)
            db.session.commit()
            if end_period:
                flash("Period end logged. PCOS cycle insights updated.", "success")
            elif mark_period_start and not save_day_details:
                flash("Period start logged as Cycle Day 1. Please log your flow to improve PCOS cycle accuracy.", "success")
            else:
                flash("Cycle log updated and PCOS insights refreshed.", "success")
            return redirect(
                url_for(
                    "calendar",
                    selected=log_date.strftime("%Y-%m-%d"),
                    view_month=log_date.strftime("%Y-%m"),
                )
            )
        logs = CycleLog.query.filter_by(user_id=user.id).order_by(CycleLog.log_date.desc()).all()
        today = date.today()
        selected_param = request.args.get("selected")
        selected_date = datetime.strptime(selected_param, "%Y-%m-%d").date() if selected_param else today
        view_month_param = request.args.get("view_month")
        if view_month_param:
            try:
                displayed_month = datetime.strptime(view_month_param, "%Y-%m").date().replace(day=1)
            except ValueError:
                displayed_month = selected_date.replace(day=1)
        else:
            displayed_month = selected_date.replace(day=1)
        cycle_info = get_cycle_info(user, selected_date)
        model = cycle_model(logs)
        logs_by_date = {
            log.log_date: log
            for log in logs
            if log.log_date.year == displayed_month.year and log.log_date.month == displayed_month.month
        }
        first_weekday, total_days = monthrange(displayed_month.year, displayed_month.month)
        weekday_offset = (first_weekday + 1) % 7
        calendar_days = []
        for _ in range(weekday_offset):
            calendar_days.append(None)
        for day in range(1, total_days + 1):
            cell_date = date(displayed_month.year, displayed_month.month, day)
            log = logs_by_date.get(cell_date)
            inferred_day, inferred_start = inferred_cycle_day(cell_date, model)
            cycle_day = log.cycle_day if log and log.cycle_day else inferred_day
            details = unpack_cycle_details(log.symptoms if log else "")
            phase_name = cycle_phase_name(cell_date, cycle_day, inferred_start, model, log)
            cycle_day_label = (
                "Logged Day 1 (Period start)"
                if log and is_period_start_log(log) and log.cycle_day == 1
                else f"Logged Day {log.cycle_day}"
                if log and log.cycle_day
                else f"Estimated Day {cycle_day}"
                if model["phase_estimation_enabled"] and cycle_day
                else "Log more cycle data"
            )
            calendar_days.append(
                {
                    "day": day,
                    "date_iso": cell_date.strftime("%Y-%m-%d"),
                    "log": log,
                    "phase": cycle_phase_for_day(cell_date, cycle_day, inferred_start, model, log),
                    "phase_name": phase_name,
                    "cycle_day": cycle_day,
                    "cycle_day_label": cycle_day_label,
                    "flow_level": normalize_flow_level(log.flow_level) if log else "",
                    "period_start": is_period_start_log(log) if log else False,
                    "symptoms": details["symptoms"],
                    "notes": details["notes"],
                    "has_data": bool(log),
                    "selected": cell_date == selected_date,
                    "is_today": cell_date == today,
                }
        )
        previous_month = (displayed_month.replace(day=1) - timedelta(days=1)).replace(day=1)
        next_month = (displayed_month.replace(day=28) + timedelta(days=4)).replace(day=1)
        phase_guide = [
            {"phase": "Period", "days": "Logged only"},
            {"phase": "Possible Fertile Window", "days": cycle_info["fertile_window_text"]},
            {"phase": "Possible Ovulation", "days": cycle_info["ovulation_status"]},
            {"phase": "Luteal Phase", "days": "Shown only when ovulation is likely"},
        ]
        selected_log = next((log for log in logs if log.log_date == selected_date), None)
        selected_details = unpack_cycle_details(selected_log.symptoms if selected_log else "")
        selected_flow = normalize_flow_level(selected_log.flow_level) if selected_log else ""
        selected_entry = {
            "date_label": selected_date.strftime("%b %d, %Y"),
            "date_iso": selected_date.strftime("%Y-%m-%d"),
            "phase": cycle_info["phase"],
            "cycle_day": cycle_info["cycle_day_label"],
            "cycle_day_value": selected_log.cycle_day if selected_log and selected_log.cycle_day else (cycle_info["day_number"] or 1),
            "flow_level": (
                "Period start marked"
                if selected_log and is_period_start_log(selected_log) and not selected_flow
                else selected_flow or "Not logged"
            ),
            "flow_value": selected_flow,
            "period_start": is_period_start_log(selected_log) if selected_log else False,
            "symptoms": selected_details["symptoms"],
            "notes": selected_details["notes"],
            "has_data": bool(selected_log),
        }
        cycle_prompts = []
        latest_cycle_log = logs[0] if logs else None
        if cycle_info["needs_flow_log_prompt"]:
            cycle_prompts.append("Please log your flow to improve accuracy.")
        if cycle_info["next_period_earliest"] and 0 <= (cycle_info["next_period_earliest"] - today).days <= 1 and not selected_log:
            cycle_prompts.append("Did your period start today?")
        if not selected_log:
            cycle_prompts.append("Log today's symptoms and flow")
        if not latest_cycle_log or (today - latest_cycle_log.log_date).days >= 4:
            cycle_prompts.append("Tap a day to update your cycle")
        return render_template(
            "calendar.html",
            logs=logs,
            cycle_info=cycle_info,
            phase_guide=phase_guide,
            calendar_days=calendar_days,
            calendar_month=displayed_month.strftime("%B %Y"),
            previous_month=previous_month.strftime("%Y-%m"),
            next_month=next_month.strftime("%Y-%m"),
            selected_log=selected_log,
            selected_date=selected_date,
            selected_entry=selected_entry,
            cycle_prompts=cycle_prompts,
        )

    @app.route("/cycle-phases")
    @login_required
    def cycle_phases():
        phase_cards = [
            {
                "slug": "period",
                "eyebrow": "Period",
                "title": "Menstrual Phase",
                "summary": "This is when the uterus sheds its lining, which causes menstrual bleeding.",
                "facts": [
                    "It often lasts about 3 to 7 days, though every cycle can be a little different.",
                    "Estrogen and progesterone are low at this point in the cycle.",
                    "This phase marks the first day of a new cycle.",
                ],
            },
            {
                "slug": "follicular",
                "eyebrow": "Build Up",
                "title": "Follicular Phase",
                "summary": "This phase starts on the first day of menstruation while the body begins preparing an egg for release.",
                "facts": [
                    "Signals from the brain help the ovaries get eggs ready.",
                    "Estrogen gradually rises during this phase.",
                    "That rise helps rebuild the uterine lining after a period.",
                ],
            },
            {
                "slug": "ovulation",
                "eyebrow": "Most Fertile",
                "title": "Ovulation Phase",
                "summary": "Ovulation happens when the ovary releases a mature egg.",
                "facts": [
                    "It often happens around the middle of the cycle, but timing can vary from person to person.",
                    "This is usually the most fertile part of the cycle.",
                    "Cycle tracking can help you spot when ovulation may be happening for you.",
                ],
            },
            {
                "slug": "luteal",
                "eyebrow": "Reset Window",
                "title": "Luteal Phase",
                "summary": "After ovulation, the body shifts into a phase that supports a possible pregnancy.",
                "facts": [
                    "Progesterone rises to help the uterus get ready.",
                    "The uterine lining stays prepared in case a fertilized egg implants.",
                    "If pregnancy does not happen, hormone levels drop and the next period begins.",
                ],
            },
        ]
        return render_template(
            "cycle_phases.html",
            phase_cards=phase_cards,
            source_url="https://www.mayoclinic.org/healthy-lifestyle/womens-health/in-depth/menstrual-cycle/art-20047186",
        )

    @app.route("/appointments", methods=["GET", "POST"])
    @login_required
    def appointments():
        user = current_user()
        view_mode = normalize_appointment_view(request.args.get("view"))
        if request.method == "POST":
            appointment_id = parse_int(request.form.get("appointment_id"), default=0)
            reminder_enabled = bool(request.form.get("reminder_enabled"))
            form_values = {
                "appointment_id": str(appointment_id) if appointment_id else "",
                "doctor_name": request.form.get("doctor_name", "").strip(),
                "appointment_date": request.form.get("appointment_date", "").strip(),
                "appointment_time": request.form.get("appointment_time", "").strip(),
                "specialty": request.form.get("specialty", "").strip(),
                "location": request.form.get("location", "").strip(),
                "notes": request.form.get("notes", "").strip(),
                "prescription": request.form.get("prescription", "").strip(),
                "follow_up_date": request.form.get("follow_up_date", "").strip(),
                "reminder_enabled": reminder_enabled,
            }
            appointment = None
            existing_status = "scheduled"
            is_edit = appointment_id > 0
            if is_edit:
                appointment = user_records(Appointment, user).filter_by(id=appointment_id).first()
                if not appointment:
                    flash("Appointment not found.", "danger")
                    return redirect(url_for("appointments", view=view_mode) if view_mode != "overview" else url_for("appointments"))
                existing_status = unpack_appointment_notes(appointment.notes).get("status", "scheduled")
            else:
                appointment = Appointment(user_id=user.id)
            appointment_date, appointment_date_error = parse_form_date(form_values["appointment_date"], "Appointment date")
            appointment_time, appointment_time_error = parse_form_time(form_values["appointment_time"], "Appointment time")
            follow_up_date, follow_up_date_error = parse_form_date(form_values["follow_up_date"], "Follow-up date", required=False)
            validation_errors = [
                error for error in [
                    "Doctor name is required." if not form_values["doctor_name"] else None,
                    "Specialty is required." if not form_values["specialty"] else None,
                    "Location is required." if not form_values["location"] else None,
                    appointment_date_error,
                    appointment_time_error,
                    follow_up_date_error,
                ]
                if error
            ]
            if validation_errors:
                for error in validation_errors:
                    flash(error, "danger")
                return render_template(
                    "appointments.html",
                    **appointment_page_context(
                        user,
                        form_values=form_values,
                        modal_open=True,
                        modal_mode="edit" if is_edit else "new",
                        view_mode=view_mode,
                    ),
                )

            if not is_edit:
                db.session.add(appointment)

            appointment.doctor_name = form_values["doctor_name"]
            appointment.appointment_date = appointment_date
            appointment.appointment_time = appointment_time
            appointment.notes = pack_appointment_notes(
                specialty=form_values["specialty"] or "General Checkup",
                location=form_values["location"] or "Clinic location",
                reminder_enabled=reminder_enabled,
                status=existing_status,
                notes_text=form_values["notes"],
            )
            appointment.prescription = encrypt_text(form_values["prescription"])
            appointment.follow_up_date = follow_up_date
            db.session.commit()
            flash("Appointment updated." if is_edit else "Appointment saved.", "success")
            return redirect(url_for("appointments", view=view_mode) if view_mode != "overview" else url_for("appointments"))
        return render_template("appointments.html", **appointment_page_context(user, view_mode=view_mode))

    @app.post("/appointments/<int:appointment_id>/delete")
    @login_required
    def appointment_delete(appointment_id):
        view_mode = normalize_appointment_view(request.args.get("view"))
        user = current_user()
        appointment = owned_record_or_404(Appointment, user, appointment_id)
        db.session.delete(appointment)
        db.session.commit()
        flash("Appointment deleted.", "success")
        return redirect(url_for("appointments", view=view_mode) if view_mode != "overview" else url_for("appointments"))

    @app.post("/appointments/<int:appointment_id>/done")
    @login_required
    def appointment_done(appointment_id):
        view_mode = normalize_appointment_view(request.args.get("view"))
        user = current_user()
        appointment = owned_record_or_404(Appointment, user, appointment_id)
        meta = unpack_appointment_notes(appointment.notes)
        appointment.notes = pack_appointment_notes(
            specialty=meta["specialty"],
            location=meta["location"],
            reminder_enabled=meta["reminder_enabled"],
            status="completed",
            notes_text=meta["notes_text"],
        )
        db.session.commit()
        flash("Appointment marked as completed.", "success")
        return redirect(url_for("appointments", view=view_mode) if view_mode != "overview" else url_for("appointments"))


app = create_app()


if __name__ == "__main__":
    app.run(debug=True)
