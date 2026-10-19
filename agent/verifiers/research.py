"""Evaluate submitted results against private reference programs and observed evidence."""

from decimal import Decimal, ROUND_HALF_UP, localcontext

from agent.environment.research import arithmetic


def rounded(value, places):
    with localcontext() as context:
        context.prec = 50
        return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def evaluate(program, nodes):
    if "cell_ref" in program:
        return nodes[program["cell_ref"]]["value"]
    left, right = [evaluate(item, nodes) for item in program["operands"]]
    return arithmetic(program["operation"], left, right)


def validate_oracle(episode):
    for check in episode.case.oracle["checks"]:
        if check["kind"] == "research_result":
            actual = rounded(evaluate(check["reference_program"], episode.nodes), check["decimal_places"])
            if actual != Decimal(check["expected_value"]):
                raise ValueError("private expected value disagrees with reference program")
            for alternative in check["evidence_alternatives"]:
                if not alternative or any(ref not in episode.nodes for ref in alternative):
                    raise ValueError("private evidence references an unknown cell")


def verify(episode):
    if episode.status == "infrastructure_failure":
        return {"reward": None, "train_eligible": False, "reason": "infrastructure_failure"}
    if episode.status == "forbidden_action":
        return {"reward": -1.0, "train_eligible": True, "reason": "forbidden_action"}
    failure = {"reward": 0.0, "train_eligible": True}
    answer = episode.submission
    if episode.status != "submitted" or not answer:
        return {**failure, "reason": episode.status}
    if answer["answer_status"] not in episode.case.oracle["accepted_outcomes"]:
        return {**failure, "reason": "outcome"}
    refs = answer["evidence_refs"]
    if not refs or any(ref not in episode.observed for ref in refs):
        return {**failure, "reason": "unobserved_evidence"}
    expanded = set()
    for ref in refs:
        expanded.update(episode.nodes[ref]["evidence_refs"])
    checks = [check for check in episode.case.oracle["checks"] if check["kind"] == "research_result"]
    results = {result["metric_id"]: result for result in answer["results"]}
    if len(results) != len(answer["results"]) or set(results) != {check["metric_id"] for check in checks}:
        return {**failure, "reason": "result_set"}
    possible_supports = {frozenset()}
    for check in checks:
        result = results[check["metric_id"]]
        if any(result[key] != check[key] for key in ("unit", "entity_id", "scope", "periods")):
            return {**failure, "reason": "result_context"}
        value = Decimal(result["value"])
        expected = rounded(evaluate(check["reference_program"], episode.nodes), check["decimal_places"])
        if value != expected or value.as_tuple().exponent != -check["decimal_places"]:
            return {**failure, "reason": "numeric_result"}
        alternatives = [frozenset(item) for item in check["evidence_alternatives"] if set(item) <= expanded]
        if not alternatives:
            return {**failure, "reason": "supporting_cells"}
        possible_supports = {support | alternative for support in possible_supports for alternative in alternatives}
    if frozenset(expanded) not in possible_supports:
        return {**failure, "reason": "supporting_cells"}
    return {"reward": 1.0, "train_eligible": True, "reason": "all_checks_passed"}
