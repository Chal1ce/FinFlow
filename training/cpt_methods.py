"""Document transformations adapted from Nemotron-CC; source facts remain authoritative."""

TRANSFORMS = {
    "distill": "Condense into information-dense prose, preserving every qualification needed to interpret retained facts",
    "textbook": "Reorganize the explicitly explained concepts and rules into a coherent educational passage",
    "knowledge_list": "Extract a concise factual list; each item must name its subject, metric, value, unit and period when present",
    "diverse_qa": "Write diverse reading-comprehension question/answer pairs, each fully answerable from this source",
}


def instruction(method, language):
    if method == "translate":
        return "Translate faithfully into " + language
    if method == "rewrite":
        return "Rewrite faithfully using varied phrasing in the original language"
    return TRANSFORMS[method] + ". Use the source language. Never add outside definitions, examples, causal claims or advice"
