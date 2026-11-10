"""Token faces and the shared Stage 2 literal/reading boundary."""


def is_literal_face(text: str) -> bool:
    """NICOLA readings are hiragana U+3041..U+3096 and ー; other faces are literal.

    This is the same set used for plain faces in Stage 2A. See docs/STAGE2.md.
    """
    return not all("ぁ" <= ch <= "ゖ" or ch == "ー" for ch in text)


def literal_text(text: str) -> str:
    if len(text) != 1:
        raise ValueError("a literal face must contain exactly one character")
    return "、" if text == "，" else text


def token_branches(token: dict) -> list[str]:
    if token["kind"] == "literal":
        return [token["text"]]
    if token["kind"] == "plain":
        return [token["kana"]]
    return list(token["faces"].values())
