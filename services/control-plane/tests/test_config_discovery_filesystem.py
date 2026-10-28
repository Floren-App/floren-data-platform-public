"""Exercise real filesystem discovery, including ext4's inaccessible recovery directory."""
import os
import shutil
import subprocess

import pytest

from app.orchestrator.base import list_files_command, parse_list


@pytest.fixture
def gnu_find():
    for executable in ('find', 'gfind'):
        binary = shutil.which(executable)
        if binary and subprocess.run([binary, '--version'], capture_output=True).returncode == 0:
            return binary
    pytest.skip('GNU find is required, as in the workspace runtime')


def test_discovery_retains_root_and_repository_configs_with_protected_recovery_dir(tmp_path, gnu_find):
    (tmp_path / 'fdp.json').write_text('{}')
    (tmp_path / 'repo').mkdir()
    (tmp_path / 'repo/fdp.json').write_text('{"version":1}')
    (tmp_path / '.fdp').mkdir()
    (tmp_path / '.fdp/fdp.json').write_text('{}')
    recovery = tmp_path / 'lost+found'
    recovery.mkdir()
    (recovery / 'fdp.json').write_text('{}')
    recovery.chmod(0)
    try:
        command = list_files_command(str(tmp_path), name='fdp.json', maxdepth=2)
        command[0] = gnu_find
        result = subprocess.run(command, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert [path for path, _ in parse_list(result.returncode, result.stdout)] == ['fdp.json', 'repo/fdp.json']
        assert os.stat(recovery).st_mode & 0o777 == 0
    finally:
        recovery.chmod(0o700)


def test_output_listing_still_reads_hidden_application_directories(tmp_path, gnu_find):
    output = tmp_path / '.fdp/runs/example/outputs'
    output.mkdir(parents=True)
    (output / 'report.csv').write_text('id\n1\n')
    command = list_files_command(str(output))
    command[0] = gnu_find
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert parse_list(result.returncode, result.stdout) == [('report.csv', 5)]
