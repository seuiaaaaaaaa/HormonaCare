# HormonaCare

HormonaCare is a Flask-based health management system for women living with PCOS. It combines health logging, cycle tracking, rule-based alerts, medication reminders, and appointment scheduling in a mobile-friendly dashboard.

## Core Service Architecture

HormonaCare now follows a single-backend approach where Python is the core service language.

- `Core Service`: Flask + SQLAlchemy + machine learning helpers in Python
- `Web Client`: current server-rendered UI/PWA-ready frontend
- `Application API Gateway`: Flask `before_request` entrypoint that centralizes API route metadata, protected-route checks, and API identity headers
- `API Layer`: controlled JSON endpoints for account access and health summaries under `/api/*`

## Features

- Secure registration and login with bcrypt password hashing
- Session-based authentication
- JSON API layer for controlled core-service access
- Basic PWA support with a manifest and service worker
- Dashboard with daily summary, cycle phase, reminders, and quick stats
- Rule-based alerts page using predefined thresholds and user input
- Medication tracker
- Lifestyle and mental health tracking
- Rule-based cycle calendar and symptom logging
- Appointment management with follow-up support
- Supabase PostgreSQL through `DATABASE_URL`

## Project Structure

- `app.py` Flask application and routes
- `app.py` also exposes the application-level API gateway entrypoint and `/api/*` endpoints for shared core-service integration
- `models.py` database schema and relationships
- `ml_model.py` machine learning helper
- `ml_service.py` service wrapper that exposes ML results to the app and API layer
- `prepare_weekly_wellness_dataset.py` preprocessing script for the Kaggle-sourced wellness training data
- `security_utils.py` field-encryption helper with optional Fernet support
- `templates/` HTML pages
- `static/css/style.css` styles
- `static/js/app.js` frontend interactions
- `static/manifest.json` PWA manifest
- `static/service-worker.js` offline shell caching

## Run Locally

1. Create a virtual environment:
   ```bash
   python -m venv venv
   ```
2. Activate it:
   ```bash
   venv\Scripts\activate
   ```
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Generate the processed ML dataset:
   ```bash
   python prepare_weekly_wellness_dataset.py
   ```
5. Copy `.env.example` to `.env` and update the values if you want a hosted database.
6. Start the app:
   ```bash
   python app.py
   ```
7. Open `http://127.0.0.1:5000`

### Testing With ngrok

- If you test the app on your phone through `ngrok`, set `PUBLIC_BASE_URL` to your current `https://...ngrok...` URL so email verification links point back to the active tunnel.
- Add your public tunnel URL to Supabase Auth redirect URLs as well. A wildcard such as `https://*.ngrok-free.app/**` is useful for rotating preview domains during development.

### Supabase Database Setup

If you want to use Supabase as the app database:

1. Open your Supabase project dashboard.
2. Copy the Session pooler Postgres connection string from the database connection settings.
3. Set `DATABASE_URL` in `.env` to that connection string.
4. Keep `?sslmode=require` on the URL so SQLAlchemy connects over SSL.

Example:

```env
DATABASE_URL=postgresql://postgres.PROJECT_REF:YOUR_DB_PASSWORD@aws-0-REGION.pooler.supabase.com:5432/postgres?sslmode=require
```

## API Endpoints

All API routes are served by the same Python backend and currently use the same authenticated session as the web app.

- `GET /api/docs`
- `GET /api/gateway`
- `POST /api/auth/register`
- `POST /api/auth/login`
- `POST /api/auth/logout`
- `GET /api/health`
- `GET /api/me`
- `GET /api/dashboard`
- `GET /api/profile`
- `GET /api/cycle`
- `GET /api/alerts`
- `GET /api/medications`
- `GET /api/lifestyle`
- `GET /api/mental-health`
- `GET /api/appointments`
- `GET /api/ml/health-assessment`

Example:

```bash
curl http://127.0.0.1:5000/api/health
```

Protected API routes require an active authenticated session.

### Mobile-Ready API Notes

- The Flask backend acts as the shared Python Core Service for the current web UI and read-only authenticated API consumers.
- `/api/*` routes expose controlled account and health-summary access without allowing client-side data sync writes.
- Mobile clients can create an account and log in through JSON endpoints:
  - `POST /api/auth/register`
  - `POST /api/auth/login`
  - `POST /api/auth/logout`
- `GET /api/docs` returns a lightweight capability map for client integration and demos.

## Security Notes

- Passwords are hashed using `bcrypt`
- Session-based authentication is enforced for protected screens and API routes
- Security headers are applied after each response
- HTTPS should be enabled in deployment so data in transit is encrypted
- Sensitive notes and appointment metadata now go through a field-encryption helper before being stored
- To activate real field-level encryption at rest, install `cryptography` and set `FIELD_ENCRYPTION_KEY`
