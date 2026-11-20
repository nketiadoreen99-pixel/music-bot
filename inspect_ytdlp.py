"""Inspect yt-dlp's js_runtimes handling to find the correct format."""
import inspect
import yt_dlp

# Find the _clean_js_runtimes method
src = inspect.getsource(yt_dlp.YoutubeDL._clean_js_runtimes)
print(src[:3000])
print("\n---DIVIDER---\n")
# Also check the YoutubeDLParams for js_runtimes
src2 = inspect.getsource(yt_dlp.YoutubeDL.__init__)
# Find js_runtimes related lines
for i, line in enumerate(src2.split('\n')):
    if 'js_runtime' in line.lower():
        print(f"  L{i}: {line}")
