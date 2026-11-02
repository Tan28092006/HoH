"""Route existing practice skills according to the candidate's frozen budget."""


def practice_skills_for(role: str, tier: str = "low") -> list[str]:
    if role == "developer":
        return ["testing"] if tier == "low" else ["workflow", "testing"]
    if role == "qa":
        return (["testing"] if tier == "low" else ["testing", "ponytail-review"] if tier == "medium"
                else ["testing", "ponytail-review", "thermo-nuclear-code-quality-review", "pstack-interrogate"])
    return []


def review_delegation_for(plan: dict | None) -> dict | None:
    count = (plan or {}).get("advisory_reviewers_per_owner", 0)
    return ({"skill": "pstack-interrogate", "reviewers": count, "read_only": True,
             "recursive": False, "wait_for_all": True, "run_full_suite": False} if count else None)
