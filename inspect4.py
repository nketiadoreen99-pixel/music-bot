import yt_dlp, os, glob

base = os.path.dirname(yt_dlp.__file__)
# Search all .py files for supported_js_runtimes
matches = []
for root, dirs, files in os.walk(base):
    for fname in files:
        if not fname.endswith('.py'):
            continue
        fpath = os.path.join(root, fname)
        try:
            with open(fpath, encoding='utf-8', errors='replace') as f:
                for i, line in enumerate(f, 1):
                    if 'supported_js_runtime' in line or 'DenoJSRuntime' in line or 'NodeJSRuntime' in line:
                        matches.append(f"{os.path.relpath(fpath, base)}:{i}: {line.rstrip()}")
        except:
            pass

with open("inspect_out.txt", "w", encoding='utf-8') as out:
    for m in matches[:30]:
        out.write(m + '\n')
