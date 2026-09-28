"""Validate question banks and promote reviewed drafts into the live bank.

    python tools/validate_questions.py                 # check every live bank
    python tools/validate_questions.py --promote secplus
        # moves drafts from data/drafts/secplus.json into data/questions/secplus.json,
        # skipping any still marked "needs_review": true (fix or delete those first)
"""
import argparse
import collections
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bank import validate_question  # noqa: E402


def load(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return json.load(f)


def save(path, items):
    with open(path, "w") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)
        f.write("\n")


def norm(text):
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def check_bank(cert, items):
    problems = []
    ids = collections.Counter(q.get("id") for q in items)
    stems = collections.Counter(norm(q.get("question", "")) for q in items)
    for q in items:
        for p in validate_question(q, cert):
            problems.append(f"{q.get('id', '?')}: {p}")
        if ids[q.get("id")] > 1:
            problems.append(f"{q.get('id')}: duplicate id")
        if stems[norm(q.get("question", ""))] > 1:
            problems.append(f"{q.get('id')}: duplicate question text")
    return problems


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--promote", metavar="CERT_ID")
    args = parser.parse_args()
    certs = {c["id"]: c for c in load(os.path.join(ROOT, "data", "certs.json"))}

    if args.promote:
        cert = certs.get(args.promote) or sys.exit(f"Unknown cert '{args.promote}'")
        live_path = os.path.join(ROOT, "data", "questions", f"{cert['id']}.json")
        draft_path = os.path.join(ROOT, "data", "drafts", f"{cert['id']}.json")
        live, drafts = load(live_path), load(draft_path)
        live_stems = {norm(q["question"]) for q in live}
        ready, held = [], []
        for q in drafts:
            if q.get("needs_review") or validate_question(q, cert) or norm(q["question"]) in live_stems:
                held.append(q)
            else:
                ready.append({k: v for k, v in q.items() if k not in ("needs_review", "review_note")})
        save(live_path, live + ready)
        save(draft_path, held)
        print(f"Promoted {len(ready)} questions; {len(held)} remain in drafts (flagged, invalid, or duplicate).")
        return

    total, failed = 0, False
    for cert_id, cert in certs.items():
        items = load(os.path.join(ROOT, "data", "questions", f"{cert_id}.json"))
        total += len(items)
        problems = check_bank(cert, items)
        by_domain = collections.Counter(str(q.get("domain")) for q in items)
        coverage = ", ".join(f"{d['id']}:{by_domain.get(d['id'], 0)}" for d in cert["domains"])
        print(f"{cert_id:12} {len(items):6} questions  [domains {coverage}]")
        for p in problems:
            failed = True
            print(f"    ✗ {p}")
    print(f"\nTotal: {total} questions")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
