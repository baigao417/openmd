"""A parser correction must not silently reuse packages from the unsafe parser."""
from openmd.adapters.xiaohongshu import XiaohongshuAdapter
from openmd.core.package_contract import package_metadata_satisfies_extraction_contract


def test_old_xhs_parser_package_is_not_accepted_by_current_adapter():
    item = XiaohongshuAdapter().resolve_items('https://www.xiaohongshu.com/explore/ffff00000000000000000004')[0]
    metadata = {
        'extraction_contract_version': 'xhs-mm-v1',
        'components': [
            {'id': 'native_text', 'status': 'verified', 'required': True},
            {'id': 'image_ocr', 'status': 'not_applicable', 'required': False},
            {'id': 'video_transcript', 'status': 'not_applicable', 'required': False},
        ],
    }
    assert not package_metadata_satisfies_extraction_contract(metadata, item)

