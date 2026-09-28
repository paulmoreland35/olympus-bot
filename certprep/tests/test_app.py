import json
import os

from bank import validate_question

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_seed_banks_are_valid():
    certs = {c["id"]: c for c in json.load(open(os.path.join(ROOT, "data", "certs.json")))}
    for cert_id, cert in certs.items():
        path = os.path.join(ROOT, "data", "questions", f"{cert_id}.json")
        for q in json.load(open(path)):
            assert validate_question(q, cert) == [], q["id"]


def test_catalog_lists_certs_with_counts(client):
    data = client.get("/api/certs").get_json()
    assert data["total_questions"] > 0
    assert all(c["question_count"] > 0 for c in data["certs"])


def test_signup_login_and_duplicates(client, user):
    assert client.post("/api/signup", json={"email": "a@example.com", "password": "password123"}).status_code == 409
    client.post("/api/logout", json={})
    assert client.post("/api/login", json={"email": "a@example.com", "password": "wrong-pass"}).status_code == 401
    assert client.post("/api/login", json={"email": "A@example.com", "password": "password123"}).status_code == 200


def test_posts_require_json(client):
    r = client.post("/api/login", data={"email": "x", "password": "y"})
    assert r.status_code == 415


def test_practice_attempt_gives_feedback_and_hides_answers(client, user):
    r = client.post("/api/attempts", json={"cert_id": "secplus", "mode": "practice", "count": 5}).get_json()
    q = r["questions"][0]
    assert "answer" not in q and "explanation" not in q
    fb = client.post(f"/api/attempts/{r['attempt_id']}/answer", json={"question_id": q["id"], "choice": 0}).get_json()
    assert "correct" in fb and "explanation" in fb
    again = client.post(f"/api/attempts/{r['attempt_id']}/answer", json={"question_id": q["id"], "choice": 1})
    assert again.status_code == 409
    in_progress = client.get(f"/api/attempts/{r['attempt_id']}").get_json()
    assert "review" not in in_progress and all("answer" not in x for x in in_progress["questions"])


def test_exam_attempt_grades_on_submit(client, user):
    r = client.post("/api/attempts", json={"cert_id": "netplus", "mode": "exam"}).get_json()
    assert r["time_limit_sec"] > 0
    for q in r["questions"]:
        res = client.post(f"/api/attempts/{r['attempt_id']}/answer", json={"question_id": q["id"], "choice": 0}).get_json()
        assert res == {"saved": True}
    done = client.post(f"/api/attempts/{r['attempt_id']}/finish", json={}).get_json()
    assert done["finished"] and done["total"] == len(r["questions"])
    assert all("answer" in item for item in done["review"])
    late = client.post(f"/api/attempts/{r['attempt_id']}/answer", json={"question_id": r["questions"][0]["id"], "choice": 1})
    assert late.status_code == 409


def test_other_users_cannot_see_attempt(client, user):
    r = client.post("/api/attempts", json={"cert_id": "secplus", "mode": "practice", "count": 3}).get_json()
    client.post("/api/logout", json={})
    client.post("/api/signup", json={"email": "b@example.com", "password": "password123"})
    assert client.get(f"/api/attempts/{r['attempt_id']}").status_code == 404


def test_premium_gating_and_readiness(client, user):
    assert client.get("/api/certs/secplus/analytics").status_code == 402
    assert client.get("/api/certs/secplus/daily").status_code == 402
    readiness = client.get("/api/certs/secplus/readiness").get_json()
    assert 0 <= readiness["score"] <= 100
    assert client.post("/api/billing/dev-toggle", json={}).get_json()["user"]["is_premium"]
    analytics = client.get("/api/certs/secplus/analytics").get_json()
    assert len(analytics["domains"]) == 5
    daily = client.get("/api/certs/secplus/daily").get_json()
    assert not daily["answered"]
    assert "correct" in client.post("/api/certs/secplus/daily", json={"choice": 0}).get_json()
    assert client.post("/api/certs/secplus/daily", json={"choice": 0}).status_code == 409
    assert client.get("/api/certs/secplus/daily").get_json()["answered"]


def test_readiness_rewards_correct_answers(client, user):
    import sqlite3
    r = client.post("/api/attempts", json={"cert_id": "secplus", "mode": "practice", "count": 7}).get_json()
    before = client.get("/api/certs/secplus/readiness").get_json()["score"]
    # Answer everything correctly by reading the key from the bank file.
    bank = {q["id"]: q for q in json.load(open(os.path.join(ROOT, "data", "questions", "secplus.json")))}
    for q in r["questions"]:
        client.post(f"/api/attempts/{r['attempt_id']}/answer", json={"question_id": q["id"], "choice": bank[q["id"]]["answer"]})
    after = client.get("/api/certs/secplus/readiness").get_json()["score"]
    assert after > before


def test_flashcards_capped_for_free_users(client, user):
    data = client.get("/api/certs/secplus/flashcards").get_json()
    assert data["limited"] and len(data["cards"]) <= 10
