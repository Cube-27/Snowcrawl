/** A text shown in a list, with a React key that names it by its content rather than its position. */
export interface KeyedText {
  readonly key: string;
  readonly text: string;
}

/** Keys each text by itself, numbered among equal texts before it, so equal entries of a read-only list stay
 * distinct. */
export function keyedTexts(texts: readonly string[]): KeyedText[] {
  const seen = new Map<string, number>();
  return texts.map((text) => {
    const earlier = seen.get(text) ?? 0;
    seen.set(text, earlier + 1);
    return { key: `${String(earlier)}:${text}`, text };
  });
}
