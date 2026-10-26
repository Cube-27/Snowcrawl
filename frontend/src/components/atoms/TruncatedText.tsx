import { Tooltip } from "@heroui/react";
import { useCallback, useState } from "react";

/** Text cut to its cell's fixed width. Only text that is cut off gets a tooltip with the full
 * text, on hover or focus, and only it is a tab stop: a cell whose text shows in full is not interactive, and WAI-ARIA
 * APG (Developing a Keyboard Interface) keeps non-interactive content out of the tab order. The tooltip overlays the
 * table, so it moves nothing. */
export function TruncatedText({ text }: Readonly<{ text: string }>) {
  const [truncated, setTruncated] = useState(false);
  // The text's own box is measured, in either form, and again whenever it resizes (CSSOM View: content wider than its
  // box is cut off).
  const measure = useCallback((element: HTMLElement | null) => {
    if (element === null) return;
    const check = () => {
      setTruncated(element.scrollWidth > element.clientWidth);
    };
    check();
    const observer = new ResizeObserver(check);
    observer.observe(element);
    return () => {
      observer.disconnect();
    };
  }, []);
  // Keyed by the text: new text in a box of the same size fires no resize, so the box is measured anew.
  const content = (
    <span key={text} ref={measure} className="block truncate">
      {text}
    </span>
  );
  if (!truncated) return content;
  return (
    <Tooltip delay={0}>
      <Tooltip.Trigger aria-label={text} className="block">
        {content}
      </Tooltip.Trigger>
      <Tooltip.Content>{text}</Tooltip.Content>
    </Tooltip>
  );
}
