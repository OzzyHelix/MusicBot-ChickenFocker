import asyncio
import json
import logging
import os
import pathlib
import shutil
import time

from typing import TYPE_CHECKING
from .utils import format_size_from_bytes

if TYPE_CHECKING:
    from .bot import MusicBot
    from .entry import BasePlaylistEntry

log = logging.getLogger(__name__)


class AudioFileCache:
    """
    This class provides methods to manage the audio file cache and get info about it.
    """

    def __init__(self, bot: "MusicBot") -> None:
        self.bot = bot
        self.config = bot.config
        self.cache_path = pathlib.Path(bot.config.audio_cache_path)

        self.size_bytes = 0
        self.file_count = 0

        # Stores filenames without extension associated to a playlist URL.
        self.auto_playlist_cachemap = {}
        self.cachemap_file_lock = asyncio.Lock()

    def cache_dir_exists(self) -> bool:
        """Wrapper for self.cache.is_dir() for external use."""
        return self.cache_path.is_dir()

    def get_cache_size(self) -> tuple[int, int]:
        """
        Returns AudioFileCache size as a two member tuple containing size_bytes and file_count.
        """
        return (self.size_bytes, self.file_count)

    def scan_audio_cache(self) -> tuple[int, int]:
        """
        Scan the audio cache directory and return a tuple with info.
        Returns (size_in_bytes:int, number_of_files:int)
        """
        cached_bytes = 0
        cached_files = 0
        if os.path.isdir(self.cache_path):
            for cache_file in pathlib.Path(self.cache_path).iterdir():
                cached_files += 1
                cached_bytes += os.path.getsize(cache_file)
        self.size_bytes = cached_bytes
        self.file_count = cached_files

        return self.get_cache_size()

    def _delete_cache_file(self, path: pathlib.Path) -> bool:
        """
        Wrapper for pathlib unlink(missing_ok=True) while logging exceptions.
        """
        try:
            path.unlink(missing_ok=True)
            return True
        except Exception:
            log.exception(f"Failed to delete cache file:  {path}")
            return False

    def _delete_cache_dir(self) -> bool:
        """
        Attempts immediate removal of the cache file directory while logging errors.
        """
        try:
            shutil.rmtree(self.cache_path)
            self.cached_audio_bytes = 0
            log.debug("Audio cache directory has been removed.")
            return True
        except Exception:
            new_name = self.cache_path.stem + "__"
            try:
                new_path = self.cache_path.rename(self.cache_path.with_stem(new_name))
            except Exception:
                log.debug("Audio cache directory could not be removed or renamed.")
                return False
            try:
                shutil.rmtree(new_path)
            except Exception:
                new_path.rename(self.cache_path)
                log.debug("Audio cache directory could not be removed.")
                return False

    def _process_cache_delete(self) -> bool:
        """
        Sorts cache by access or creation time and deletes any that are older than set limits.
        Will retain cached autoplaylist if enabled and files are in the cachemap.
        """
        if self.config.storage_limit_bytes == 0 and self.config.storage_limit_days == 0:
            log.debug("Audio cache has no limits set, nothing to delete.")
            return False

        if os.name == "nt":
            # On Windows, creation time (ctime) is the only reliable way to do this.
            # mtime is usually older than download time. atime is changed on multiple files by some part of the player.
            # To make this consistent everywhere, we need to store last-played times for songs on our own.
            cached_files = sorted(
                pathlib.Path(self.cache_path).iterdir(),
                key=os.path.getctime,
                reverse=True,
            )
        else:
            cached_files = sorted(
                pathlib.Path(self.cache_path).iterdir(),
                key=os.path.getatime,
                reverse=True,
            )

        max_age = time.time() - (86400 * self.config.storage_limit_days)
        cached_size = 0
        removed_count = 0
        removed_size = 0
        retained_count = 0
        retained_size = 0
        # Accumulate file sizes until a set limit is reached and purge remaining files.
        for cache_file in cached_files:
            file_size = os.path.getsize(cache_file)

            # Do not purge files from autoplaylist if retention is enabled.
            if self._check_autoplay_cachemap(cache_file):
                retained_count += 1
                retained_size += file_size
                cached_size += file_size
                continue

            # get file access/creation time.
            if os.name == "nt":
                file_time = os.path.getctime(cache_file)
            else:
                file_time = os.path.getatime(cache_file)

            # enforce size limit before time limit.
            if (
                self.config.storage_limit_bytes
                and self.config.storage_limit_bytes < cached_size
            ):
                self._delete_cache_file(cache_file)
                removed_count += 1
                removed_size += file_size
                continue

            if self.config.storage_limit_days:
                if file_time < max_age:
                    self._delete_cache_file(cache_file)
                    removed_count += 1
                    removed_size += file_size
                    continue

            cached_size += file_size

        if removed_count:
            log.debug(
                "Audio cache deleted {} file{}, total of {} removed.".format(
                    removed_count,
                    "" if removed_count == 1 else "s",
                    format_size_from_bytes(removed_size),
                )
            )
        if retained_count:
            log.debug(
                "Audio cached retained {} file{} from autoplaylist, total of {} retained.".format(
                    retained_count,
                    "" if retained_count == 1 else "s",
                    format_size_from_bytes(retained_size),
                )
            )
        self.file_count = len(cached_files) - removed_count
        self.size_bytes = cached_size
        log.debug(
            "Audio cache is now {} over {} file{}.".format(
                format_size_from_bytes(self.size_bytes),
                self.file_count,
                "" if self.file_count == 1 else "s",
            )
        )
        return True

    def delete_old_audiocache(self, remove_dir: bool = False) -> bool:
        """
        Handle deletion of cache data according to settings and return bool status.
        Will return False if no cache directory exists, and error prevented deletion.
        Param `remove_dir` is intened only to be used in bot-startup.
        """

        if not os.path.isdir(self.cache_path):
            log.debug("Audio cache directory is missing, nothing to delete.")
            return False

        if self.config.save_videos:
            return self._process_cache_delete()
        elif remove_dir:
            return self._delete_cache_dir()

        return True

    def handle_new_cache_entry(self, entry: "BasePlaylistEntry") -> None:
        """1
        Test given entry for cachemap inclusion and run cache limit checks.
        """
        if entry.url in self.bot.autoplaylist:
            # ignore partial downloads
            if entry.cache_busted:
                log.noise(
                    "Audio cache file is from autoplaylist but marked as busted, ignoring it."
                )
            else:
                self.add_autoplay_cachemap_entry(entry)

        if self.config.save_videos:
            if self.config.storage_limit_bytes:
                # TODO: This could be improved with min/max options, preventing calls to clear on each new entry.
                self.size_bytes = self.size_bytes + entry.downloaded_bytes
                if self.size_bytes > self.config.storage_limit_bytes:
                    log.debug(
                        f"Cache level requires cleanup. {format_size_from_bytes(self.size_bytes)}"
                    )
                    self.delete_old_audiocache()
            elif self.config.storage_limit_days:
                # Only running time check if it is the only option enabled, cuts down on IO.
                self.delete_old_audiocache()

    def load_autoplay_cachemap(self) -> None:
        """
        Load cachemap json file if it exists and settings are enabled.
        Cachemap file path is generated in Config using the auto playlist file name.
        The cache map is a dict with filename keys for playlist url values.
        Filenames are stored without their extension due to ytdl potentially getting a different format.
        """
        if (
            not self.config.storage_retain_autoplay
            or not self.config.auto_playlist
            or not self.config.save_videos
        ):
            self.auto_playlist_cachemap = {}
            return

        if not os.path.isfile(self.config.auto_playlist_cachemap_file):
            log.debug("Autoplaylist has no cache map, moving on.")
            self.auto_playlist_cachemap = {}
            return

        with open(self.config.auto_playlist_cachemap_file, "r") as fh:
            try:
                self.auto_playlist_cachemap = json.load(fh)
                log.info(
                    f"Loaded autoplaylist cache map with {len(self.auto_playlist_cachemap)} entries."
                )
            except Exception:
                log.exception("Failed to load autoplaylist cache map.")
                self.auto_playlist_cachemap = {}

    async def save_autoplay_cachemap(self) -> None:
        """
        Uses asyncio.Lock to save cachemap as a json file, if settings are enabled.
        """
        if (
            not self.config.storage_retain_autoplay
            or not self.config.auto_playlist
            or not self.config.save_videos
        ):
            return

        async with self.cachemap_file_lock:
            try:
                with open(self.config.auto_playlist_cachemap_file, "w") as fh:
                    json.dump(self.auto_playlist_cachemap, fh)
                    log.debug(
                        f"Saved autoplaylist cache map with {len(self.auto_playlist_cachemap)} entries."
                    )
            except Exception:
                log.exception("Failed to save autoplaylist cache map.")

    def add_autoplay_cachemap_entry(self, entry: "BasePlaylistEntry") -> None:
        """
        Store an entry in autoplaylist cachemap, and update the cachemap file if needed.
        """
        if (
            not self.config.storage_retain_autoplay
            or not self.config.auto_playlist
            or not self.config.save_videos
        ):
            return

        change_made = False
        filename = pathlib.Path(entry.filename).stem
        if filename in self.auto_playlist_cachemap:
            if self.auto_playlist_cachemap[filename] != entry.url:
                log.warning(
                    "Autoplaylist cache map conflict on Key: {}  Old: {}  New: {}".format(
                        filename,
                        self.auto_playlist_cachemap[filename],
                        entry.url,
                    )
                )
                self.auto_playlist_cachemap[filename] = entry.url
                change_made = True
        else:
            self.auto_playlist_cachemap[filename] = entry.url
            change_made = True

        if change_made:
            self.bot.loop.create_task(self.save_autoplay_cachemap())

    def remove_autoplay_cachemap_entry(self, entry: "BasePlaylistEntry") -> None:
        """
        Remove an entry from cachemap and update cachemap file if needed.
        """
        if (
            not self.config.storage_retain_autoplay
            or not self.config.auto_playlist
            or not self.config.save_videos
        ):
            return

        filename = pathlib.Path(entry.filename).stem
        if filename in self.auto_playlist_cachemap:
            del self.auto_playlist_cachemap[filename]
            self.bot.loop.create_task(self.save_autoplay_cachemap())

    def remove_autoplay_cachemap_entry_by_url(self, url: str) -> None:
        """
        Remove all entries having the given URL from cachemap and update cachemap if needed.
        """
        if (
            not self.config.storage_retain_autoplay
            or not self.config.auto_playlist
            or not self.config.save_videos
        ):
            return

        to_remove = set()
        for map_key, map_url in self.auto_playlist_cachemap.items():
            if map_url == url:
                to_remove.add(map_key)

        for key in to_remove:
            del self.auto_playlist_cachemap[key]

        if len(to_remove):
            self.bot.loop.create_task(self.save_autoplay_cachemap())

    def _check_autoplay_cachemap(self, filename: pathlib.Path) -> bool:
        """
        Test if filename is a valid autoplaylist file still.
        Returns True if map entry URL is still in autoplaylist.
        If settings are disabled for cache retention this will also return false.
        """
        if (
            not self.config.storage_retain_autoplay
            or not self.config.auto_playlist
            or not self.config.save_videos
        ):
            return False

        if filename.stem in self.auto_playlist_cachemap:
            cached_url = self.auto_playlist_cachemap[filename.stem]
            if cached_url in self.bot.autoplaylist:
                return True

        return False