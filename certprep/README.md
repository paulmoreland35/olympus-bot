# CertPrep: IT certification practice platform

A freemium web app for passing major IT certifications: practice tests, full timed exam
simulations, an exam-readiness score, a domain roadmap, performance analytics, a daily
challenge, and flashcards. It covers 13 exams out of the box: CompTIA A+ (Core 1 and 2),
Network+, Security+, CySA+, PenTest+, SecurityX; ISC2 CC and CISSP; Cisco CCNA; AWS Cloud
Practitioner and Solutions Architect Associate; and Microsoft AZ-900.

Stack: Flask + SQLite backend, vanilla-JS single-page frontend (no build step), Stripe
subscriptions, and a Claude-powered tool for growing the question bank.

## Run it locally

```bash
cd certprep
pip install -r requirements.txt
DEV_MODE=1 python app.py          # open http://localhost:5000
python -m pytest -q tests         # run the test suite
```

`DEV_MODE=1` adds a **"Dev mode: toggle premium"** button on the Pricing page, so you can
try the paid features without Stripe. Never enable it in production.

## Free vs Premium

| Feature | Free | Premium |
|---|---|---|
| Practice tests with explanations (every question) | ✓ | ✓ |
| Timed full-exam simulation (weighted by domain) | ✓ | ✓ |
| Exam readiness score and domain roadmap | ✓ | ✓ |
| Performance analytics (per-domain accuracy, weak areas, trend, most-missed) | Score only | ✓ |
| Daily challenge | | ✓ |
| Flashcards | 10 per session | Unlimited |

You can move features between tiers by switching the `@login_required` and
`@premium_required` decorators in `app.py`.

## Growing the question bank (the path to thousands of questions)

Questions live in `data/questions/<cert_id>.json`, one file per exam, and are synced into
the database every time the app starts. The repo ships with **62 original starter questions**.
To scale up:

```bash
export ANTHROPIC_API_KEY=...
# 1. Draft questions with Claude. Each batch is checked by a separate "blind" call
#    that answers without seeing the key; disagreements get "needs_review": true
python tools/generate_questions.py secplus --per-domain 40

# 2. Review data/drafts/secplus.json: fix or delete anything flagged

# 3. Promote reviewed drafts into the live bank, then validate everything
python tools/validate_questions.py --promote secplus
python tools/validate_questions.py
```

At `--per-domain 400`, 13 exams come to roughly 20,000+ questions. Budget for API cost and,
more importantly, **human review time**. A wrong answer key does more damage to your
reputation than a small bank does.

Question format:

```json
{
  "id": "secplus-gen-00042",
  "domain": "4",
  "difficulty": 2,
  "question": "…",
  "choices": ["…", "…", "…", "…"],
  "answer": 1,
  "explanation": "Why B is right and why the others are wrong."
}
```

`answer` can also be a list (for example `[0, 2]`) for "choose two" questions.

### Content rules (important)

- **Write original questions.** Never use real exam questions or "brain dumps". It breaks
  the vendors' candidate agreements and can get your users' certifications revoked.
- **Check the domain weights and exam details in `data/certs.json`** against each vendor's
  current official exam objectives before launch. Exams are revised every few years.
- Don't publish testimonials, pass rates or "X questions" claims you can't back up. The
  homepage shows the real count straight from the database.

## Deploying (Railway, Render, Fly…)

Set the service's root directory to `certprep/`. The `Procfile` runs gunicorn. Set these
environment variables (see `.env.example`):

- `SECRET_KEY`: a long random string (**required**, or sessions reset on every restart)
- `DATABASE_PATH`: put SQLite on a persistent volume, e.g. `/data/certprep.db`
- `SESSION_COOKIE_SECURE=1` when served over HTTPS

### Taking payments with Stripe

1. Create a Product with two recurring Prices (monthly and annual) in Stripe.
2. Set `STRIPE_SECRET_KEY`, `STRIPE_PRICE_MONTHLY`, `STRIPE_PRICE_ANNUAL`, and the display
   strings `PRICE_MONTHLY_DISPLAY` / `PRICE_ANNUAL_DISPLAY`.
3. Add a webhook endpoint at `https://<your-domain>/api/billing/webhook` for
   `checkout.session.completed`, `customer.subscription.updated` and
   `customer.subscription.deleted`, then set `STRIPE_WEBHOOK_SECRET`.

## Project layout

```
certprep/
  app.py                    Flask app: auth, attempts, scoring, analytics, billing
  bank.py                   question validation helpers
  data/certs.json           exam catalog: domains, weights, exam length, passing score
  data/questions/*.json     live question banks
  data/drafts/*.json        AI-generated drafts awaiting review (created by the generator)
  static/                   single-page frontend (index.html, app.js, styles.css)
  tools/                    generate_questions.py, validate_questions.py
  tests/                    pytest suite
```

## How the readiness score works

For each domain, the score looks at your last 50 answers and takes the accuracy, smoothed
toward 25% (random guessing) with a weight of 8 answers, so a few lucky guesses can't inflate
it. Domain estimates are then weighted by the official exam percentages. 85+ reads as
"Exam ready".

## Ideas for next steps

- Performance-based questions (PBQs): drag-and-drop and simulated terminal tasks
- Training games (timed rapid-fire, port-number matching) built on the existing bank
- Spaced repetition for missed questions
- Password reset email, login rate limiting, and an admin page for reviewing drafts
- Move to Postgres once you have real traffic
