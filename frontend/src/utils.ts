/**
 * Shared UK-locale formatting helpers -- GBP currency and en-GB dates.
 * Centralized here so every page formats money/dates the same way rather
 * than each hand-rolling its own `$`/string-concat or US-locale date call.
 */

const gbpFormatter = new Intl.NumberFormat("en-GB", {
  style: "currency",
  currency: "GBP",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

export function formatGBP(amount: number): string {
  return gbpFormatter.format(amount);
}

export function formatDateUK(value: string | Date): string {
  const date = typeof value === "string" ? new Date(value) : value;
  return date.toLocaleDateString("en-GB");
}

export function formatDateTimeUK(value: string | Date): string {
  const date = typeof value === "string" ? new Date(value) : value;
  return date.toLocaleString("en-GB");
}
