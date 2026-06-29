"""extract_speakable — chain-of-thought must never reach the voice path."""

from jarvis.brain import extract_speakable

# The exact failure mode from yennefer-daemon.log on 2026-06-10: content empty,
# the whole token budget burned inside reasoning_content, no closing tag.
LEAKED_REASONING = (
    "Thinking Process: 1. **Analyze the Request:** * **Persona:** Yennefer of "
    "Vengerberg (from The Witcher). * **Traits:** Confident, sharp, intelligent..."
)


def test_plain_content_passes_through():
    assert extract_speakable({"content": "Time slips, as ever."}) == "Time slips, as ever."


def test_think_tags_in_content_are_stripped():
    msg = {"content": "<think>plan the line</think>Time slips, as ever."}
    assert extract_speakable(msg) == "Time slips, as ever."


def test_tagless_reasoning_is_never_spoken():
    msg = {"content": "", "reasoning_content": LEAKED_REASONING}
    assert extract_speakable(msg) == ""


def test_reasoning_with_closing_tag_yields_only_the_answer():
    msg = {
        "content": "",
        "reasoning_content": "I should be dry and brief.</think>Time slips, as ever.",
    }
    assert extract_speakable(msg) == "Time slips, as ever."


def test_reasoning_ignored_when_content_present():
    msg = {"content": "Time slips, as ever.", "reasoning_content": LEAKED_REASONING}
    assert extract_speakable(msg) == "Time slips, as ever."


def test_missing_fields_yield_silence():
    assert extract_speakable({}) == ""
    assert extract_speakable({"content": None, "reasoning_content": None}) == ""


def test_unclosed_think_in_content_yields_silence():
    # Model cut off mid-thought inside content itself.
    assert extract_speakable({"content": "<think>half a thought"}) == ""


def test_tagless_reasoning_in_content_is_silenced():
    msg = {
        "content": (
            "The user is asking about the stakes of the matches scheduled for today. "
            "I need to synthesize the information from the search results."
        )
    }

    assert extract_speakable(msg) == ""


def test_tagless_reasoning_with_final_answer_marker_yields_answer():
    msg = {
        "content": (
            "The user is asking about fixtures.\n"
            "Final answer: England plays Panama today."
        )
    }

    assert extract_speakable(msg) == "England plays Panama today."
