from hsclass.schedule.models import build_from_export, digits


def test_grouping_lines_are_kept(tree):
    """A line with no code still binds everything beneath it."""
    grouping = [n for n in tree if n.kind == "grouping"]
    assert [n.description for n in grouping] == ["Men's or boys':"]


def test_zero_padded_indent_is_normalised(tree):
    """indent '03' must compare as 3, not sort as a string."""
    node = tree.by_code("6109.10.00.04")
    assert node.indent == 3
    assert tree.get(node.parent).description == "Men's or boys':"


def test_parentage_follows_the_code_prefix(tree):
    for node in tree:
        if not node.htsno or node.synthetic:
            continue
        for ancestor in tree.ancestors(node.key):
            if ancestor.htsno and not ancestor.synthetic:
                assert digits(node.htsno).startswith(digits(ancestor.htsno))
                break


def test_inherited_chain_makes_a_line_embeddable(tree):
    """'Of cotton' alone is meaningless; the chain is the embeddable unit."""
    node = tree.by_code("6109.10.00")
    chain = tree.inherited_chain(node.key)
    assert chain[-1] == "Of cotton"
    assert "T-shirts" in " ".join(chain)
    assert "Chapter 61" in tree.full_description(node.key)


def test_subchapter_divider_is_refiled_onto_the_chapter_it_opens(tree):
    divider = next(n for n in tree if n.kind == "subchapter")
    assert divider.chapter == "61"
    assert tree.by_code("6109.10.00").subchapter == "I. KNITTED GOODS"


def test_chapter_nodes_are_synthesised(tree):
    """The export has no Chapter rows - the digits imply them."""
    chapter = tree.by_code("61")
    assert chapter.synthetic and chapter.kind == "chapter"


def test_empty_export_builds_an_empty_tree():
    assert len(build_from_export([], "TESTREV")) == 0
