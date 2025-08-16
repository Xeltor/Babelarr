from pathlib import Path

from babelarr.queue_db import QueueRepository


def test_count_returns_number_of_items(tmp_path):
    db_path = tmp_path / "queue.db"
    with QueueRepository(str(db_path)) as repo:
        assert repo.count() == 0
        repo.add(Path("a"))
        repo.add(Path("b"))
        assert repo.count() == 2
        repo.remove(Path("a"))
        assert repo.count() == 1
