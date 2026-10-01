import io
p = r"C:\Users\mixiaobu\Desktop\ai\LlamaMonitor\static\index.html"
newp = r"C:\Users\mixiaobu\Desktop\ai\LlamaMonitor\tools\_sys_section_new.html"
with io.open(p, "r", encoding="utf-8", newline="") as f:
    lines = f.readlines()
with io.open(newp, "r", encoding="utf-8", newline="") as f:
    new = f.read()

start = end = None
for i, l in enumerate(lines):
    if 'id="page-system"' in l:
        start = i
    elif start is not None and 'id="page-gpu"' in l:
        end = i
        break
assert start is not None and end is not None, (start, end)
# keep CRLF: detect newline style
nl = "\r\n" if "\r\n" in lines[start] else "\n"
new_lines = [l + nl if not l.endswith("\n") else l for l in new.split("\n")]
# drop a trailing empty line artifact
out = lines[:start] + new_lines + lines[end:]
with io.open(p, "w", encoding="utf-8", newline="") as f:
    f.writelines(out)
print("spliced system section: old lines %d..%d (%d lines) -> new %d lines"%(start, end, end-start, len(new_lines)))
print("total lines now:", len(out))
