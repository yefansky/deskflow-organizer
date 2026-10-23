"""Deterministic, screen-bounded non-overlapping panel placement."""
from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True)
class Rect:
    key: str
    x: int
    y: int
    width: int
    height: int

def overlaps(a: Rect, b: Rect, gap: int = 10) -> bool:
    return not (a.x + a.width + gap <= b.x or b.x + b.width + gap <= a.x or a.y + a.height + gap <= b.y or b.y + b.height + gap <= a.y)

def can_place_rectangle(candidate: Rect, occupied: list[Rect]) -> bool:
    """User placement never moves neighbours; touching edges are allowed."""
    return all(other.key == candidate.key or not overlaps(candidate, other, gap=0)
               for other in occupied)

def restore_rectangles(items: list[Rect], bounds: tuple[int, int, int, int], margin: int = 8) -> dict[str, Rect]:
    """Preserve a valid saved layout exactly; repair only invalid display profiles.

    Automatic packing has a visual gap. User placement only prohibits overlap,
    so repacking every saved layout with the automatic gap loses manual intent.
    """
    left, top, right, bottom = bounds
    valid = all(item.width > 0 and item.height > 0 and item.x >= left + margin
                and item.y >= top + margin and item.x + item.width <= right - margin
                and item.y + item.height <= bottom - margin
                and can_place_rectangle(item, items) for item in items)
    if valid: return {item.key: item for item in items}
    return pack_rectangles(items, bounds, margin=margin, gap=0)

def resize_rectangle(rect: Rect, edge: str, dx: int, dy: int, bounds: tuple[int, int, int, int], margin: int = 12, minimum_width: int = 210, minimum_height: int = 120) -> Rect:
    """Resize from any edge/corner while keeping the opposite edge fixed."""
    left, top, right, bottom = bounds
    x, y, width, height = rect.x, rect.y, rect.width, rect.height
    if edge in {"left", "top_left", "bottom_left"}:
        new_x = max(left + margin, min(rect.x + dx, rect.x + rect.width - minimum_width))
        width, x = rect.width + rect.x - new_x, new_x
    if edge in {"right", "top_right", "bottom_right"}:
        width = max(minimum_width, min(rect.width + dx, right - rect.x - margin))
    if edge in {"top", "top_left", "top_right"}:
        new_y = max(top + margin, min(rect.y + dy, rect.y + rect.height - minimum_height))
        height, y = rect.height + rect.y - new_y, new_y
    if edge in {"bottom", "bottom_left", "bottom_right"}:
        height = max(minimum_height, min(rect.height + dy, bottom - rect.y - margin))
    return Rect(rect.key, x, y, width, height)

def _right_biased_dynamic_pack(items: list[Rect], bounds: tuple[int, int, int, int], margin: int, gap: int) -> dict[str, Rect]:
    """Beam-search dynamic program over a right-to-left column skyline.

    Each state is the top and bottom occupied height of every column (rightmost
    first).  For every next panel, transitions enumerate contiguous spans and
    either a top or a bottom anchor.  Treating the lower edge as a first-class
    state lets short blocks use the visually valuable lower-right space instead
    of leaving it as a permanent hole.
    """
    left, top, right, bottom = bounds
    available_width, available_height = right - left - 2 * margin, bottom - top - 2 * margin
    cell_width = max(1, min(320, max(item.width for item in items) + gap))
    columns = max(1, available_width // cell_width)
    ordered = sorted(items, key=lambda item: (-item.x, -item.height, item.key))
    states: list[tuple[tuple[tuple[int, int], ...], float, dict[str, Rect]]] = [
        (tuple((0, 0) for _ in range(columns)), 0.0, {})
    ]
    for item in ordered:
        width, height = min(item.width, available_width), min(item.height, available_height)
        span = min(columns, max(1, (width + gap + cell_width - 1) // cell_width))
        next_states: dict[tuple[int, ...], tuple[float, dict[str, Rect]]] = {}
        for skyline, score, placements in states:
            for start in range(columns - span + 1):
                top_used = max(skyline[column][0] for column in range(start, start + span))
                bottom_used = max(skyline[column][1] for column in range(start, start + span))
                desired_start = max(0, min(columns - span, round((right - margin - width - item.x) / cell_width)))
                for anchor in ("top", "bottom"):
                    y_offset = top_used if anchor == "top" else available_height - bottom_used - height
                    fits = y_offset >= top_used and y_offset + height <= available_height - bottom_used
                    if not fits: continue
                    # Right columns are deliberately much cheaper; requested
                    # positions only resolve ties.  Top is preferred for the
                    # first item, but bottom anchors claim useful gaps once a
                    # right column has already started filling.
                    transition = (start * available_height * 18
                                  + abs(start - desired_start) * 140
                                  + (8 if anchor == "bottom" else 0)
                                  + min(top_used, bottom_used))
                    next_skyline = list(skyline)
                    for column in range(start, start + span):
                        old_top, old_bottom = next_skyline[column]
                        next_skyline[column] = (max(old_top, y_offset + height + gap) if anchor == "top" else old_top,
                                                max(old_bottom, available_height - y_offset + gap) if anchor == "bottom" else old_bottom)
                    state_key = tuple(next_skyline)
                    x = right - margin - start * cell_width - width
                    rect = Rect(item.key, x, top + margin + y_offset, width, height)
                    new_score = score + transition
                    old = next_states.get(state_key)
                    if old is None or new_score < old[0]:
                        next_states[state_key] = (new_score, {**placements, item.key: rect})
        # Beam limit keeps the dynamic program deterministic and fast even with
        # custom blocks, while retaining alternatives that a greedy pass loses.
        states = [(skyline, score, placements) for skyline, (score, placements) in sorted(next_states.items(), key=lambda entry: (entry[1][0], max(max(pair) for pair in entry[0]), entry[0]))[:160]]
    return min(states, key=lambda state: (state[1], max(max(pair) for pair in state[0])))[2]

def pack_rectangles(items: list[Rect], bounds: tuple[int, int, int, int], margin: int = 12, gap: int = 10, prefer_right: bool = False) -> dict[str, Rect]:
    """Place every rectangle fully inside bounds without overlap when space permits.

    Candidates form a fine grid and are ordered by distance from the requested
    position, preserving the user's intent while producing deterministic output.
    """
    if prefer_right and items:
        return _right_biased_dynamic_pack(items, bounds, margin, gap)
    left, top, right, bottom = bounds
    placed: list[Rect] = []
    result: dict[str, Rect] = {}
    for item in items:
        width, height = min(item.width, right-left-2*margin), min(item.height, bottom-top-2*margin)
        max_x, max_y = right-width-margin, bottom-height-margin
        wanted_x, wanted_y = max(left+margin, min(item.x, max_x)), max(top+margin, min(item.y, max_y))
        candidates = [(wanted_x, wanted_y)]
        candidates += [(x, y) for y in range(top+margin, max_y+1, 16) for x in range(left+margin, max_x+1, 16)]
        candidates = sorted(set(candidates), key=lambda point: (point[0]-wanted_x)**2 + (point[1]-wanted_y)**2)
        choice = next((Rect(item.key, x, y, width, height) for x, y in candidates if all(not overlaps(Rect(item.key, x, y, width, height), old, gap) for old in placed)), None)
        # A saturated screen cannot satisfy no-overlap; still keep the block reachable.
        choice = choice or Rect(item.key, wanted_x, wanted_y, width, height)
        placed.append(choice); result[item.key] = choice
    return result
