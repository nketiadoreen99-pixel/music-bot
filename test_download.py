"""Test download of a YouTube Music URL with the updated yt-dlp options."""
import os, sys, tempfile, shutil

import music_bot as bot

url = "https://music.youtube.com/watch?v=H9NJenpBV2I"
print(f"Test URL: {url}")
print(f"yt-dlp version: {bot.yt_dlp.version.__version__}")
print(f"FFmpeg: {bot._ffmpeg_available()}")
print(f"JS runtime: {bot._find_js_runtime() or 'none'}")
print(f"Cookies: {bool(bot._COOKIEFILE)}")
print()

tmpdir = tempfile.mkdtemp(prefix="test_dl_")
print(f"tmpdir: {tmpdir}")

try:
    path, info = bot.download_audio(url, tmpdir)
    size_mb = os.path.getsize(path) / (1024 * 1024)
    print(f"\nDOWNLOAD SUCCESS")
    print(f"  file: {path}")
    print(f"  size: {size_mb:.2f} MB")
    print(f"  title: {info.get('title', '?')}")
    print(f"  duration: {info.get('duration', '?')}s")
except RuntimeError as e:
    print(f"\nDOWNLOAD FAILED: {e}")
except Exception as e:
    print(f"\nUNEXPECTED ERROR: {e}")
    import traceback
    traceback.print_exc()
finally:
    shutil.rmtree(tmpdir, ignore_errors=True)
    print("\nDONE")
