import React from "react";

interface RiskBadgeProps {
  band: string | null | undefined;
  style?: React.CSSProperties;
}

const BAND_CLASS: Record<string, string> = {
  low: "badge-low",
  medium: "badge-medium",
  high: "badge-high",
};

export default function RiskBadge({ band, style }: RiskBadgeProps) {
  if (!band) {
    return <span className="badge" style={style}>unknown</span>;
  }
  const cls = BAND_CLASS[band] ?? "badge-medium";
  return (
    <span className={`badge ${cls}`} style={style}>
      {band}
    </span>
  );
}
