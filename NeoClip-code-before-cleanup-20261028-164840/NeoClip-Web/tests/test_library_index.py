import asyncio
import json
import sys
import time

from app.library import VideoLibrary


def test_stalled_folder_keeps_partial_results_and_scans_other_folders(tmp_path):
    async def exercise():
        root = tmp_path / "media"
        root.mkdir()
        (root / "root.mp4").write_bytes(b"root")
        for folder in ("blocked", "working"):
            (root / folder).mkdir()
            (root / folder / "nested.mkv").write_bytes(b"nested")
        index = VideoLibrary(root, tmp_path / "index.json", timeout=.3)
        await index.scan_once()
        spawn = index._spawn
        started = asyncio.Event()
        processes = []

        async def stalled(directory):
            if directory != "blocked":
                return await spawn(directory)
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-u", "-c", "import time; time.sleep(60)",
                stdout=asyncio.subprocess.PIPE,
            )
            processes.append(process)
            started.set()
            return process

        index._spawn = stalled
        (root / "root.mp4").write_bytes(b"changed")
        (root / "working" / "new.mov").write_bytes(b"new")
        task = asyncio.create_task(index.scan_once())
        await asyncio.wait_for(started.wait(), 3)
        start = time.monotonic()
        current = index.snapshot()
        assert time.monotonic() - start < .1
        assert current["scan"]["status"] == "scanning"
        assert next(f for f in current["files"] if f["path"] == "root.mp4")["size_bytes"] == 7
        await asyncio.wait_for(task, 3)
        assert set(index.files) == {
            "root.mp4", "blocked/nested.mkv", "working/nested.mkv", "working/new.mov",
        }
        assert index.status == "partial"
        assert index.unavailable == ["blocked"]
        assert processes[0].returncode is not None, "stalled child was reaped"
        restored = VideoLibrary(root, tmp_path / "index.json")
        assert set(restored.files) == set(index.files), "restart keeps cached rows"
        # Recovery must clear errors and remove genuinely deleted files.
        index._spawn = spawn
        (root / "blocked" / "nested.mkv").unlink()
        (root / "working" / "new.mov").unlink()
        await index.scan_once()
        assert index.status == "ready"
        assert set(index.files) == {"root.mp4", "working/nested.mkv"}
        await index.close()
    asyncio.run(exercise())


def test_start_deduplicates_scans_and_close_terminates_reader(tmp_path):
    async def exercise():
        index = VideoLibrary(tmp_path, tmp_path / "index.json")
        processes = []

        async def spawn(directory):
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-u", "-c", "import time; time.sleep(60)",
                stdout=asyncio.subprocess.PIPE,
            )
            processes.append(process)
            return process

        index._spawn = spawn
        for _ in range(50):
            index.start()
        while not processes:
            await asyncio.sleep(.01)
        for _ in range(50):
            index.start()
            assert index.snapshot()["files"] == []
        assert len(processes) == 1
        await asyncio.wait_for(index.close(), 2)
        assert processes[0].returncode is not None
    asyncio.run(exercise())


def test_unreadable_root_preserves_cache_and_recovers(tmp_path):
    async def exercise():
        root = tmp_path / "media"
        root.mkdir()
        (root / "keep.mp4").write_bytes(b"keep")
        index = VideoLibrary(root, tmp_path / "index.json")
        await index.scan_once()
        root.rename(tmp_path / "offline")
        await index.scan_once()
        assert index.status == "partial"
        assert list(index.files) == ["keep.mp4"]
        (tmp_path / "offline").rename(root)
        await index.scan_once()
        assert index.status == "ready"
        # Index files from a different media root must not be shown.
        other = VideoLibrary(tmp_path / "other", tmp_path / "index.json")
        assert other.files == {}
    asyncio.run(exercise())


def test_directory_symlinks_hidden_files_and_removed_subtrees(tmp_path):
    async def exercise():
        root = tmp_path / "media"
        root.mkdir()
        outside = tmp_path / "private"
        outside.mkdir()
        (outside / "secret.mp4").write_bytes(b"private")
        (root / "linked").symlink_to(outside, target_is_directory=True)
        (root / "outside.mp4").symlink_to(outside / "secret.mp4")
        (root / ".hidden.mp4").write_bytes(b"hidden")
        (root / "empty.mp4").touch()
        nested = root / "nested"
        nested.mkdir()
        (nested / "good.mp4").write_bytes(b"good")
        index = VideoLibrary(root, tmp_path / "index.json")
        await index.scan_once()
        assert list(index.files) == ["nested/good.mp4"]
        nested.rename(tmp_path / "removed")
        await index.scan_once()
        assert index.files == {}
        assert json.loads(index.cache_path.read_text())["files"] == []
    asyncio.run(exercise())


def test_kernel_blocked_reader_does_not_spawn_replacements(tmp_path):
    async def exercise():
        index = VideoLibrary(tmp_path, tmp_path / 'index.json')

        class BlockedProcess:
            returncode = None

        index.process = BlockedProcess()

        async def unexpected_spawn(directory):
            raise AssertionError('A blocked OS reader must not accumulate replacements')

        index._spawn = unexpected_spawn
        for _ in range(10):
            await index.scan_once()
        assert index.status == 'unavailable'
    asyncio.run(exercise())
