import re
from collections.abc import Sequence

from api.core.models import ConversationMessage


_FOLLOW_UP = re.compile(
    r"^(?:and\b|what about\b|tell me more\b|why\b|how so\b|which technologies\b)|"
    r"\b(?:it|that|this|they|their|those|these|its|he|his)\b",
    re.IGNORECASE,
)


def retrieval_query(question: str, history: Sequence[ConversationMessage]) -> str:
    """Add tightly bounded subject hints only for referential follow-ups."""
    if not history or not _FOLLOW_UP.search(question):
        return question

    prior_users = [item.content for item in history if item.role == "user"][-2:]
    hints = []
    header = f"Current question: {question}\n\nSUBJECT HINTS ONLY (not evidence):\n"
    fixed_labels = 2 * len("Prior user subject: ") + len("Prior assistant subject hint (unverified): ") + 4
    remaining = max(0, 5100 - len(header) - fixed_labels)
    user_budget = min(2000, remaining)
    assistant_budget = min(1000, max(0, remaining - user_budget))
    assistant = next((item.content for item in reversed(history) if item.role == "assistant"), "")[:assistant_budget]
    used = 0
    # Spend the available budget on the most recent subject first, then render
    # retained turns chronologically for readability.
    selected = []
    for text in reversed(prior_users):
        room = user_budget - used
        if room <= 0:
            break
        take = text[:room]
        selected.append(take)
        used += len(take)
    hints.extend(f"Prior user subject: {text}" for text in reversed(selected))
    if assistant:
        hints.append(f"Prior assistant subject hint (unverified): {assistant}")

    # Current question remains first and unchanged. Hint budgets plus labels cap
    # this string below 5100 characters.
    return f"Current question: {question}\n\nSUBJECT HINTS ONLY (not evidence):\n" + "\n".join(hints)
