"""Deterministic unit tests for task_server tools (isolated temp file, no LLM)."""
import pytest

import task_server as ts


@pytest.fixture(autouse=True)
def isolated_tasks_file(monkeypatch, tmp_path):
    path = tmp_path / "tasks.txt"
    monkeypatch.setattr(ts, "TASKS_FILE", str(path))
    return path


def test_list_tasks_when_file_missing():
    assert ts.list_tasks() == []


def test_add_then_list_preserves_order(isolated_tasks_file):
    assert "added successfully" in ts.add_task("Buy groceries")
    ts.add_task("Call mom")
    assert ts.list_tasks() == ["Buy groceries", "Call mom"]
    assert isolated_tasks_file.read_text(encoding="utf-8") == "Buy groceries\nCall mom\n"


def test_list_skips_blank_lines(isolated_tasks_file):
    isolated_tasks_file.write_text("A\n\n   \nB\n", encoding="utf-8")
    assert ts.list_tasks() == ["A", "B"]


def test_unicode_task_round_trip():
    ts.add_task("Visit São Paulo ☀")
    assert ts.list_tasks() == ["Visit São Paulo ☀"]


def test_plan_trip_prompt_mentions_inputs():
    text = ts.plan_trip_prompt("Rome", 3)
    assert "Rome" in text and "3-day" in text



def test_plan_trip_for_future_date_prompt():
    text = ts.plan_trip_for_future_date("Rome", "2026-03-03", "2026-03-05")
    assert "Rome" in text and 'from_date="2026-03-03"' in text and 'to_date="2026-03-05"' in text
    assert "3 day(s)" in text and "Tuesday" in text and "unavailable_dates" in text


@pytest.mark.parametrize("start, end, expected", [
    ("2026-03-05", "2026-03-03", "on or after"),
    ("next friday", "2026-03-03", "YYYY-MM-DD"),
    ("2026-03-01", "2026-03-20", "maximum is 15"),
])
def test_plan_trip_for_future_date_rejects_bad_dates(start, end, expected):
    with pytest.raises(ValueError, match=expected):
        ts.plan_trip_for_future_date("Rome", start, end)


def test_clear_tasks_empties_list(isolated_tasks_file):
    ts.add_task("A")
    ts.add_task("B")
    assert "2 task(s) removed" in ts.clear_tasks()
    assert ts.list_tasks() == []
    assert isolated_tasks_file.read_text(encoding="utf-8") == ""


def test_clear_tasks_when_file_missing():
    assert "0 task(s) removed" in ts.clear_tasks()
    assert ts.list_tasks() == []


def test_update_task_by_number_is_one_based(isolated_tasks_file):
    for t in ("A", "B", "C"):
        ts.add_task(t)
    assert "Task 2 was updated" in ts.update_task("2", "B2")
    assert ts.list_tasks() == ["A", "B2", "C"]
    assert isolated_tasks_file.read_text(encoding="utf-8") == "A\nB2\nC\n"


def test_update_task_by_text_case_insensitive():
    ts.add_task("Buy groceries")
    ts.add_task("Call mom")
    assert "updated" in ts.update_task("  call MOM ", "Call mom at 6pm")
    assert ts.list_tasks() == ["Buy groceries", "Call mom at 6pm"]


@pytest.mark.parametrize("target, expected", [("0", "no task number 0"), ("3", "no task number 3"),
                                              ("Walk dog", "no task matches"), ("Dup", "several tasks")])
def test_update_task_bad_target_leaves_list_unchanged(target, expected):
    for t in ("Dup", "Dup"):
        ts.add_task(t)
    result = ts.update_task(target, "X")
    assert result.startswith("Error") and expected in result and "1. Dup" in result
    assert ts.list_tasks() == ["Dup", "Dup"]


def test_update_task_rejects_empty_and_flattens_newlines():
    ts.add_task("A")
    assert ts.update_task("1", "   ").startswith("Error")
    ts.update_task("1", "line one\nline two")
    assert ts.list_tasks() == ["line one line two"]


def test_update_task_on_empty_list():
    assert "empty" in ts.update_task("1", "X")
