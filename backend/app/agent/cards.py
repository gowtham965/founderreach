"""Startup cards in the chat: remember only match ids, and rebuild them with their current state on reload."""
from sqlalchemy.orm import Session

from app.models import Match, User
from app.services import matching


def card_refs(cards: list[dict]) -> list[dict]:
    """What to remember about a reply's cards: just match ids. A startup drafted then marked sent
    in one turn is remembered once, at its last position."""
    refs, seen = [], set()
    for c in reversed(cards):
        if c.get("type") == "matches":
            refs.append({"type": "matches", "ids": [i["match_id"] for i in c.get("items", [])]})
        elif c.get("item") and c["item"]["match_id"] not in seen:
            seen.add(c["item"]["match_id"])
            refs.append({"type": "draft", "id": c["item"]["match_id"]})
    return list(reversed(refs))


def rebuild_cards(db: Session, user: User, refs: list[dict] | None) -> list[dict]:
    """Cards for a stored reply, from current data. Matches that no longer exist are dropped."""
    out = []
    for ref in refs or []:
        if ref.get("type") == "matches":
            items = [matching.match_card(m) for m in (db.get(Match, i) for i in ref.get("ids", []))
                     if m is not None and m.user_id == user.id]
            if items:
                out.append({"type": "matches", "items": items})
        else:
            m = db.get(Match, ref.get("id"))
            if m is not None and m.user_id == user.id:
                out.append({"type": "draft", "item": matching.match_card(m)})
    return out
