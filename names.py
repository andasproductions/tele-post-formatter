import spacy

_nlp = None

def get_nlp():
    global _nlp
    if _nlp is None:
        _nlp = spacy.load("en_core_web_md")
    return _nlp


def reflow_paragraphs(text: str, target_words: int = 25) -> str:
    """Reflow a block of text into paragraphs by accumulating sentences until target word count."""
    nlp = get_nlp()
    doc = nlp(text)
    sentences = [sent.text.strip() for sent in doc.sents if sent.text.strip()]

    paragraphs = []
    current: list[str] = []
    current_words = 0

    for sent in sentences:
        sent_words = len(sent.split())
        # If we already have enough content and this sentence alone would push
        # us well past the target, break before adding it.
        if current and current_words >= target_words // 2 and current_words + sent_words > target_words:
            paragraphs.append(" ".join(current))
            current = []
            current_words = 0
        current.append(sent)
        current_words += sent_words
        if current_words >= target_words:
            paragraphs.append(" ".join(current))
            current = []
            current_words = 0

    if current:
        paragraphs.append(" ".join(current))

    return "\n\n".join(paragraphs)


def extract_names(text: str, ignored_names: list[str]) -> list[str]:
    """Extract unique PERSON entity names from text, excluding ignored names."""
    nlp = get_nlp()
    doc = nlp(text)

    ignored_lower = {n.lower() for n in ignored_names}
    seen = set()
    names = []

    for ent in doc.ents:
        if ent.label_ == "PERSON":
            name = ent.text.strip()
            key = name.lower()
            if key not in ignored_lower and key not in seen:
                seen.add(key)
                names.append(name)

    return names
