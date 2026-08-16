"""One token -> the kana faces the decoder may take."""


def token_branches(token: dict) -> list[str]:
    if token["kind"] == "plain":
        return [token["kana"]]
    return list(token["faces"].values())
