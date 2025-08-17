from pathlib import Path

from babelarr.queue_db import QueueRepository


def test_count_returns_number_of_items(tmp_path):
    db_path = tmp_path / "queue.db"
    with QueueRepository(str(db_path)) as repo:
        assert repo.count() == 0
        repo.add(Path("a"), "nl")
        repo.add(Path("b"), "nl")
        assert repo.count() == 2
        repo.remove(Path("a"), "nl")
        assert repo.count() == 1


def test_add_stores_priority(tmp_path):
    db_path = tmp_path / "queue.db"
    with QueueRepository(str(db_path)) as repo:
        repo.add(Path("a"), "nl", priority=5)
        repo.add(Path("b"), "nl")
        rows = sorted(repo.all(), key=lambda r: r[0])
        assert rows == [(Path("a"), "nl", 5), (Path("b"), "nl", 0)]
