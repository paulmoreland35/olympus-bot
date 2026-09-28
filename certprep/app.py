"""CertPrep: practice tests, readiness scoring and analytics for IT certifications.

Run locally:
    pip install -r requirements.txt
    python app.py            # http://localhost:5000

Questions live in data/questions/<cert_id>.json and are synced into SQLite on startup.
"""
import datetime as dt
import functools
import hashlib
import json
import os
import random
import re
import sqlite3
import time

from flask import Flask, g, jsonify, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash

from bank import normalize_answer, validate_question

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
QUESTIONS_DIR = os.path.join(DATA_DIR, "questions")

SITE_NAME = os.environ.get("SITE_NAME", "CertPrep")
DEV_MODE = os.environ.get("DEV_MODE", "0") == "1"

# Readiness scoring: each domain's accuracy is smoothed toward a guessing prior,
# so a couple of lucky answers can't produce a high score.
PRIOR_ACCURACY = 0.25
PRIOR_WEIGHT = 8
RECENT_PER_DOMAIN = 50
FREE_FLASHCARDS_PER_SESSION = 10

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    is_premium INTEGER NOT NULL DEFAULT 0,
    stripe_customer_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS questions (
    id TEXT PRIMARY KEY,
    cert_id TEXT NOT NULL,
    domain_id TEXT NOT NULL,
    difficulty INTEGER NOT NULL DEFAULT 2,
    question TEXT NOT NULL,
    choices TEXT NOT NULL,
    answer TEXT NOT NULL,
    explanation TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_questions_cert ON questions(cert_id, domain_id);
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    cert_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    question_ids TEXT NOT NULL,
    time_limit_sec INTEGER,
    started_at REAL NOT NULL,
    finished_at REAL,
    score REAL
);
CREATE TABLE IF NOT EXISTS responses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    attempt_id INTEGER REFERENCES attempts(id),
    source TEXT NOT NULL DEFAULT 'attempt',
    question_id TEXT NOT NULL,
    cert_id TEXT NOT NULL,
    domain_id TEXT NOT NULL,
    choice TEXT NOT NULL,
    correct INTEGER NOT NULL,
    answered_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_responses_attempt_q ON responses(attempt_id, question_id);
CREATE INDEX IF NOT EXISTS idx_responses_user_cert ON responses(user_id, cert_id, domain_id);
"""


def create_app(db_path=None):
    app = Flask(__name__, static_folder="static", static_url_path="/static")
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY") or ("dev-only-secret" if DEV_MODE else os.urandom(32)),
        DATABASE=db_path or os.environ.get("DATABASE_PATH", os.path.join(BASE_DIR, "certprep.db")),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "0") == "1",
        PERMANENT_SESSION_LIFETIME=dt.timedelta(days=30),
    )

    with open(os.path.join(DATA_DIR, "certs.json")) as f:
        certs = json.load(f)
    app.config["CERTS"] = {c["id"]: c for c in certs}
    app.config["CERT_ORDER"] = [c["id"] for c in certs]

    with app.app_context():
        db = get_db()
        db.executescript(SCHEMA)
        sync_questions(db, app.config["CERTS"])
        db.commit()

    app.teardown_appcontext(close_db)
    register_routes(app)
    return app


# ---------------------------------------------------------------- database

def get_db():
    from flask import current_app
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def load_question_files(certs):
    for cert_id, cert in certs.items():
        path = os.path.join(QUESTIONS_DIR, f"{cert_id}.json")
        if not os.path.exists(path):
            continue
        with open(path) as f:
            for q in json.load(f):
                yield cert, q


def sync_questions(db, certs):
    """Upsert every valid question from data/questions into the DB; deactivate removed ones."""
    seen = set()
    for cert, q in load_question_files(certs):
        if validate_question(q, cert):
            continue  # tools/validate_questions.py reports these
        seen.add(q["id"])
        db.execute(
            """INSERT INTO questions (id, cert_id, domain_id, difficulty, question, choices, answer, explanation, active)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
               ON CONFLICT(id) DO UPDATE SET cert_id=excluded.cert_id, domain_id=excluded.domain_id,
                 difficulty=excluded.difficulty, question=excluded.question, choices=excluded.choices,
                 answer=excluded.answer, explanation=excluded.explanation, active=1""",
            (q["id"], cert["id"], str(q["domain"]), int(q.get("difficulty", 2)), q["question"],
             json.dumps(q["choices"]), json.dumps(normalize_answer(q["answer"])), q["explanation"]),
        )
    existing = [r[0] for r in db.execute("SELECT id FROM questions WHERE active = 1")]
    for qid in existing:
        if qid not in seen:
            db.execute("UPDATE questions SET active = 0 WHERE id = ?", (qid,))


# ---------------------------------------------------------------- helpers

def error(message, status=400):
    return jsonify({"error": message}), status


def current_user():
    uid = session.get("user_id")
    if uid is None:
        return None
    if "user" not in g:
        g.user = get_db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    return g.user


def login_required(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if current_user() is None:
            return error("Sign in required", 401)
        return fn(*args, **kwargs)
    return wrapper


def premium_required(fn):
    @functools.wraps(fn)
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user()["is_premium"]:
            return error("Premium required", 402)
        return fn(*args, **kwargs)
    return wrapper


def public_question(row):
    """Question as sent to the browser: never includes the answer."""
    answer = json.loads(row["answer"])
    return {
        "id": row["id"],
        "domain_id": row["domain_id"],
        "question": row["question"],
        "choices": json.loads(row["choices"]),
        "select": len(answer),
    }


def reveal(row):
    return {"answer": json.loads(row["answer"]), "explanation": row["explanation"]}


def pick_weighted(db, cert, count, domain_id=None):
    """Pick `count` random active questions, distributed by the exam's domain weights."""
    if domain_id:
        rows = db.execute(
            "SELECT * FROM questions WHERE cert_id = ? AND domain_id = ? AND active = 1 ORDER BY RANDOM() LIMIT ?",
            (cert["id"], domain_id, count),
        ).fetchall()
        return rows
    pools = {
        d["id"]: db.execute(
            "SELECT * FROM questions WHERE cert_id = ? AND domain_id = ? AND active = 1 ORDER BY RANDOM()",
            (cert["id"], d["id"]),
        ).fetchall()
        for d in cert["domains"]
    }
    total_weight = sum(d["weight"] for d in cert["domains"])
    picked = []
    for d in cert["domains"]:
        want = round(count * d["weight"] / total_weight)
        picked.extend(pools[d["id"]][:want])
        pools[d["id"]] = pools[d["id"]][want:]
    # Top up from whatever is left if rounding or thin domains left us short.
    leftovers = [q for pool in pools.values() for q in pool]
    random.shuffle(leftovers)
    picked.extend(leftovers[: max(0, count - len(picked))])
    random.shuffle(picked)
    return picked[:count]


def domain_stats(db, user_id, cert):
    stats = []
    for d in cert["domains"]:
        rows = db.execute(
            "SELECT correct FROM responses WHERE user_id = ? AND cert_id = ? AND domain_id = ? "
            "ORDER BY answered_at DESC LIMIT ?",
            (user_id, cert["id"], d["id"], RECENT_PER_DOMAIN),
        ).fetchall()
        n = len(rows)
        correct = sum(r["correct"] for r in rows)
        smoothed = (correct + PRIOR_ACCURACY * PRIOR_WEIGHT) / (n + PRIOR_WEIGHT)
        stats.append({
            "domain_id": d["id"], "name": d["name"], "weight": d["weight"],
            "answered": n, "correct": correct,
            "accuracy": round(correct / n * 100, 1) if n else None,
            "estimate": round(smoothed * 100, 1),
        })
    return stats


def readiness(stats):
    total_weight = sum(s["weight"] for s in stats)
    score = sum(s["estimate"] * s["weight"] for s in stats) / total_weight
    answered = sum(s["answered"] for s in stats)
    if answered < 20:
        label = "Not enough data yet"
    elif score >= 85:
        label = "Exam ready"
    elif score >= 75:
        label = "Almost there"
    elif score >= 60:
        label = "Building up"
    else:
        label = "Just getting started"
    return {"score": round(score), "label": label, "answered": answered}


def daily_question_id(db, cert_id, day):
    ids = [r[0] for r in db.execute(
        "SELECT id FROM questions WHERE cert_id = ? AND active = 1 ORDER BY id", (cert_id,))]
    if not ids:
        return None
    digest = hashlib.sha256(f"{cert_id}:{day}".encode()).hexdigest()
    return ids[int(digest, 16) % len(ids)]


def grade(row, choice):
    return sorted(choice) == json.loads(row["answer"])


def attempt_summary(db, attempt, cert):
    ids = json.loads(attempt["question_ids"])
    rows = {r["id"]: r for r in db.execute(
        f"SELECT * FROM questions WHERE id IN ({','.join('?' * len(ids))})", ids)} if ids else {}
    responses = {r["question_id"]: r for r in db.execute(
        "SELECT * FROM responses WHERE attempt_id = ?", (attempt["id"],))}
    review = []
    per_domain = {d["id"]: {"name": d["name"], "correct": 0, "total": 0} for d in cert["domains"]}
    for qid in ids:
        row = rows.get(qid)
        if row is None:
            continue
        resp = responses.get(qid)
        is_correct = bool(resp and resp["correct"])
        per_domain[row["domain_id"]]["total"] += 1
        per_domain[row["domain_id"]]["correct"] += int(is_correct)
        review.append({
            **public_question(row), **reveal(row),
            "your_choice": json.loads(resp["choice"]) if resp else None,
            "correct": is_correct,
        })
    return {
        "id": attempt["id"], "cert_id": attempt["cert_id"], "mode": attempt["mode"],
        "score": attempt["score"], "finished": attempt["finished_at"] is not None,
        "correct": sum(1 for r in review if r["correct"]), "total": len(review),
        "domains": [{"domain_id": k, **v} for k, v in per_domain.items() if v["total"]],
        "review": review,
    }


# ---------------------------------------------------------------- routes

def register_routes(app):
    certs = app.config["CERTS"]

    @app.before_request
    def require_json_for_writes():
        # JSON-only API: browsers can't send cross-site application/json without a CORS
        # preflight, which (with SameSite=Lax cookies) blocks CSRF on state-changing routes.
        if request.method == "POST" and request.path.startswith("/api/") \
                and request.path != "/api/billing/webhook" and not request.is_json:
            return error("Expected application/json", 415)

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.get("/api/config")
    def config():
        return jsonify({
            "site_name": SITE_NAME,
            "billing_enabled": bool(os.environ.get("STRIPE_SECRET_KEY")),
            "dev_mode": DEV_MODE,
            "prices": {
                "monthly": os.environ.get("PRICE_MONTHLY_DISPLAY", "$19/month"),
                "annual": os.environ.get("PRICE_ANNUAL_DISPLAY", "$99/year"),
            },
        })

    # ---- auth

    @app.get("/api/me")
    def me():
        user = current_user()
        if user is None:
            return jsonify({"user": None})
        return jsonify({"user": {"id": user["id"], "email": user["email"], "is_premium": bool(user["is_premium"])}})

    @app.post("/api/signup")
    def signup():
        body = request.get_json(silent=True) or {}
        email = str(body.get("email", "")).strip().lower()
        password = str(body.get("password", ""))
        if not EMAIL_RE.match(email):
            return error("Enter a valid email address")
        if len(password) < 8:
            return error("Password must be at least 8 characters")
        db = get_db()
        try:
            cur = db.execute(
                "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
                (email, generate_password_hash(password), dt.datetime.utcnow().isoformat()),
            )
            db.commit()
        except sqlite3.IntegrityError:
            return error("An account with that email already exists", 409)
        session.clear()
        session.permanent = True
        session["user_id"] = cur.lastrowid
        return me()

    @app.post("/api/login")
    def login():
        body = request.get_json(silent=True) or {}
        email = str(body.get("email", "")).strip().lower()
        user = get_db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if user is None or not check_password_hash(user["password_hash"], str(body.get("password", ""))):
            return error("Incorrect email or password", 401)
        session.clear()
        session.permanent = True
        session["user_id"] = user["id"]
        return me()

    @app.post("/api/logout")
    def logout():
        session.clear()
        return jsonify({"ok": True})

    # ---- catalog & roadmap

    @app.get("/api/certs")
    def list_certs():
        counts = dict(get_db().execute(
            "SELECT cert_id, COUNT(*) FROM questions WHERE active = 1 GROUP BY cert_id").fetchall())
        out = [{**{k: certs[cid][k] for k in ("id", "vendor", "name", "code", "exam")},
                "question_count": counts.get(cid, 0)} for cid in app.config["CERT_ORDER"]]
        return jsonify({"certs": out, "total_questions": sum(counts.values())})

    @app.get("/api/certs/<cert_id>")
    def cert_detail(cert_id):
        cert = certs.get(cert_id)
        if cert is None:
            return error("Unknown certification", 404)
        db = get_db()
        counts = dict(db.execute(
            "SELECT domain_id, COUNT(*) FROM questions WHERE cert_id = ? AND active = 1 GROUP BY domain_id",
            (cert_id,)).fetchall())
        user = current_user()
        stats = domain_stats(db, user["id"], cert) if user else None
        roadmap = []
        for i, d in enumerate(cert["domains"]):
            item = {**d, "question_count": counts.get(d["id"], 0)}
            if stats:
                s = stats[i]
                item["answered"] = s["answered"]
                item["status"] = ("not_started" if s["answered"] == 0
                                  else "strong" if s["estimate"] >= 80
                                  else "in_progress")
            roadmap.append(item)
        return jsonify({"cert": {**cert, "question_count": sum(counts.values())}, "roadmap": roadmap,
                        "readiness": readiness(stats) if stats else None})

    # ---- practice & exam attempts

    @app.post("/api/attempts")
    @login_required
    def start_attempt():
        body = request.get_json(silent=True) or {}
        cert = certs.get(body.get("cert_id"))
        if cert is None:
            return error("Unknown certification", 404)
        mode = body.get("mode", "practice")
        if mode not in ("practice", "exam"):
            return error("mode must be 'practice' or 'exam'")
        domain_id = body.get("domain_id")
        if domain_id and domain_id not in {d["id"] for d in cert["domains"]}:
            return error("Unknown domain")
        if mode == "exam":
            count, time_limit = cert["exam"]["questions"], cert["exam"]["minutes"] * 60
            domain_id = None
        else:
            count, time_limit = max(1, min(int(body.get("count", 10)), 100)), None
        db = get_db()
        rows = pick_weighted(db, cert, count, domain_id)
        if not rows:
            return error("No questions available for this selection yet", 404)
        if mode == "exam" and len(rows) < count:
            # Scale the timer down so a thin question bank still feels like the real pace.
            time_limit = max(60, round(time_limit * len(rows) / count))
        cur = db.execute(
            "INSERT INTO attempts (user_id, cert_id, mode, question_ids, time_limit_sec, started_at) VALUES (?, ?, ?, ?, ?, ?)",
            (current_user()["id"], cert["id"], mode, json.dumps([r["id"] for r in rows]), time_limit, time.time()),
        )
        db.commit()
        return jsonify({"attempt_id": cur.lastrowid, "mode": mode, "time_limit_sec": time_limit,
                        "questions": [public_question(r) for r in rows]})

    def load_attempt(attempt_id):
        return get_db().execute("SELECT * FROM attempts WHERE id = ? AND user_id = ?",
                                (attempt_id, current_user()["id"])).fetchone()

    @app.get("/api/attempts/<int:attempt_id>")
    @login_required
    def get_attempt(attempt_id):
        attempt = load_attempt(attempt_id)
        if attempt is None:
            return error("Attempt not found", 404)
        summary = attempt_summary(get_db(), attempt, certs[attempt["cert_id"]])
        if not summary["finished"]:
            # Don't leak answers for an in-progress attempt.
            summary["questions"] = [{k: q[k] for k in ("id", "domain_id", "question", "choices", "select")}
                                    for q in summary.pop("review")]
            summary["time_limit_sec"] = attempt["time_limit_sec"]
            summary["started_at"] = attempt["started_at"]
        return jsonify(summary)

    @app.post("/api/attempts/<int:attempt_id>/answer")
    @login_required
    def answer(attempt_id):
        attempt = load_attempt(attempt_id)
        if attempt is None:
            return error("Attempt not found", 404)
        if attempt["finished_at"] is not None:
            return error("Attempt already finished", 409)
        body = request.get_json(silent=True) or {}
        qid = body.get("question_id")
        if qid not in json.loads(attempt["question_ids"]):
            return error("Question is not part of this attempt")
        try:
            choice = normalize_answer(body.get("choice"))
        except (TypeError, ValueError):
            return error("choice must be an int or list of ints")
        db = get_db()
        row = db.execute("SELECT * FROM questions WHERE id = ?", (qid,)).fetchone()
        correct = grade(row, choice)
        if attempt["mode"] == "practice":
            existing = db.execute("SELECT 1 FROM responses WHERE attempt_id = ? AND question_id = ?",
                                  (attempt_id, qid)).fetchone()
            if existing:
                return error("Already answered", 409)
        db.execute(
            """INSERT INTO responses (user_id, attempt_id, question_id, cert_id, domain_id, choice, correct, answered_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(attempt_id, question_id) DO UPDATE SET choice=excluded.choice,
                 correct=excluded.correct, answered_at=excluded.answered_at""",
            (current_user()["id"], attempt_id, qid, row["cert_id"], row["domain_id"],
             json.dumps(choice), int(correct), time.time()),
        )
        db.commit()
        if attempt["mode"] == "exam":
            return jsonify({"saved": True})  # graded only when the exam is submitted
        return jsonify({"correct": correct, **reveal(row)})

    @app.post("/api/attempts/<int:attempt_id>/finish")
    @login_required
    def finish(attempt_id):
        attempt = load_attempt(attempt_id)
        if attempt is None:
            return error("Attempt not found", 404)
        db = get_db()
        if attempt["finished_at"] is None:
            total = len(json.loads(attempt["question_ids"]))
            correct = db.execute("SELECT COALESCE(SUM(correct), 0) FROM responses WHERE attempt_id = ?",
                                 (attempt_id,)).fetchone()[0]
            db.execute("UPDATE attempts SET finished_at = ?, score = ? WHERE id = ?",
                       (time.time(), round(correct / total * 100, 1) if total else 0, attempt_id))
            db.commit()
            attempt = load_attempt(attempt_id)
        return jsonify(attempt_summary(db, attempt, certs[attempt["cert_id"]]))

    # ---- readiness (free) & analytics (premium)

    @app.get("/api/certs/<cert_id>/readiness")
    @login_required
    def readiness_route(cert_id):
        cert = certs.get(cert_id)
        if cert is None:
            return error("Unknown certification", 404)
        return jsonify(readiness(domain_stats(get_db(), current_user()["id"], cert)))

    @app.get("/api/certs/<cert_id>/analytics")
    @premium_required
    def analytics(cert_id):
        cert = certs.get(cert_id)
        if cert is None:
            return error("Unknown certification", 404)
        db = get_db()
        uid = current_user()["id"]
        stats = domain_stats(db, uid, cert)
        weak = sorted((s for s in stats if s["answered"] >= 3), key=lambda s: s["estimate"])[:3]
        trend = [{"attempt_id": r["id"], "mode": r["mode"], "score": r["score"],
                  "date": dt.datetime.utcfromtimestamp(r["finished_at"]).strftime("%Y-%m-%d")}
                 for r in db.execute(
                     "SELECT * FROM attempts WHERE user_id = ? AND cert_id = ? AND finished_at IS NOT NULL "
                     "ORDER BY finished_at DESC LIMIT 30", (uid, cert_id))][::-1]
        missed = [{"id": r["id"], "question": r["question"], "domain_id": r["domain_id"], "misses": r["misses"]}
                  for r in db.execute(
                      """SELECT q.id, q.question, q.domain_id, COUNT(*) AS misses
                         FROM responses r JOIN questions q ON q.id = r.question_id
                         WHERE r.user_id = ? AND r.cert_id = ? AND r.correct = 0
                         GROUP BY q.id ORDER BY misses DESC LIMIT 10""", (uid, cert_id))]
        return jsonify({"readiness": readiness(stats), "domains": stats, "weak_areas": weak,
                        "trend": trend, "most_missed": missed})

    # ---- daily challenge (premium)

    @app.get("/api/certs/<cert_id>/daily")
    @premium_required
    def daily(cert_id):
        if cert_id not in certs:
            return error("Unknown certification", 404)
        db = get_db()
        day = dt.date.today().isoformat()
        qid = daily_question_id(db, cert_id, day)
        if qid is None:
            return error("No questions available yet", 404)
        row = db.execute("SELECT * FROM questions WHERE id = ?", (qid,)).fetchone()
        prior = db.execute(
            "SELECT * FROM responses WHERE user_id = ? AND source = 'daily' AND question_id = ? AND answered_at >= ?",
            (current_user()["id"], qid, time.mktime(dt.date.today().timetuple()))).fetchone()
        out = {"day": day, "question": public_question(row), "answered": prior is not None}
        if prior:
            out.update({"correct": bool(prior["correct"]), "your_choice": json.loads(prior["choice"]), **reveal(row)})
        return jsonify(out)

    @app.post("/api/certs/<cert_id>/daily")
    @premium_required
    def daily_answer(cert_id):
        if cert_id not in certs:
            return error("Unknown certification", 404)
        db = get_db()
        qid = daily_question_id(db, cert_id, dt.date.today().isoformat())
        today_start = time.mktime(dt.date.today().timetuple())
        if db.execute("SELECT 1 FROM responses WHERE user_id = ? AND source = 'daily' AND question_id = ? AND answered_at >= ?",
                      (current_user()["id"], qid, today_start)).fetchone():
            return error("You already answered today's challenge", 409)
        try:
            choice = normalize_answer((request.get_json(silent=True) or {}).get("choice"))
        except (TypeError, ValueError):
            return error("choice must be an int or list of ints")
        row = db.execute("SELECT * FROM questions WHERE id = ?", (qid,)).fetchone()
        correct = grade(row, choice)
        db.execute(
            "INSERT INTO responses (user_id, source, question_id, cert_id, domain_id, choice, correct, answered_at) "
            "VALUES (?, 'daily', ?, ?, ?, ?, ?, ?)",
            (current_user()["id"], qid, cert_id, row["domain_id"], json.dumps(choice), int(correct), time.time()))
        db.commit()
        return jsonify({"correct": correct, **reveal(row)})

    # ---- flashcards (free, capped; unlimited for premium)

    @app.get("/api/certs/<cert_id>/flashcards")
    @login_required
    def flashcards(cert_id):
        cert = certs.get(cert_id)
        if cert is None:
            return error("Unknown certification", 404)
        limit = 50 if current_user()["is_premium"] else FREE_FLASHCARDS_PER_SESSION
        domain_id = request.args.get("domain_id") or None
        rows = pick_weighted(get_db(), cert, limit, domain_id)
        cards = []
        for r in rows:
            q = public_question(r)
            answer = json.loads(r["answer"])
            cards.append({"id": r["id"], "domain_id": r["domain_id"], "front": q["question"],
                          "back": " / ".join(q["choices"][i] for i in answer), "explanation": r["explanation"]})
        return jsonify({"cards": cards, "limited": not current_user()["is_premium"]})

    # ---- billing

    @app.post("/api/billing/checkout")
    @login_required
    def checkout():
        secret = os.environ.get("STRIPE_SECRET_KEY")
        if not secret:
            return error("Billing is not configured", 503)
        import stripe
        stripe.api_key = secret
        plan = (request.get_json(silent=True) or {}).get("plan", "annual")
        price = os.environ.get("STRIPE_PRICE_ANNUAL" if plan == "annual" else "STRIPE_PRICE_MONTHLY")
        if not price:
            return error("Price not configured", 503)
        user = current_user()
        base = request.host_url.rstrip("/")
        params = {
            "mode": "subscription",
            "line_items": [{"price": price, "quantity": 1}],
            "client_reference_id": str(user["id"]),
            "success_url": f"{base}/#/billing/success",
            "cancel_url": f"{base}/#/pricing",
        }
        if user["stripe_customer_id"]:
            params["customer"] = user["stripe_customer_id"]
        else:
            params["customer_email"] = user["email"]
        checkout_session = stripe.checkout.Session.create(**params)
        return jsonify({"url": checkout_session.url})

    @app.post("/api/billing/webhook")
    def stripe_webhook():
        secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
        if not secret:
            return error("Billing is not configured", 503)
        import stripe
        try:
            event = stripe.Webhook.construct_event(
                request.get_data(), request.headers.get("Stripe-Signature", ""), secret)
        except (ValueError, stripe.error.SignatureVerificationError):
            return error("Invalid signature", 400)
        db = get_db()
        obj = event["data"]["object"]
        if event["type"] == "checkout.session.completed" and obj.get("client_reference_id"):
            db.execute("UPDATE users SET is_premium = 1, stripe_customer_id = ? WHERE id = ?",
                       (obj.get("customer"), int(obj["client_reference_id"])))
        elif event["type"] == "customer.subscription.deleted":
            db.execute("UPDATE users SET is_premium = 0 WHERE stripe_customer_id = ?", (obj.get("customer"),))
        elif event["type"] == "customer.subscription.updated":
            active = obj.get("status") in ("active", "trialing")
            db.execute("UPDATE users SET is_premium = ? WHERE stripe_customer_id = ?",
                       (int(active), obj.get("customer")))
        db.commit()
        return jsonify({"received": True})

    @app.post("/api/billing/dev-toggle")
    @login_required
    def dev_toggle_premium():
        """Local testing only: flip premium on/off without Stripe. Disabled unless DEV_MODE=1."""
        if not DEV_MODE:
            return error("Not found", 404)
        db = get_db()
        db.execute("UPDATE users SET is_premium = 1 - is_premium WHERE id = ?", (current_user()["id"],))
        db.commit()
        g.pop("user", None)
        return me()


app = create_app()

if __name__ == "__main__":
    app.run(debug=DEV_MODE, port=int(os.environ.get("PORT", 5000)))
