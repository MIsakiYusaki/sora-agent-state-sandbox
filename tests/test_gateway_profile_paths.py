from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
from unittest.mock import patch

import pytest

from sora_sandbox.conversation_gateway.state import ProfileStateRepository, _windows_extended_path
from sora_sandbox.state import seed_state


@pytest.mark.parametrize(('source', 'expected'), [
    ('C:/Sora/data/../profiles', '\\\\?\\C:\\Sora\\profiles'),
    ('\\\\server\\share\\profiles', '\\\\?\\UNC\\server\\share\\profiles'),
    ('\\\\?\\C:\\Sora\\profiles', '\\\\?\\C:\\Sora\\profiles'),
    ('\\\\?\\UNC\\server\\share\\profiles', '\\\\?\\UNC\\server\\share\\profiles'),
])
def test_windows_extended_path_preserves_location(source, expected):
    assert _windows_extended_path(source) == expected


@pytest.mark.skipif(os.name != 'nt', reason='Windows filesystem regression')
def test_profile_roundtrip_and_atomic_failure_beyond_max_path():
    with tempfile.TemporaryDirectory(prefix='sora-profile-path-') as temporary:
        # Each component is valid; only the total filename length exceeds MAX_PATH.
        root = Path(temporary) / ('nested-' * 20) / 'gateway_profiles'
        repository = ProfileStateRepository(root, profile_scope='acceptance')
        try:
            path = repository._path('acceptance')
            assert len(str(path)) > 260
            state = seed_state()
            state['session']['user_id'] = 'synthetic-path-test'
            repository.save('acceptance', state)
            reopened = ProfileStateRepository(root, profile_scope='acceptance')
            assert reopened.load('acceptance')['session']['user_id'] == 'synthetic-path-test'
            with patch('sora_sandbox.conversation_gateway.state.os.replace', side_effect=OSError('injected replace failure')):
                state['session']['user_id'] = 'must-not-commit'
                with pytest.raises(OSError, match='injected replace failure'):
                    repository.save('acceptance', state)
            assert reopened.load('acceptance')['session']['user_id'] == 'synthetic-path-test'
            assert not list(repository.root.glob('*.tmp'))
            with pytest.raises(PermissionError):
                repository.load('foreign-profile')
        finally:
            # Only this test-created directory; extended prefix is needed on Windows.
            cleanup = root if str(root).startswith('\\\\?\\') else Path('\\\\?\\' + str(root.absolute()))
            shutil.rmtree(cleanup)


def test_existing_profile_filename_is_preserved(tmp_path):
    repository = ProfileStateRepository(tmp_path, profile_scope='existing')
    repository.save('existing', seed_state())
    before = list(tmp_path.glob('profile-*.json'))
    assert len(before) == 1
    reopened = ProfileStateRepository(tmp_path, profile_scope='existing')
    reopened.load('existing')
    assert [p.name for p in tmp_path.glob('profile-*.json')] == [p.name for p in before]
