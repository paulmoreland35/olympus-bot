"""Question-bank helpers shared by the web app and the tools/ scripts."""


def normalize_answer(answer):
    """Answers are stored as a sorted list of choice indexes (supports 'choose two')."""
    if isinstance(answer, int):
        return [answer]
    return sorted(int(a) for a in answer)


def validate_question(q, cert):
    """Return a list of problems with one question dict (empty list = valid)."""
    problems = []
    for key in ("id", "domain", "question", "choices", "answer", "explanation"):
        if key not in q:
            problems.append(f"missing '{key}'")
    if problems:
        return problems
    domain_ids = {d["id"] for d in cert["domains"]}
    if str(q["domain"]) not in domain_ids:
        problems.append(f"unknown domain '{q['domain']}'")
    choices = q["choices"]
    if not isinstance(choices, list) or not 2 <= len(choices) <= 8:
        problems.append("choices must be a list of 2-8 options")
    elif len({c.strip().lower() for c in choices}) != len(choices):
        problems.append("duplicate choices")
    try:
        answer = normalize_answer(q["answer"])
        if not answer or any(a < 0 or a >= len(choices) for a in answer):
            problems.append("answer index out of range")
    except (TypeError, ValueError):
        problems.append("answer must be an int or list of ints")
    if not str(q["explanation"]).strip():
        problems.append("empty explanation")
    return problems
