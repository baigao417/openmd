from openmd.cli_items import batch_status


def test_batch_status_distinguishes_all_success_partial_and_all_failure():
    assert batch_status(item_count=3, success_count=3, failed_count=0) == "verified"
    assert batch_status(item_count=3, success_count=3, failed_count=0, partial_count=1) == "partial"
    assert batch_status(item_count=3, success_count=2, failed_count=1) == "partial"
    assert batch_status(item_count=3, success_count=0, failed_count=3) == "failed"
    assert batch_status(item_count=0, success_count=0, failed_count=0) == "failed"
