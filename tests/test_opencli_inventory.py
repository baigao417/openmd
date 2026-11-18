import copy
from pathlib import Path

import pytest


def test_inventory_covers_dynamic_commands_options_and_owned_columns():
    from openmd.runtime.opencli_inventory import inventory
    rows = {(r['site'], r['command']): r for r in inventory()['commands']}
    assert {('chatgpt', 'detail'), ('deepseek', 'detail'), ('grok', 'detail')} <= rows.keys()
    assert '--markdown' in rows['grok', 'detail']['args']
    assert '--site-session' in rows['twitter', 'thread']['args']
    assert '--selector_stdin' in rows['openmd-xiaohongshu', 'note-assets']['args']
    assert 'content' in rows['xiaoe', 'content']['columns']
    assert ('claude', 'detail') not in rows  # declared plans are not live calls


@pytest.fixture
def contract_package(tmp_path):
    root = tmp_path / 'package'
    target = root / 'clis/example/detail.js'
    target.parent.mkdir(parents=True)
    target.write_text("""
// cli({site:'wrong', name:'wrong', columns:['wrong']})
cli({site:'example', name:'detail',
 args:[{name:'id', positional:true}, {name:'markdown', type:'boolean'}],
 columns:['Role', 'Text'], func: async () => []});
""", encoding='utf-8')
    return root, target


def sample_inventory():
    return {'commands': [{'site': 'example', 'command': 'detail', 'kind': 'upstream',
                          'args': ['--markdown'], 'positionals': 1, 'columns': ['Role', 'Text']}]}


def test_contract_resolves_managed_package_without_execution(contract_package, monkeypatch):
    from openmd.runtime import managed_tools
    from openmd.runtime.opencli_inventory import check_contracts
    root, _ = contract_package
    monkeypatch.setattr(managed_tools, 'managed_package_root', lambda: root)
    assert check_contracts(sample_inventory()) == []


def test_contract_rejects_changed_column(contract_package):
    from openmd.runtime.opencli_inventory import check_contracts
    root, target = contract_package
    target.write_text(target.read_text(encoding='utf-8').replace("'Text'", "'Body'"), encoding='utf-8')
    failures = check_contracts(sample_inventory(), package_root=root)
    assert any('column_missing:Text' in f for f in failures)


def test_contract_rejects_missing_arg_and_command(contract_package):
    from openmd.runtime.opencli_inventory import check_contracts
    root, _ = contract_package
    data = copy.deepcopy(sample_inventory())
    data['commands'][0]['args'].append('--unknown')
    assert any('arg_missing:--unknown' in f for f in check_contracts(data, package_root=root))
    data['commands'][0]['command'] = 'absent'
    assert any('command_missing' in f for f in check_contracts(data, package_root=root))


def test_inventory_is_in_declaration_drift_check():
    from openmd.routing.generate import projections
    root = Path(__file__).resolve().parents[1]
    paths = {p.relative_to(root).as_posix() for p in projections(root)}
    assert {'docs/opencli-inventory.generated.md', 'docs/opencli-inventory.generated.json'} <= paths
