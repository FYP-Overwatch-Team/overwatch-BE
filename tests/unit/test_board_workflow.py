from app.boards.workflow import first_step, native_targets, reachable_in_workflow, tracker_targets
from app.connectors.jira import _workflow

COLUMNS = [
    {"id": "todo", "name": "To do", "status_category": "todo", "status_ids": [], "next": ["doing"]},
    {"id": "doing", "name": "In progress", "status_category": "in_progress", "status_ids": [], "next": ["todo", "review"]},
    {"id": "review", "name": "Review", "status_category": "in_progress", "status_ids": [], "next": None},
    {"id": "done", "name": "Done", "status_category": "done", "status_ids": [], "next": []},
]
NATIVE = {"column_mode": "default", "columns": COLUMNS}


def test_a_column_allows_its_next_columns_or_any_when_it_names_none():
    assert native_targets(NATIVE, "todo") == ["doing"]
    assert native_targets(NATIVE, "review") == ["todo", "doing", "done"]
    assert native_targets(NATIVE, "done") == []


def test_the_first_step_is_on_the_shortest_way_there():
    assert first_step(NATIVE, "todo", "done") == "doing"
    assert first_step(NATIVE, "todo", "doing") == "doing"
    assert first_step(NATIVE, "done", "todo") is None


def test_jira_workflows_become_moves_from_statuses_or_from_any():
    statuses = {"r1": {"id": "1", "name": "To Do"}, "r2": {"id": "2", "name": "In Progress"}, "r3": {"id": "3", "name": "In Review"}}
    raw = {"name": "Dev", "transitions": [
        {"id": "1", "name": "Create", "type": "INITIAL", "toStatusReference": "r1"},
        {"id": "11", "name": "Start", "type": "DIRECTED", "toStatusReference": "r2", "links": [{"fromStatusReference": "r1"}]},
        {"id": "21", "name": "Ask for review", "type": "DIRECTED", "toStatusReference": "r3", "links": [{"fromStatusReference": "r2"}]},
        {"id": "31", "name": "Reopen", "type": "GLOBAL", "toStatusReference": "r1"},
    ]}

    assert _workflow(raw, statuses)["transitions"] == [
        {"id": "11", "name": "Start", "from": ["1"], "to": "2"},
        {"id": "21", "name": "Ask for review", "from": ["2"], "to": "3"},
        {"id": "31", "name": "Reopen", "from": None, "to": "1"},
    ]


def test_a_tracker_column_is_reachable_through_a_status_it_holds():
    board = {
        "column_mode": "tracker",
        "columns": [
            {"id": "c-todo", "status_ids": ["1"], "status_category": "todo"},
            {"id": "c-doing", "status_ids": ["2", "4"], "status_category": "in_progress"},
            {"id": "c-review", "status_ids": ["3"], "status_category": "in_progress"},
        ],
        "tracker_workflow": {
            "statuses": {"1": {"name": "To Do", "category": "todo"}, "2": {"name": "In Progress", "category": "in_progress"}, "3": {"name": "In Review", "category": "in_progress"}},
            "workflows": {"w": {"transitions": [
                {"id": "11", "name": "Start", "from": ["1"], "to": "2"},
                {"id": "21", "name": "Review", "from": ["2"], "to": "3"},
                {"id": "31", "name": "Reopen", "from": None, "to": "1"},
            ]}},
            "by_issue_type": {"10001": "w"},
        },
    }
    item = {"status_id": "1", "issue_type_id": "10001", "column_id": "c-todo"}

    reachable = reachable_in_workflow(board, item)

    assert [(m["status_id"], m["transition_name"]) for m in reachable] == [("2", "Start")]
    assert tracker_targets(board, item, reachable) == [{"column_id": "c-doing", "status_id": "2", "status": "In Progress", "transition_name": "Start"}]
    assert reachable_in_workflow(board, {**item, "issue_type_id": "other"}) is None
