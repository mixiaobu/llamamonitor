import pathlib

p = pathlib.Path("static/css/mobile.css")
lines = p.read_text(encoding="utf-8").split("\n")
# 0-based indices
# My Round-5 block: line 524..591 (1-based) -> idx 523..590
# Verify boundaries
assert lines[523].strip().startswith("/* ---------- 推理性能页（Round 5）"), lines[523]
assert lines[590].strip().startswith(".slot-table td.slot-detail-btn"), lines[590]
assert lines[591].strip() == "}", repr(lines[591])
# 760 block close: line 459 (1-based) idx 458
assert lines[458].strip() == "}", repr(lines[458])
assert "gpuProcTableWrap" in lines[457], lines[457]

block = lines[523:591]          # my Round-5 mobile rules (68 lines)
# Remove from 360 block: drop lines idx 523..590 (keep the 591 '}')
after_remove = lines[:523] + lines[591:]
# Now the 760-block close shifted? No — removal is AFTER idx 458, so idx 458 unchanged.
# Insert block before idx 458 (the '}' of 760 block) with a blank separator line.
final = after_remove[:458] + [""] + block + after_remove[458:]
p.write_text("\n".join(final), encoding="utf-8")
print("moved", len(block), "lines from 360-block to 760-block")
print("new total lines:", len(final))
