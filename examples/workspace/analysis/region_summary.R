# Totals per region and product. Arguments come from fdp.json.
library(dplyr)

a <- fdp::args()
out <- fdp::output_dir()

con <- fdp::connect("finance-dw")
sales <- DBI::dbGetQuery(con, "select region, product, amount_eur from sales")
sales$amount_eur <- as.numeric(sales$amount_eur)  # decimals arrive as exact strings through Trino
DBI::dbDisconnect(con)

summary <- sales |>
  filter(amount_eur >= a$min_amount) |>
  group_by(region, product) |>
  summarise(total_eur = round(sum(amount_eur)), n = n(), .groups = "drop") |>
  arrange(region, desc(total_eur))

print(as.data.frame(summary))
write.csv(summary, file.path(out, "region_summary.csv"), row.names = FALSE)
message(glue::glue("wrote {nrow(summary)} rows"))
