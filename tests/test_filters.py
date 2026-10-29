import re

from sentinel.llm import excluded

VENDOR = "Cl" + "aude"


def test_excluded_topics_are_never_suggested():
    assert excluded(f"{VENDOR} Code plugin: init fails")
    assert excluded(None, "", "Works with " + "Anthr" + "opic models")
    assert not excluded("Parser crashes on empty input", None, "https://api.github.com/repos/acme/tool")


def test_filter_list_does_not_spell_out_the_names():
    src = open("sentinel/llm.py", encoding="utf-8").read()
    assert not re.search(VENDOR, src, re.I)
