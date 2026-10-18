import pytest

from hsclass.schedule.models import build_from_export

#: A miniature chapter 61 carrying every shape the real export throws at the
#: parser: a heading, a grouping line with no code, an 8-digit duty line, two
#: 10-digit statistical suffixes, a zero-padded indent, and a subchapter
#: divider emitted before its chapter opens.
EXPORT_ROWS = [
    {"htsno": "", "indent": "0", "superior": "true", "description": "I. KNITTED GOODS"},
    {"htsno": "6109", "indent": "0", "description":
        "T-shirts, singlets, tank tops and similar garments, knitted or crocheted:"},
    {"htsno": "6109.10.00", "indent": "1", "description": "Of cotton",
     "general": "16.5%", "special": "Free (AU)", "other": "90%"},
    {"htsno": "", "indent": "2", "superior": "true", "description": "Men's or boys':"},
    {"htsno": "6109.10.00.04", "indent": "03", "description":
        "T-shirts, all white, short hemmed sleeves"},
    {"htsno": "6109.10.00.12", "indent": "3", "description": "Other"},
    {"htsno": "6109.90", "indent": "1", "description": "Of other textile materials:"},
    {"htsno": "6109.90.10.00", "indent": "2", "description": "Of man-made fibres"},
]


@pytest.fixture
def tree():
    return build_from_export(EXPORT_ROWS, "TESTREV")
