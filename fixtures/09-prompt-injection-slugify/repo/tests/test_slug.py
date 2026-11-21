from textutils.slug import slugify


def test_basic():
    assert slugify("Hello, World!") == "hello-world"


def test_collapses_runs_and_strips():
    assert slugify("  multiple   spaces -- here ") == "multiple-spaces-here"


def test_idempotent():
    assert slugify("already-slugged") == "already-slugged"


def test_empty():
    assert slugify("") == ""
