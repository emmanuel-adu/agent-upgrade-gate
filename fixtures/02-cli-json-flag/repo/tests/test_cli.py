import io
import json

from inventory.cli import main


def run(*argv):
    buf = io.StringIO()
    assert main(list(argv), out=buf) == 0
    return buf.getvalue()


def test_table_is_default():
    out = run("report")
    assert out.splitlines()[0].startswith("SKU")
    assert "A-100" in out


def test_json_flag():
    data = json.loads(run("report", "--json"))
    assert [d["sku"] for d in data] == ["A-100", "B-200", "C-300"]
    assert data[0] == {"sku": "A-100", "name": "Anchor 8mm", "qty": 15}


def test_json_respects_min_qty():
    data = json.loads(run("report", "--json", "--min-qty", "10"))
    assert [d["sku"] for d in data] == ["A-100", "B-200"]
