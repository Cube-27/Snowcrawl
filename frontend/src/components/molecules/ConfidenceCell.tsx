/** A confidence, or a rate such as a config field's yield, as a number with two decimals; "—" when there is none. */
export function confidenceText(confidence: number | null | undefined): string {
  return confidence == null ? "—" : confidence.toFixed(2);
}

/** A field's confidence as a number with two decimals, never a bar: a bar implies a calibration the value may not have.
 * Whether the engine is calibrated is said once per record, by
 * `UncalibratedBadge`, not per field. */
export function ConfidenceCell({ confidence }: Readonly<{ confidence: number | null }>) {
  return <span>{confidenceText(confidence)}</span>;
}
