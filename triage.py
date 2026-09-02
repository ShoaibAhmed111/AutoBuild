# triage.py
"""
Cheap, mostly-heuristic task triage.

Goal: don't burn a full plan -> write -> verify -> repair LLM pipeline
(multiple model calls, full repo map + full contracts in every prompt) on
something that's obviously a one-file, low-risk tweak. And don't silently
attempt something that's genuinely too risky/cross-cutting to safely
auto-apply -- hand the user precise instructions instead.

HONESTY NOTE: this is pattern matching, not understanding. It will
misclassify sometimes -- a prompt that doesn't use any of the recognized
risk words but is actually a sweeping change will be treated as STANDARD
(the safe default), and a prompt that happens to mention "color" but is
actually complex will be sent down MECHANICAL and can still fail there
(mechanical tier still runs full verification -- it just skips the
multi-file planning step). This is a cost-saving heuristic layer in front
of the existing pipeline, not a replacement for verification.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class TaskTier(str, Enum):
    MECHANICAL = "MECHANICAL"        # single obvious file, low risk -> skip multi-file planning
    STANDARD = "STANDARD"            # normal plan -> write -> verify -> repair pipeline
    GUIDANCE_ONLY = "GUIDANCE_ONLY"  # too complex/risky to auto-apply -> instructions, no file writes


# Patterns that mean "do not attempt this automatically." These are
# deliberately broad/conservative -- false positives here just mean the user
# gets a guidance doc instead of an auto-apply for something that might have
# been fine; that's a much cheaper mistake than silently botching a schema
# migration or an auth change.
HIGH_RISK_PATTERNS = [
    r"\bmigrat(e|ion)s?\b",
    r"\brefactor (the )?(entire|whole)\b",
    r"\brewrite (the )?(whole|entire)\b",
    r"\bdatabase schema\b",
    r"\bschema migration\b",
    r"\bchange the api contract\b",
    r"\bbreaking change\b",
    r"\bauth(entication|orization)?\s+(system|flow|logic)\b",
    r"\bpayment\b",
    r"\bencrypt(ion)?\b",
    r"\bmicroservice\b",
    r"\bswitch (from|to) \w+\s+(to|from)\s+\w+\b",  # "switch from X to Y" -- framework/db swaps
    r"\bmulti[- ]tenant\b",
    r"\bhorizontal(ly)? scal(e|ing)\b",
]

# Patterns suggesting a small, localized, low-risk edit -- the kind of thing
# that shouldn't need a multi-file architectural plan.
MECHANICAL_PATTERNS = [
    r"\bcolou?r\b",
    r"\brename\b",
    r"\badd a (console\.log|print statement|comment)\b",
    r"\bfix (a |the )?typo\b",
    r"\bupdate (the )?(text|label|copy|wording)\b",
    r"\badd a button\b",
    r"\b(css|padding|margin|font[- ]size|border[- ]radius)\b",
    r"\btweak\b",
    r"\bstyle\b",
]

_FILE_EXT_RE = re.compile(r"\.(py|js|ts|tsx|jsx|html|css|json)\b")


@dataclass
class TriageResult:
    tier: TaskTier
    reason: str


def classify(user_prompt: str) -> TriageResult:
    text = user_prompt.lower()

    for pat in HIGH_RISK_PATTERNS:
        if re.search(pat, text):
            return TriageResult(
                TaskTier.GUIDANCE_ONLY,
                f"Matched a high-risk/cross-cutting pattern ('{pat.strip(chr(92)+chr(98))}'). "
                f"Producing instructions instead of auto-applying changes.",
            )

    mentions_multiple_files = len(_FILE_EXT_RE.findall(text)) > 1
    is_short = len(user_prompt.split()) < 25
    matched_mechanical = next((p for p in MECHANICAL_PATTERNS if re.search(p, text)), None)

    if matched_mechanical and is_short and not mentions_multiple_files:
        return TriageResult(
            TaskTier.MECHANICAL,
            f"Short, single-concern request matched a mechanical pattern ('{matched_mechanical}'); "
            f"skipping multi-file planning.",
        )

    return TriageResult(TaskTier.STANDARD, "Default: full plan -> write -> verify -> repair pipeline.")


def pick_mechanical_target_file(user_prompt: str, repo_map: str) -> tuple[str | None, str]:
    """
    Heuristic single-file selection for the MECHANICAL tier: scores each
    [FILE: ...] block in the repo map by keyword overlap with the prompt.

    Returns (relative_path_or_None, reason). Returns None if no file scores
    clearly higher than the rest -- callers MUST fall back to the STANDARD
    (full planning) tier in that case rather than guessing a file, since a
    wrong guess here means writing to the wrong file with no planning
    safety net.
    """
    prompt_words = set(re.findall(r"[a-zA-Z]{3,}", user_prompt.lower()))
    blocks = re.split(r"\n(?=\[FILE: )", repo_map)

    scores: list[tuple[str, int]] = []
    for block in blocks:
        m = re.match(r"\[FILE: (.+?)\]", block.strip())
        if not m:
            continue
        file_path = m.group(1).strip()
        block_words = set(re.findall(r"[a-zA-Z]{3,}", block.lower()))
        path_words = set(re.findall(r"[a-zA-Z]{3,}", file_path.lower()))
        score = len(prompt_words & block_words) + 2 * len(prompt_words & path_words)
        if score > 0:
            scores.append((file_path, score))

    if not scores:
        return None, "No file in the repo map shared any keywords with the request."

    scores.sort(key=lambda x: x[1], reverse=True)
    top_file, top_score = scores[0]

    if len(scores) > 1 and scores[1][1] == top_score:
        return None, f"Multiple files tied for relevance ({top_file} and {scores[1][0]}); ambiguous."

    return top_file, f"Selected by keyword overlap (score={top_score})."
