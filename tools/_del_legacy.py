import io
p = "static/js/app.js"
src = io.open(p, encoding="utf-8").read()
start = src.index("  /* 监控事件（History 页 /api/events，最近 30 条） */")
end_marker = "  function refreshGpuStatus() {"
end = src.index(end_marker, start)
removed = src[start:end]
# sanity: the removed block should contain EVENT_TYPE_LABELS + legacy render + EVENTS_PAGE_SIZE, and NOT refreshGpuStatus
assert "EVENT_TYPE_LABELS" in removed and "legacyRenderEventsList_DISABLED" in removed
assert "refreshGpuStatus" not in removed
assert "EVENTS_PAGE_SIZE" in removed
new = src[:start] + src[end:]
io.open(p, "w", encoding="utf-8", newline="").write(new)
print("removed %d chars, %d lines" % (len(removed), removed.count("\n") + 1))
