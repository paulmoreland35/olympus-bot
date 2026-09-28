"use strict";

const state = { user: null, config: null, certs: null };
const app = document.getElementById("app");
const LETTERS = "ABCDEFGH";
let activeTimer = null;

// ---------------------------------------------------------------- utilities

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

async function api(path, options = {}) {
  const init = { credentials: "same-origin", headers: {} };
  if (options.body !== undefined) {
    init.method = options.method || "POST";
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  } else if (options.method) {
    init.method = options.method;
    init.headers["Content-Type"] = "application/json";
    init.body = "{}";
  }
  const res = await fetch(path, init);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.error || `Request failed (${res.status})`);
    err.status = res.status;
    throw err;
  }
  return data;
}

function render(html) {
  app.innerHTML = html;
  window.scrollTo(0, 0);
}

function certById(id) {
  return (state.certs || []).find((c) => c.id === id);
}

function fmtTime(sec) {
  const s = Math.max(0, Math.floor(sec));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
  return (h ? `${h}:${String(m).padStart(2, "0")}` : `${m}`) + `:${String(r).padStart(2, "0")}`;
}

function gauge(score, label) {
  return `<div class="gauge">
    <div class="gauge-ring" style="--pct:${score}"><span>${score}</span></div>
    <div><h3>Exam readiness</h3><p class="muted">${esc(label)}</p>
    <p class="small muted">Weighted by the official domain percentages. Scores start low and rise as you answer more questions correctly.</p></div>
  </div>`;
}

function lockedCard(feature) {
  return `<div class="card locked stack">
    <span class="pill premium">PREMIUM</span>
    <h2>${esc(feature)} is a Premium feature</h2>
    <p class="muted">Upgrade to unlock domain analytics, the daily challenge, and unlimited flashcards.</p>
    <a class="btn primary" href="#/pricing">See plans</a>
  </div>`;
}

function requireLogin() {
  if (state.user) return true;
  location.hash = `#/login?next=${encodeURIComponent(location.hash)}`;
  return false;
}

// ---------------------------------------------------------------- nav

function renderNav() {
  const nav = document.getElementById("nav");
  if (state.user) {
    nav.innerHTML = `
      <a href="#/">Certifications</a>
      ${state.user.is_premium ? '<span class="pill premium">PREMIUM</span>' : '<a href="#/pricing">Go Premium</a>'}
      <span class="muted small nav-email">${esc(state.user.email)}</span>
      <a href="#" id="logout">Sign out</a>`;
    document.getElementById("logout").onclick = async (e) => {
      e.preventDefault();
      await api("/api/logout", { method: "POST" });
      state.user = null;
      renderNav();
      location.hash = "#/";
    };
  } else {
    nav.innerHTML = `<a href="#/">Certifications</a><a href="#/pricing">Pricing</a>
      <a href="#/login">Sign in</a><a class="btn primary" href="#/signup">Start free</a>`;
  }
}

// ---------------------------------------------------------------- pages

async function pageHome() {
  const data = await api("/api/certs");
  state.certs = data.certs;
  const cards = data.certs.map((c) => `
    <a class="card cert-card" href="#/cert/${c.id}">
      <div class="vendor">${esc(c.vendor)}</div>
      <h3>${esc(c.name)}</h3>
      <div class="muted small">${esc(c.code)} · ${c.exam.questions} questions · ${c.exam.minutes} min</div>
      <div class="row" style="margin-top:10px"><span class="pill">${c.question_count.toLocaleString()} practice questions</span></div>
    </a>`).join("");
  render(`
    <section class="hero">
      <h1>Pass your next IT certification.</h1>
      <p class="lead">Practice tests weighted like the real exam, explanations for every answer,
        and a readiness score that tells you when you're ready to book.</p>
      <div class="row">${state.user ? "" : '<a class="btn primary" href="#/signup">Start free</a>'}
        <a class="btn" href="#/pricing">Compare plans</a></div>
      <div class="stat-row">
        <div class="stat"><b>${data.total_questions.toLocaleString()}</b><span class="muted small">practice questions</span></div>
        <div class="stat"><b>${data.certs.length}</b><span class="muted small">certifications</span></div>
      </div>
    </section>
    <h2>Choose a certification</h2>
    <div class="grid">${cards}</div>`);
}

async function pageAuth(mode, params) {
  const isSignup = mode === "signup";
  render(`
    <form class="card auth stack" id="auth-form">
      <h2>${isSignup ? "Create your free account" : "Welcome back"}</h2>
      <div class="field"><label for="email">Email</label><input id="email" type="email" autocomplete="email" required></div>
      <div class="field"><label for="password">Password</label>
        <input id="password" type="password" minlength="8" autocomplete="${isSignup ? "new-password" : "current-password"}" required></div>
      <p class="error" id="auth-error"></p>
      <button class="btn primary block" type="submit">${isSignup ? "Create account" : "Sign in"}</button>
      <p class="small muted center">${isSignup ? 'Already have an account? <a href="#/login">Sign in</a>'
        : 'New here? <a href="#/signup">Create a free account</a>'}</p>
    </form>`);
  document.getElementById("auth-form").onsubmit = async (e) => {
    e.preventDefault();
    try {
      const data = await api(isSignup ? "/api/signup" : "/api/login", {
        body: { email: document.getElementById("email").value, password: document.getElementById("password").value },
      });
      state.user = data.user;
      renderNav();
      location.hash = params.get("next") || "#/";
    } catch (err) {
      document.getElementById("auth-error").textContent = err.message;
    }
  };
}

async function pageCert(certId) {
  const data = await api(`/api/certs/${certId}`);
  const { cert, roadmap, readiness } = data;
  const domainOptions = cert.domains.map((d) => `<option value="${d.id}">${d.id}.0 ${esc(d.name)}</option>`).join("");
  const steps = roadmap.map((d) => {
    const status = d.status || "not_started";
    const statusPill = { strong: '<span class="pill good">Strong</span>', in_progress: '<span class="pill warn">In progress</span>',
      not_started: '<span class="pill">Not started</span>' }[status];
    return `<li>
      <span class="step ${status}">${esc(d.id)}</span>
      <div style="flex:1;min-width:180px"><b>${esc(d.name)}</b>
        <div class="muted small">${d.weight}% of exam · ${d.question_count} question${d.question_count === 1 ? "" : "s"}${d.answered !== undefined ? ` · ${d.answered} answered` : ""}</div></div>
      ${state.user ? statusPill : ""}
      <button class="btn" data-domain="${d.id}">Practice</button>
    </li>`;
  }).join("");
  render(`
    <div class="stack">
      <div><div class="vendor">${esc(cert.vendor)} · ${esc(cert.code)}</div><h1>${esc(cert.name)}</h1>
        <p class="muted">${cert.exam.questions} questions · ${cert.exam.minutes} minutes · Passing: ${esc(cert.exam.passing)} ·
          ${cert.question_count.toLocaleString()} practice questions available</p></div>
      ${readiness ? `<div class="card">${gauge(readiness.score, readiness.label)}</div>` : ""}
      <div class="card stack">
        <h2>Start practicing</h2>
        <div class="row">
          <div style="flex:1;min-width:180px"><label for="count">Questions</label>
            <select id="count"><option>10</option><option selected>20</option><option>40</option><option>60</option></select></div>
          <div style="flex:2;min-width:220px"><label for="domain">Domain</label>
            <select id="domain"><option value="">All domains (weighted)</option>${domainOptions}</select></div>
        </div>
        <div class="row">
          <button class="btn primary" id="start-practice">Practice (instant feedback)</button>
          <button class="btn" id="start-exam">Full exam simulation (timed)</button>
        </div>
        <div class="row small">
          <a href="#/flashcards/${cert.id}">Flashcards</a> ·
          <a href="#/daily/${cert.id}">Daily challenge</a> ·
          <a href="#/analytics/${cert.id}">Performance analytics</a>
        </div>
      </div>
      <div class="card"><h2>Your roadmap</h2><p class="muted small">Work through each exam domain. A domain turns green once your estimated accuracy reaches 80%.</p>
        <ul class="roadmap">${steps}</ul></div>
    </div>`);

  const start = async (mode, domainId) => {
    if (!requireLogin()) return;
    try {
      const body = { cert_id: cert.id, mode };
      if (mode === "practice") {
        body.count = Number(document.getElementById("count").value);
        body.domain_id = domainId ?? (document.getElementById("domain").value || null);
      }
      const attempt = await api("/api/attempts", { body });
      sessionStorage.setItem(`attempt-${attempt.attempt_id}`, JSON.stringify(attempt));
      location.hash = `#/quiz/${attempt.attempt_id}`;
    } catch (err) {
      alert(err.message);
    }
  };
  document.getElementById("start-practice").onclick = () => start("practice");
  document.getElementById("start-exam").onclick = () => start("exam");
  app.querySelectorAll("[data-domain]").forEach((b) => { b.onclick = () => start("practice", b.dataset.domain); });
}

async function pageQuiz(attemptId) {
  if (!requireLogin()) return;
  let attempt;
  try {
    attempt = JSON.parse(sessionStorage.getItem(`attempt-${attemptId}`) || "null");
  } catch { attempt = null; }
  const server = await api(`/api/attempts/${attemptId}`);
  if (server.finished) { location.hash = `#/results/${attemptId}`; return; }
  if (!attempt) attempt = { attempt_id: server.id, mode: server.mode, time_limit_sec: server.time_limit_sec, questions: server.questions };

  const questions = attempt.questions;
  const isExam = attempt.mode === "exam";
  const answers = {};   // qid -> [choice indexes]
  const feedback = {};  // qid -> {correct, answer, explanation} (practice mode)
  let index = 0;
  const deadline = attempt.time_limit_sec ? (server.started_at || Date.now() / 1000) + attempt.time_limit_sec : null;

  const finish = async () => {
    clearInterval(activeTimer);
    await api(`/api/attempts/${attemptId}/finish`, { method: "POST" });
    sessionStorage.removeItem(`attempt-${attemptId}`);
    location.hash = `#/results/${attemptId}`;
  };

  const draw = () => {
    const q = questions[index];
    const fb = feedback[q.id];
    const picked = answers[q.id] || [];
    const choices = q.choices.map((c, i) => {
      let cls = picked.includes(i) ? "selected" : "";
      if (fb) cls = fb.answer.includes(i) ? "correct" : picked.includes(i) ? "wrong" : "";
      return `<button class="choice ${cls}" data-i="${i}" ${fb ? "disabled" : ""}><span class="key">${LETTERS[i]}</span><span>${esc(c)}</span></button>`;
    }).join("");
    const needsSubmit = q.select > 1 && !fb && !isExam;
    render(`
      <div class="card">
        <div class="quiz-head">
          <b>Question ${index + 1} of ${questions.length}</b>
          <span class="pill">${isExam ? "Exam simulation" : "Practice"}</span>
          <span class="spacer"></span>
          ${deadline ? '<span class="timer" id="timer"></span>' : ""}
        </div>
        <div class="question">${esc(q.question)}${q.select > 1 ? ` <span class="muted small">(Choose ${q.select})</span>` : ""}</div>
        <div id="choices">${choices}</div>
        ${fb ? `<div class="explain"><b class="${fb.correct ? "" : "error"}">${fb.correct ? "Correct" : "Incorrect"}.</b> ${esc(fb.explanation)}</div>` : ""}
        <div class="row" style="margin-top:16px">
          <button class="btn" id="prev" ${index === 0 ? "disabled" : ""}>Back</button>
          ${needsSubmit ? '<button class="btn primary" id="check">Check answer</button>' : ""}
          <span class="spacer"></span>
          ${index < questions.length - 1 ? '<button class="btn primary" id="next">Next</button>' : ""}
          <button class="btn" id="finish">${isExam ? "Submit exam" : "Finish"}</button>
        </div>
      </div>
      ${isExam ? `<div class="card" style="margin-top:16px"><div class="qnav">${questions.map((qq, i) =>
        `<button data-jump="${i}" class="${answers[qq.id] ? "answered" : ""} ${i === index ? "current" : ""}">${i + 1}</button>`).join("")}</div></div>` : ""}`);

    app.querySelectorAll("#choices .choice").forEach((btn) => {
      btn.onclick = async () => {
        const i = Number(btn.dataset.i);
        let next = answers[q.id] ? [...answers[q.id]] : [];
        if (q.select > 1) {
          next = next.includes(i) ? next.filter((x) => x !== i) : [...next, i].slice(-q.select);
        } else {
          next = [i];
        }
        answers[q.id] = next;
        if (isExam) {
          await api(`/api/attempts/${attemptId}/answer`, { body: { question_id: q.id, choice: next } });
          draw();
        } else if (q.select === 1) {
          await check();
        } else {
          draw();
        }
      };
    });
    const check = async () => {
      if ((answers[q.id] || []).length !== q.select) return;
      feedback[q.id] = await api(`/api/attempts/${attemptId}/answer`, { body: { question_id: q.id, choice: answers[q.id] } });
      draw();
    };
    const byId = (id) => document.getElementById(id);
    if (byId("check")) byId("check").onclick = check;
    byId("prev").onclick = () => { index -= 1; draw(); };
    if (byId("next")) byId("next").onclick = () => { index += 1; draw(); };
    byId("finish").onclick = () => {
      const unanswered = questions.filter((qq) => !answers[qq.id]).length;
      if (unanswered && !confirm(`${unanswered} question(s) unanswered. Finish anyway?`)) return;
      finish();
    };
    app.querySelectorAll("[data-jump]").forEach((b) => { b.onclick = () => { index = Number(b.dataset.jump); draw(); }; });
    tick();
  };

  const tick = () => {
    const el = document.getElementById("timer");
    if (!el || !deadline) return;
    const left = deadline - Date.now() / 1000;
    el.textContent = `⏱ ${fmtTime(left)}`;
    el.classList.toggle("low", left < 300);
    if (left <= 0) finish();
  };
  clearInterval(activeTimer);
  if (deadline) activeTimer = setInterval(tick, 1000);
  draw();
}

async function pageResults(attemptId) {
  if (!requireLogin()) return;
  const r = await api(`/api/attempts/${attemptId}`);
  if (!r.finished) { location.hash = `#/quiz/${attemptId}`; return; }
  const domains = r.domains.map((d) => {
    const pct = Math.round((d.correct / d.total) * 100);
    return `<div><div class="row"><span>${esc(d.domain_id)}.0 ${esc(d.name)}</span><span class="spacer"></span><b>${d.correct}/${d.total}</b></div>
      <div class="bar"><i style="width:${pct}%"></i></div></div>`;
  }).join("");
  const review = r.review.map((q, n) => `
    <div class="card">
      <div class="muted small">Question ${n + 1} · Domain ${esc(q.domain_id)}.0 · ${q.correct ? "✅ Correct" : "❌ Incorrect"}</div>
      <div class="question">${esc(q.question)}</div>
      ${q.choices.map((c, i) => {
        const cls = q.answer.includes(i) ? "correct" : (q.your_choice || []).includes(i) ? "wrong" : "";
        return `<div class="choice ${cls}"><span class="key">${LETTERS[i]}</span><span>${esc(c)}</span></div>`;
      }).join("")}
      <div class="explain">${esc(q.explanation)}</div>
    </div>`).join("");
  render(`
    <div class="stack">
      <div class="card">
        <h1>${Math.round(r.score)}%</h1>
        <p class="muted">${r.correct} of ${r.total} correct · ${r.mode === "exam" ? "Exam simulation" : "Practice"}</p>
        <div class="stack">${domains}</div>
        <div class="row" style="margin-top:16px">
          <a class="btn primary" href="#/cert/${r.cert_id}">Back to roadmap</a>
          <a class="btn" href="#/analytics/${r.cert_id}">View analytics</a>
        </div>
      </div>
      <h2>Review</h2>
      ${review}
    </div>`);
}

async function pageAnalytics(certId) {
  if (!requireLogin()) return;
  let a;
  try {
    a = await api(`/api/certs/${certId}/analytics`);
  } catch (err) {
    if (err.status === 402) {
      const r = await api(`/api/certs/${certId}/readiness`);
      render(`<div class="stack"><div class="card">${gauge(r.score, r.label)}</div>${lockedCard("Performance analytics")}</div>`);
      return;
    }
    throw err;
  }
  const domains = a.domains.map((d) => `
    <div><div class="row"><span>${esc(d.domain_id)}.0 ${esc(d.name)} <span class="muted small">(${d.weight}%)</span></span>
      <span class="spacer"></span><span class="small muted">${d.answered} answered</span>
      <b>${d.accuracy === null ? "—" : d.accuracy + "%"}</b></div>
      <div class="bar"><i style="width:${d.accuracy || 0}%"></i></div></div>`).join("");
  const weak = a.weak_areas.length
    ? a.weak_areas.map((w) => `<li>${esc(w.name)}: ${w.accuracy}% accuracy across ${w.answered} questions</li>`).join("")
    : '<li class="muted">Answer at least 3 questions in a domain to see weak areas.</li>';
  const trend = a.trend.length ? trendChart(a.trend) : '<p class="muted">Finish a practice test or exam to start your trend line.</p>';
  const missed = a.most_missed.map((m) => `<li>${esc(m.question)} <span class="muted small">(missed ${m.misses}×)</span></li>`).join("")
    || '<li class="muted">Nothing missed yet.</li>';
  render(`
    <div class="stack">
      <div class="card">${gauge(a.readiness.score, a.readiness.label)}</div>
      <div class="card stack"><h2>Accuracy by domain</h2>${domains}</div>
      <div class="card"><h2>Weak areas</h2><ul>${weak}</ul></div>
      <div class="card"><h2>Score trend</h2>${trend}</div>
      <div class="card"><h2>Most-missed questions</h2><ul>${missed}</ul></div>
    </div>`);
}

function trendChart(points) {
  const w = 600, h = 180, pad = 28;
  const xs = (i) => pad + (points.length === 1 ? (w - 2 * pad) / 2 : (i * (w - 2 * pad)) / (points.length - 1));
  const ys = (v) => h - pad - (v / 100) * (h - 2 * pad);
  const path = points.map((p, i) => `${i ? "L" : "M"}${xs(i).toFixed(1)},${ys(p.score).toFixed(1)}`).join(" ");
  const dots = points.map((p, i) => `<circle cx="${xs(i)}" cy="${ys(p.score)}" r="4" fill="var(--accent)"><title>${esc(p.date)}: ${p.score}% (${esc(p.mode)})</title></circle>`).join("");
  const grid = [0, 50, 100].map((v) => `<line x1="${pad}" x2="${w - pad}" y1="${ys(v)}" y2="${ys(v)}" stroke="var(--border)"/>
    <text x="4" y="${ys(v) + 4}" font-size="11" fill="var(--muted)">${v}</text>`).join("");
  return `<svg viewBox="0 0 ${w} ${h}" width="100%" role="img" aria-label="Score trend">${grid}
    <path d="${path}" fill="none" stroke="var(--accent)" stroke-width="2.5"/>${dots}</svg>`;
}

async function pageDaily(certId) {
  if (!requireLogin()) return;
  let d;
  try {
    d = await api(`/api/certs/${certId}/daily`);
  } catch (err) {
    if (err.status === 402) { render(lockedCard("The daily challenge")); return; }
    throw err;
  }
  const draw = (result) => {
    const q = d.question;
    const done = result || (d.answered ? d : null);
    render(`
      <div class="card">
        <div class="quiz-head"><b>Daily challenge · ${esc(d.day)}</b><span class="spacer"></span><a href="#/cert/${certId}">Back</a></div>
        <div class="question">${esc(q.question)}</div>
        ${q.choices.map((c, i) => {
          let cls = "";
          if (done) cls = done.answer.includes(i) ? "correct" : (done.your_choice || []).includes(i) ? "wrong" : "";
          return `<button class="choice ${cls}" data-i="${i}" ${done ? "disabled" : ""}><span class="key">${LETTERS[i]}</span><span>${esc(c)}</span></button>`;
        }).join("")}
        ${done ? `<div class="explain"><b>${done.correct ? "Correct!" : "Not quite."}</b> ${esc(done.explanation)}</div>
          <p class="muted small">Come back tomorrow for a new question.</p>` : ""}
      </div>`);
    if (!done) {
      app.querySelectorAll(".choice").forEach((b) => {
        b.onclick = async () => {
          const choice = [Number(b.dataset.i)];
          const res = await api(`/api/certs/${certId}/daily`, { body: { choice } });
          draw({ ...res, your_choice: choice });
        };
      });
    }
  };
  draw();
}

async function pageFlashcards(certId) {
  if (!requireLogin()) return;
  const { cards, limited } = await api(`/api/certs/${certId}/flashcards`);
  if (!cards.length) { render('<div class="card">No flashcards available yet.</div>'); return; }
  let i = 0, flipped = false;
  const draw = () => {
    const c = cards[i];
    render(`
      <div class="stack">
        <div class="row"><b>Flashcard ${i + 1} of ${cards.length}</b><span class="spacer"></span><a href="#/cert/${certId}">Back</a></div>
        <div class="card flashcard" id="fc">${flipped
          ? `<div><b>${esc(c.back)}</b><p class="muted small">${esc(c.explanation)}</p></div>`
          : `<div>${esc(c.front)}<p class="muted small">Tap to reveal</p></div>`}</div>
        <div class="row"><button class="btn" id="prev" ${i === 0 ? "disabled" : ""}>Previous</button><span class="spacer"></span>
          <button class="btn primary" id="next" ${i === cards.length - 1 ? "disabled" : ""}>Next</button></div>
        ${limited ? '<p class="muted small center">Free accounts get 10 cards per session. <a href="#/pricing">Go Premium</a> for unlimited.</p>' : ""}
      </div>`);
    document.getElementById("fc").onclick = () => { flipped = !flipped; draw(); };
    document.getElementById("prev").onclick = () => { i -= 1; flipped = false; draw(); };
    document.getElementById("next").onclick = () => { i += 1; flipped = false; draw(); };
  };
  draw();
}

async function pagePricing() {
  const cfg = state.config;
  let plan = "annual";
  const rows = [
    ["Practice tests & explanations", "All questions", "All questions"],
    ["Full exam simulations", "✓", "✓"],
    ["Exam readiness score", "✓", "✓"],
    ["Domain roadmap", "✓", "✓"],
    ["Performance analytics (domains, weak areas, trend)", "Score only", "✓"],
    ["Daily challenge", "—", "✓"],
    ["Flashcards", "10 per session", "Unlimited"],
  ];
  const draw = () => {
    render(`
      <div class="stack">
        <div class="center"><h1>Free to start. Premium when you're serious.</h1>
          <p class="muted">Every question is free. Premium adds the tools that show you exactly what to study next.</p></div>
        <div class="plans">
          <button class="card plan ${plan === "monthly" ? "selected" : ""}" data-plan="monthly" style="text-align:left;cursor:pointer;color:inherit;font:inherit">
            <div class="muted">Monthly</div><h2>${esc(cfg.prices.monthly)}</h2></button>
          <button class="card plan ${plan === "annual" ? "selected" : ""}" data-plan="annual" style="text-align:left;cursor:pointer;color:inherit;font:inherit">
            <div class="muted">Annual</div><h2>${esc(cfg.prices.annual)}</h2><span class="pill premium">Best value</span></button>
        </div>
        <div class="card table-wrap"><table class="compare">
          <tr><th>What you get</th><th>Free</th><th>Premium</th></tr>
          ${rows.map((r) => `<tr><td>${esc(r[0])}</td><td class="muted">${esc(r[1])}</td><td class="hl">${esc(r[2])}</td></tr>`).join("")}
        </table></div>
        <p class="error" id="pay-error"></p>
        ${state.user?.is_premium ? '<p class="center"><span class="pill premium">You are Premium</span></p>'
          : '<button class="btn primary block" id="go">Go Premium →</button>'}
        ${cfg.dev_mode && state.user ? '<button class="btn block" id="dev">Dev mode: toggle premium (no payment)</button>' : ""}
      </div>`);
    app.querySelectorAll("[data-plan]").forEach((b) => { b.onclick = () => { plan = b.dataset.plan; draw(); }; });
    const go = document.getElementById("go");
    if (go) go.onclick = async () => {
      if (!requireLogin()) return;
      try {
        const { url } = await api("/api/billing/checkout", { body: { plan } });
        location.href = url;
      } catch (err) {
        document.getElementById("pay-error").textContent = err.message;
      }
    };
    const dev = document.getElementById("dev");
    if (dev) dev.onclick = async () => {
      const data = await api("/api/billing/dev-toggle", { method: "POST" });
      state.user = data.user;
      renderNav();
      draw();
    };
  };
  draw();
}

async function pageBillingSuccess() {
  const data = await api("/api/me");
  state.user = data.user;
  renderNav();
  render(`<div class="card center stack"><h1>Welcome to Premium 🎉</h1>
    <p class="muted">If your badge doesn't show yet, refresh in a few seconds while we confirm payment.</p>
    <a class="btn primary" href="#/">Pick a certification</a></div>`);
}

// ---------------------------------------------------------------- router

async function route() {
  clearInterval(activeTimer);
  const [path, query] = location.hash.replace(/^#/, "").split("?");
  const params = new URLSearchParams(query || "");
  const parts = (path || "/").split("/").filter(Boolean);
  try {
    if (!state.certs && parts[0] && parts[0] !== "login" && parts[0] !== "signup") {
      state.certs = (await api("/api/certs")).certs;
    }
    switch (parts[0]) {
      case undefined: return await pageHome();
      case "login": return await pageAuth("login", params);
      case "signup": return await pageAuth("signup", params);
      case "cert": return await pageCert(parts[1]);
      case "quiz": return await pageQuiz(parts[1]);
      case "results": return await pageResults(parts[1]);
      case "analytics": return await pageAnalytics(parts[1]);
      case "daily": return await pageDaily(parts[1]);
      case "flashcards": return await pageFlashcards(parts[1]);
      case "pricing": return await pagePricing();
      case "billing": return await pageBillingSuccess();
      default: render('<div class="card">Page not found. <a href="#/">Go home</a></div>');
    }
  } catch (err) {
    if (err.status === 401) { state.user = null; renderNav(); requireLogin(); return; }
    render(`<div class="card"><p class="error">${esc(err.message)}</p><a href="#/">Go home</a></div>`);
  }
}

(async function init() {
  const [config, me] = await Promise.all([api("/api/config"), api("/api/me")]);
  state.config = config;
  state.user = me.user;
  document.title = config.site_name;
  document.getElementById("brand-name").textContent = config.site_name;
  renderNav();
  window.addEventListener("hashchange", route);
  route();
})();
