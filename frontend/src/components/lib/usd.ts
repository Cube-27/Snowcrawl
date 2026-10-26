const USD = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 4,
});

/** An estimated LLM cost in USD: to four decimals, since one call costs fractions of a cent. */
export function formatUsd(value: number): string {
  return USD.format(value);
}
