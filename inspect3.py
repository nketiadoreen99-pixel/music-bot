import sys, os
# Find the YoutubeDL.py file
import yt_dlp
ydl_path = os.path.join(os.path.dirname(yt_dlp.__file__), "YoutubeDL.py")
with open(ydl_path, "r", encoding="utf-8") as f:
    lines = f.readlines()
# Find js_runtimes references
with open("inspect_out.txt", "w", encoding="utf-8") as out:
    for i, line in enumerate(lines):
        if "js_runtime" in line.lower() or "_clean_js" in line.lower():
            # Print surrounding context
            start = max(0, i-2)
            end = min(len(lines), i+10)
            for j in range(start, end):
                out.write(f"L{j}: {lines[j]}")
            out.write("---\n")
