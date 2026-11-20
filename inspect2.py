import yt_dlp, inspect, sys

# Try to find _clean_js_runtimes
try:
    src = inspect.getsource(yt_dlp.YoutubeDL._clean_js_runtimes)
    with open("inspect_out.txt", "w", encoding="utf-8") as f:
        f.write(src[:3000])
except Exception as e:
    with open("inspect_out.txt", "w", encoding="utf-8") as f:
        f.write(f"Error: {e}\n")
        # Try to find js_runtimes in the whole module
        import yt_dlp.YoutubeDL as ydl_mod
        full_src = inspect.getsource(ydl_mod)
        for i, line in enumerate(full_src.split('\n')):
            if 'js_runtime' in line.lower():
                f.write(f"  L{i}: {line}\n")
