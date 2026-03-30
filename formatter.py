TWITTER_LIMIT = 280
BLUESKY_LIMIT = 300


def _split_text(text: str, limit: int) -> list[str]:
    """
    Split text into chunks fitting within limit characters.
    Non-final chunks get an ellipsis + double newline + n/total appended.
    Final chunk has no numbering.
    The overhead of the suffix is accounted for before filling each chunk.
    """
    # First pass: figure out how many chunks we need.
    # We do this by simulating the split greedily.
    chunks = _greedy_split(text, limit)
    total = len(chunks)

    if total == 1:
        return chunks

    # Second pass: re-split knowing total, so numbering overhead is exact.
    # Numbering format: "\n\nn/total" — length = 2 + len(str(n)) + 1 + len(str(total))
    # Ellipsis: 1 char (…)
    # Only non-final chunks carry this overhead.
    result = []
    remaining = text
    prev_ended_at_sentence = False
    for i in range(1, total + 1):
        is_last = (i == total)
        prefix = "…" if (i > 1 and not prev_ended_at_sentence) else ""
        if is_last:
            result.append((prefix + remaining.strip()) if prefix else remaining.strip())
            break
        overhead = len(f"…\n\n{i}/{total}") + len(prefix)
        available = limit - overhead
        chunk, remaining = _take_chunk(remaining.strip(), available)
        chunk_stripped = chunk.strip()
        ellipsis = "" if chunk_stripped and chunk_stripped[-1] in ".!?" else "…"
        prev_ended_at_sentence = (ellipsis == "")
        result.append(prefix + chunk_stripped + ellipsis + f"\n\n{i}/{total}")

    return result


def _greedy_split(text: str, limit: int) -> list[str]:
    """Split greedily without numbering to estimate chunk count."""
    chunks = []
    remaining = text.strip()
    prev_ended_at_sentence = False
    while remaining:
        is_first = len(chunks) == 0
        leading = 0 if (is_first or prev_ended_at_sentence) else 1  # leading ellipsis on chunks 2+
        if len(remaining) + leading <= limit:
            chunks.append(remaining)
            break
        chunk, remaining = _take_chunk(remaining, limit - 1 - leading)  # -1 for trailing ellipsis
        chunk_stripped = chunk.strip()
        ellipsis = "" if chunk_stripped and chunk_stripped[-1] in ".!?" else "…"
        prev_ended_at_sentence = (ellipsis == "")
        chunks.append(chunk_stripped + ellipsis)
        remaining = remaining.strip()
    return chunks


def _take_chunk(text: str, max_chars: int) -> tuple[str, str]:
    """
    Take up to max_chars from text, breaking at a word boundary.
    Returns (chunk, remainder).
    Prefers sentence boundaries (. ! ?) then word boundaries.
    """
    if len(text) <= max_chars:
        return text, ""

    window = text[:max_chars]

    # Prefer paragraph break
    idx = window.rfind("\n\n")
    if idx > max_chars // 2:
        return text[:idx], text[idx + 2:]

    # Try to break at sentence boundary
    for punct in (".", "!", "?"):
        idx = window.rfind(punct)
        if idx > max_chars // 2:
            return text[:idx + 1], text[idx + 1:]

    # Fall back to word boundary
    idx = window.rfind(" ")
    if idx == -1:
        return text[:max_chars], text[max_chars:]
    return text[:idx], text[idx + 1:]


def apply_config(text: str, prefix: str, suffix: str) -> str:
    parts = []
    if prefix:
        parts.append(prefix)
    parts.append(text)
    if suffix:
        parts.append(suffix)
    return "\n\n".join(parts) if (prefix or suffix) else text


def format_platform(text: str, platform: str, config: dict) -> list[str]:
    """
    Apply prefix/suffix and split for a given platform.
    text should already have handle substitutions applied.
    """
    cfg = config.get(platform, {})
    prefix = cfg.get("prefix", "").strip()
    suffix = cfg.get("suffix", "").strip()
    full_text = apply_config(text, prefix, suffix)

    if platform == "twitter":
        return _split_text(full_text, TWITTER_LIMIT)
    elif platform == "bluesky":
        return _split_text(full_text, BLUESKY_LIMIT)
    else:
        # Instagram — no splitting
        return [full_text]


def apply_substitutions(text: str, substitutions: dict[str, dict]) -> str:
    """
    Apply name → handle substitutions to text.
    substitutions: { "Sarah Johnson": {"twitter": "@sarahj", "bluesky": "@sarahj.bsky.social", "instagram": "@sarahj"} }
    Returns a dict of platform → substituted text.
    """
    platform_texts = {
        "twitter": text,
        "bluesky": text,
        "instagram": text,
    }
    for name, handles in substitutions.items():
        for platform in platform_texts:
            handle = handles.get(platform)
            if handle:
                platform_texts[platform] = platform_texts[platform].replace(name, handle)
    return platform_texts
