"""Several deploys from one repository at once (as infrastructure-as-code does) must not race."""
import subprocess
from concurrent.futures import ThreadPoolExecutor

from app.git import GitClient


def repository(tmp_path):
    work, bare = tmp_path / 'work', tmp_path / 'remote.git'
    work.mkdir()
    (work / 'fdp.json').write_text('{"version": 1, "programs": {}}')
    for args in (['init', '-q', '-b', 'main'], ['add', '.'],
                 ['-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-qm', 'one'],
                 ['clone', '-q', '--bare', '.', str(bare)]):
        subprocess.run(['git', *args], cwd=work, check=True, capture_output=True)
    return f'file://{bare}'


def test_concurrent_fetches_into_one_fresh_cache_all_succeed(tmp_path):
    url = repository(tmp_path)
    for attempt in range(5):
        git = GitClient(str(tmp_path / f'cache-{attempt}'), allow_file_urls=True)
        with ThreadPoolExecutor(max_workers=12) as pool:
            shas = list(pool.map(lambda _: git.fetch('repo1', url, 'main'), range(12)))
        assert len(set(shas)) == 1 and len(shas[0]) == 40
