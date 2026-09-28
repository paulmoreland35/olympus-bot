"""Draft new practice questions with Claude, then cross-check the answer key.

Drafts are written to data/drafts/<cert_id>.json. They are NOT served to users until
you review them and run:  python tools/validate_questions.py --promote <cert_id>

Usage:
    export ANTHROPIC_API_KEY=...
    python tools/generate_questions.py secplus --per-domain 20
    python tools/generate_questions.py secplus --domain 4 --count 50

How it keeps quality up:
  * questions are generated in small batches, per exam domain, with recent stems passed
    back in so Claude avoids repeating itself;
  * near-duplicates of existing live or draft questions are dropped;
  * every batch goes through a separate "blind" verification call that answers the
    questions without seeing the key. Disagreements are flagged ("needs_review": true)
    so a human looks at them first.
"""
import argparse
import json
import os
import re
import sys

import anthropic

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

MODEL = os.environ.get("QUESTION_MODEL", "claude-opus-5-5")
BATCH_SIZE = 10

QUESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "choices": {"type": "array", "items": {"type": "string"}},
                    "answer": {"type": "integer"},
                    "explanation": {"type": "string"},
                    "difficulty": {"type": "integer", "enum": [1, 2, 3]},
                    "objective": {"type": "string"},
                },
                "required": ["question", "choices", "answer", "explanation", "difficulty", "objective"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["questions"],
    "additionalProperties": False,
}

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "answers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "answer": {"type": "integer"},
                    "ambiguous": {"type": "boolean"},
                    "note": {"type": "string"},
                },
                "required": ["index", "answer", "ambiguous", "note"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["answers"],
    "additionalProperties": False,
}

GENERATOR_SYSTEM = """You write original practice questions for IT certification candidates.

Rules:
- Write ORIGINAL questions that test the published exam objectives. Never reproduce real exam
  questions or "brain dump" content; candidates agree to exam NDAs and using leaked items is harmful.
- Exactly 4 choices, exactly one correct. Distractors must be plausible to someone who half-knows
  the topic, but clearly wrong to an expert. Avoid "all of the above" / "none of the above".
- Prefer realistic scenario questions ("A technician notices... what should they do FIRST?")
  over trivia, and mix in some direct recall.
- Vary which letter is correct.
- The explanation says why the right answer is right AND briefly why the tempting distractors are wrong.
- Be factually precise and current for the exam version given. If a fact is version-dependent or
  disputed, choose a different topic rather than guess.
- "objective" is a short label for the exam objective or topic the question tests."""


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)


def norm(text):
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def call_json(client, system, prompt, schema, effort="high"):
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": prompt}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Model declined this request; try a different domain or wording.")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Response was cut off; lower --batch-size.")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


def generate_batch(client, cert, domain, n, avoid):
    avoid_text = "\n".join(f"- {s}" for s in avoid[-60:]) or "(none yet)"
    prompt = f"""Certification: {cert['vendor']} {cert['name']} (exam {cert['code']})
Domain {domain['id']}.0: {domain['name']} ({domain['weight']}% of the exam)

Write {n} new questions for this domain. Mix difficulty: roughly 30% easy (1), 50% medium (2), 20% hard (3).

Do not duplicate or closely paraphrase these existing questions:
{avoid_text}"""
    return call_json(client, GENERATOR_SYSTEM, prompt, QUESTION_SCHEMA)["questions"]


def verify_batch(client, cert, questions):
    """Answer the questions blind; return {index: (answer, ambiguous, note)}."""
    listing = "\n\n".join(
        f"[{i}] {q['question']}\n" + "\n".join(f"  {j}) {c}" for j, c in enumerate(q["choices"]))
        for i, q in enumerate(questions))
    prompt = f"""You are an expert reviewer for the {cert['vendor']} {cert['name']} ({cert['code']}) exam.
Answer each question below by the 0-based index of the single best choice. Set "ambiguous" to true if
more than one choice is defensible, the question is factually wrong, or it depends on an outdated fact,
and explain in "note".

{listing}"""
    result = call_json(client, "You carefully verify certification practice questions.", prompt, VERIFY_SCHEMA)
    return {a["index"]: a for a in result["answers"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("cert_id")
    parser.add_argument("--domain", help="Only this domain id (default: all domains)")
    parser.add_argument("--count", type=int, help="Questions for --domain")
    parser.add_argument("--per-domain", type=int, default=10, help="Questions per domain when --domain is not set")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("--no-verify", action="store_true", help="Skip the blind verification pass")
    args = parser.parse_args()

    certs = {c["id"]: c for c in load_json(os.path.join(ROOT, "data", "certs.json"), [])}
    cert = certs.get(args.cert_id)
    if cert is None:
        sys.exit(f"Unknown cert '{args.cert_id}'. Options: {', '.join(certs)}")

    live_path = os.path.join(ROOT, "data", "questions", f"{cert['id']}.json")
    draft_path = os.path.join(ROOT, "data", "drafts", f"{cert['id']}.json")
    os.makedirs(os.path.dirname(draft_path), exist_ok=True)
    live = load_json(live_path, [])
    drafts = load_json(draft_path, [])
    seen = {norm(q["question"]) for q in live + drafts}
    next_num = 1 + max([int(m.group(1)) for q in live + drafts
                        if (m := re.search(r"-gen-(\d+)$", q["id"]))] or [0])

    if args.domain:
        plan = [(d, args.count or args.per_domain) for d in cert["domains"] if d["id"] == args.domain]
        if not plan:
            sys.exit(f"Unknown domain '{args.domain}'")
    else:
        plan = [(d, args.per_domain) for d in cert["domains"]]

    client = anthropic.Anthropic()
    added = flagged = 0
    for domain, target in plan:
        avoid = [q["question"] for q in live + drafts if str(q["domain"]) == domain["id"]]
        made = 0
        while made < target:
            n = min(args.batch_size, target - made)
            print(f"Domain {domain['id']} ({domain['name']}): generating {n}...", flush=True)
            try:
                batch = generate_batch(client, cert, domain, n, avoid)
            except (anthropic.APIError, RuntimeError, json.JSONDecodeError) as exc:
                print(f"  batch failed: {exc}", file=sys.stderr)
                break
            batch = [q for q in batch if len(q["choices"]) == 4 and 0 <= q["answer"] < 4
                     and norm(q["question"]) not in seen]
            checks = {}
            if batch and not args.no_verify:
                try:
                    checks = verify_batch(client, cert, batch)
                except (anthropic.APIError, RuntimeError, json.JSONDecodeError) as exc:
                    print(f"  verification failed, flagging batch for review: {exc}", file=sys.stderr)
            for i, q in enumerate(batch):
                check = checks.get(i)
                needs_review = args.no_verify or check is None or check["ambiguous"] or check["answer"] != q["answer"]
                item = {
                    "id": f"{cert['id']}-gen-{next_num:05d}",
                    "domain": domain["id"],
                    "difficulty": q["difficulty"],
                    "objective": q["objective"],
                    "question": q["question"],
                    "choices": q["choices"],
                    "answer": q["answer"],
                    "explanation": q["explanation"],
                    "needs_review": needs_review,
                }
                if check and needs_review:
                    item["review_note"] = f"Verifier chose {check['answer']}: {check['note']}"
                drafts.append(item)
                seen.add(norm(q["question"]))
                avoid.append(q["question"])
                next_num += 1
                added += 1
                flagged += int(needs_review)
            made += max(1, len(batch))
            with open(draft_path, "w") as f:
                json.dump(drafts, f, indent=2, ensure_ascii=False)
                f.write("\n")

    print(f"\nAdded {added} draft questions to {os.path.relpath(draft_path, ROOT)} ({flagged} flagged for review).")
    print(f"Review them, then run: python tools/validate_questions.py --promote {cert['id']}")


if __name__ == "__main__":
    main()
