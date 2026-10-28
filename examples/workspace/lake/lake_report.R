# Reads the JSON summary that load-lake wrote to RustFS and writes a small text report next to it.
f <- fdp::files("lake-files")
cat("files:", paste(f$list(), collapse = ", "), "\n")

s <- f$read_json("summaries/sales_by_region.json")
regions <- s$regions[order(-s$regions$revenue_eur), ]
print(regions)

lines <- c(
  sprintf("Sales report (data loaded %s)", s$loaded_at),
  sprintf("  %-6s %6d sales  %12.2f EUR", regions$region, regions$sales, regions$revenue_eur),
  sprintf("Top region: %s", regions$region[1])
)
f$write_text("reports/sales_report.txt", lines)
cat("wrote", f$url("reports/sales_report.txt"), "\n")
