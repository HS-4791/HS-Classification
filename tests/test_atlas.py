"""The ATLAS loader, which has to read labels out of free text."""

from __future__ import annotations

import json

from hsclass.data.atlas import Example, as_dataset, load_split, parse_messages

ANSWER = (
    "HTS US Code -> 7307.19.9060\n"
    "Reasoning -> The product is classified under subheading 7307.19.9060, which "
    "pertains to tube or pipe fittings of iron or steel."
)


def exchange(question: str = "Cast carbon steel pipe fittings", answer: str = ANSWER) -> dict:
    return {"messages": [
        {"role": "user", "content": question},
        {"role": "assistant", "content": answer},
    ]}


def test_the_code_is_extracted_from_the_assistant_turn():
    example = parse_messages(exchange()["messages"])
    assert example.code == "7307199060"  # digits only
    assert example.description == "Cast carbon steel pipe fittings"
    assert example.reasoning.startswith("The product is classified")
    assert example.is_full_length


def test_a_row_with_no_code_is_dropped_not_guessed():
    assert parse_messages(exchange(answer="I am not sure what this is.")["messages"]) is None


def test_a_truncated_code_is_dropped():
    assert parse_messages(exchange(answer="HTS US Code -> 73")["messages"]) is None


def test_a_half_exchange_is_dropped():
    assert parse_messages([{"role": "user", "content": "A thing"}]) is None


def test_reasoning_is_optional():
    example = parse_messages(exchange(answer="HTS US Code -> 6109.10.0012")["messages"])
    assert example.code == "6109100012"
    assert example.reasoning == ""


def test_load_split_counts_what_it_dropped(tmp_path):
    path = tmp_path / "test.jsonl"
    path.write_text(
        "\n".join([
            json.dumps(exchange()),
            json.dumps(exchange(answer="no code here")),
            "",
            json.dumps(exchange(question="Knitted cotton t-shirt",
                                answer="HTS US Code -> 6109.10.0012")),
        ]),
        encoding="utf-8",
    )
    examples, dropped = load_split(path)
    assert len(examples) == 2 and dropped == 1


def test_as_dataset_gives_the_pairs_the_eval_runner_takes():
    pairs = as_dataset([Example("a t-shirt", "6109100012", "because knitted")])
    assert pairs == [("a t-shirt", "6109100012")]


def test_the_question_template_is_stripped_to_the_goods():
    """Every row is phrased "What is the HTS US Code for X?".

    That framing is how the fine-tuning set was built, not what a broker
    sends. Left in, it is eight tokens of identical boilerplate on every
    query, and it costs 4.5 points of top-1 at six digits.
    """
    asked = "What is the HTS US Code for cast carbon steel pipe fittings?"
    example = parse_messages(exchange(question=asked)["messages"])
    assert example.description == "cast carbon steel pipe fittings"


def test_a_description_that_is_not_a_question_is_left_alone():
    example = parse_messages(exchange(question="Knitted cotton t-shirt, 60% cotton")["messages"])
    assert example.description == "Knitted cotton t-shirt, 60% cotton"
