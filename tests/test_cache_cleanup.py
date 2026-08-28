from maxread.cache_cleanup import cleanup_completed_cache, cleanup_source_cache, local_date_cutoff_utc
from maxread.db import Store


def test_cleanup_source_cache_keeps_pipeline_artifacts(tmp_path):
    workdir = tmp_path / "work"
    paper = workdir / "papers" / "2608.00001"
    artifacts = paper / "pipeline_artifacts"
    figures = paper / "rendered_figures"
    artifacts.mkdir(parents=True)
    figures.mkdir()
    (paper / "2608.00001.pdf").write_bytes(b"pdf")
    (paper / "2608.00001.source").write_bytes(b"source")
    (figures / "figure.png").write_bytes(b"image")
    (artifacts / "07-quality.json").write_text("{}", encoding="utf-8")

    result = cleanup_source_cache(workdir, "paper", "2608.00001")

    assert result.files_removed == 3
    assert result.bytes_removed == len(b"pdfsourceimage")
    assert (artifacts / "07-quality.json").exists()
    assert not (paper / "2608.00001.pdf").exists()
    assert not figures.exists()


def test_cleanup_completed_cache_includes_legacy_records(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    store.upsert_paper("2608.00001", "done")
    store.conn.execute("update papers set updated_at='2026-08-20 00:00:00' where paper_id='2608.00001'")
    store.conn.commit()
    paper = tmp_path / "work" / "papers" / "2608.00001"
    paper.mkdir(parents=True)
    (paper / "paper.pdf").write_bytes(b"pdf")
    store.mark_cache_legacy_before("2026-08-21 00:00:00")

    result = cleanup_completed_cache(store, tmp_path / "work", 0)

    assert result["files_removed"] == 1
    assert store.get_paper("2608.00001").status == "legacy"


def test_mark_cache_legacy_before_preserves_newer_done_records(tmp_path):
    store = Store(tmp_path / "maxread.sqlite3")
    store.upsert_paper("old", "done")
    store.upsert_paper("new", "done")
    store.upsert_document("article-old", "cache_expired")
    store.conn.execute("update papers set updated_at='2026-08-20 00:00:00' where paper_id='old'")
    store.conn.execute("update papers set updated_at='2026-08-28 00:00:00' where paper_id='new'")
    store.conn.commit()

    result = store.mark_cache_legacy_before("2026-08-28 00:00:00")

    assert result == {"papers": 1, "documents": 1}
    assert store.get_paper("old").status == "legacy"
    assert store.get_paper("new").status == "done"
    assert store.get_document("article-old").status == "legacy"
    assert local_date_cutoff_utc("2026-08-28") == "2026-08-27 16:00:00"
