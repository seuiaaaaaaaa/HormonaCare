from datetime import date, datetime

from flask_sqlalchemy import SQLAlchemy


db = SQLAlchemy()


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(120), nullable=False)
    username = db.Column(db.String(80), unique=True, nullable=False)
    account_username = db.Column(db.String(30))
    password_hash = db.Column(db.String(255), nullable=False)
    pin_number = db.Column(db.String(255))
    security_pin_hash = db.Column(db.String(255))
    pin_failed_attempts = db.Column(db.Integer, default=0, nullable=False)
    pin_locked_until = db.Column(db.DateTime)
    role = db.Column(db.String(20), nullable=False, default="user")
    archived_at = db.Column(db.DateTime)
    archive_reason = db.Column(db.String(255))
    supabase_user_id = db.Column(db.String(80), unique=True)
    email_verified = db.Column(db.Boolean, default=False)
    email_verified_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, server_default=db.func.now())

    medications = db.relationship("Medication", backref="user", lazy="select", cascade="all, delete-orphan")
    medication_logs = db.relationship("MedicationLog", backref="user", lazy="select", cascade="all, delete-orphan")
    lifestyle_logs = db.relationship("LifestyleLog", backref="user", lazy="select", cascade="all, delete-orphan")
    mental_logs = db.relationship("MentalLog", backref="user", lazy="select", cascade="all, delete-orphan")
    cycle_logs = db.relationship("CycleLog", backref="user", lazy="select", cascade="all, delete-orphan")
    appointments = db.relationship("Appointment", backref="user", lazy="select", cascade="all, delete-orphan")
    push_subscriptions = db.relationship("WebPushSubscription", backref="user", lazy="select", cascade="all, delete-orphan")
    push_notification_logs = db.relationship("PushNotificationLog", backref="user", lazy="select", cascade="all, delete-orphan")
    profile = db.relationship("UserProfile", backref="user", uselist=False, lazy="select", cascade="all, delete-orphan")

class Medication(db.Model):
    __tablename__ = "medications"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    dosage = db.Column(db.String(80), nullable=False)
    time_of_day = db.Column(db.Time, nullable=False)
    notes = db.Column(db.Text)
    status = db.Column(db.String(20), default="pending")
    reminder_enabled = db.Column(db.Boolean, default=False)
    client_sync_id = db.Column(db.String(80), index=True)
    client_updated_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, server_default=db.func.now())


class MedicationLog(db.Model):
    __tablename__ = "medication_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    medication_id = db.Column(db.Integer, nullable=False, index=True)
    medication_name = db.Column(db.String(120), nullable=False)
    dosage = db.Column(db.String(80), nullable=False)
    scheduled_time = db.Column(db.Time)
    notes = db.Column(db.Text)
    status = db.Column(db.String(20), default="taken")
    client_sync_id = db.Column(db.String(80), index=True)
    client_updated_at = db.Column(db.DateTime)
    taken_at = db.Column(db.DateTime, nullable=False, server_default=db.func.now())


class LifestyleLog(db.Model):
    __tablename__ = "lifestyle_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    log_date = db.Column(db.Date, default=date.today, nullable=False)
    sleep_hours = db.Column(db.Float, nullable=False)
    water_intake_liters = db.Column(db.Float, nullable=False)
    diet_quality = db.Column(db.String(50), nullable=False)
    exercise_minutes = db.Column(db.Integer, nullable=False)
    notes = db.Column(db.Text)
    client_sync_id = db.Column(db.String(80), index=True)
    client_updated_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, server_default=db.func.now())


class MentalLog(db.Model):
    __tablename__ = "mental_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    log_date = db.Column(db.Date, default=date.today, nullable=False)
    mood = db.Column(db.String(50), nullable=False)
    stress_level = db.Column(db.Integer, nullable=False)
    wellness_tip = db.Column(db.String(255))
    client_sync_id = db.Column(db.String(80), index=True)
    client_updated_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, server_default=db.func.now())


class CycleLog(db.Model):
    __tablename__ = "cycle_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    log_date = db.Column(db.Date, default=date.today, nullable=False)
    period_start = db.Column(db.Boolean, default=False)
    cycle_day = db.Column(db.Integer, nullable=False)
    symptoms = db.Column(db.Text)
    flow_level = db.Column(db.String(30))
    client_sync_id = db.Column(db.String(80), index=True)
    client_updated_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, server_default=db.func.now())


class Appointment(db.Model):
    __tablename__ = "appointments"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    doctor_name = db.Column(db.String(120), nullable=False)
    appointment_date = db.Column(db.Date, nullable=False)
    appointment_time = db.Column(db.Time, nullable=False)
    notes = db.Column(db.Text)
    prescription = db.Column(db.Text)
    follow_up_date = db.Column(db.Date)
    client_sync_id = db.Column(db.String(80), index=True)
    client_updated_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, server_default=db.func.now())


class UserProfile(db.Model):
    __tablename__ = "user_profiles"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, unique=True)
    age = db.Column(db.Integer)
    diagnosis_date = db.Column(db.Date)
    general_notifications = db.Column(db.Boolean, default=True)
    notify_meds = db.Column(db.Boolean, default=True)
    notify_appointments = db.Column(db.Boolean, default=True)
    notify_alerts = db.Column(db.Boolean, default=True)
    dark_mode = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, server_default=db.func.now())


class WebPushSubscription(db.Model):
    __tablename__ = "web_push_subscriptions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    endpoint = db.Column(db.Text, nullable=False, unique=True)
    p256dh = db.Column(db.Text, nullable=False)
    auth = db.Column(db.Text, nullable=False)
    user_agent = db.Column(db.String(255))
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, server_default=db.func.now())
    updated_at = db.Column(db.DateTime, server_default=db.func.now(), onupdate=db.func.now())
    last_seen_at = db.Column(db.DateTime, server_default=db.func.now())


class PushNotificationLog(db.Model):
    __tablename__ = "push_notification_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    notification_key = db.Column(db.String(180), nullable=False, unique=True)
    notification_type = db.Column(db.String(40), nullable=False)
    sent_at = db.Column(db.DateTime, server_default=db.func.now())


class AdminNote(db.Model):
    __tablename__ = "admin_notes"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    admin_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    note = db.Column(db.Text, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    created_at = db.Column(db.DateTime, server_default=db.func.now())


class AdminAuditLog(db.Model):
    __tablename__ = "admin_audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    admin_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    target_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    action = db.Column(db.String(80), nullable=False)
    details = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, server_default=db.func.now())
